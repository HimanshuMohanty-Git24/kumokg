"""Visual media preparation for VLM extraction.

* Renders every flagged page of ``needs_ocr_or_vlm`` PDFs to ``.kumokg/media/<doc_id>_p<N>.png`` at 150 DPI,
  skipping perceptually identical pages (dHash).
* Adds standalone images (already normalised by ``audit``) and notebook output PNGs.
* Writes ``vlm_batches.jsonl``: one compact line per image item, pre-assigned to batches of 2-4 images, so
  either the coding harness (Tier 1) or a BYOK vision endpoint (Tier 2) can extract ``ImageEvidence``.
* Optional ``local_ocr`` strategy runs Tesseract through PyMuPDF and stores page text in ``.kumokg/ocr/``
  so the chunker can treat OCR'd pages as text.
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

from .ir import Workspace, read_json, sha256_bytes, utcnow_iso, write_json, write_jsonl
from .modality_router import dhash, hamming

RENDER_DPI = 150
PAGE_DUP_DISTANCE = 3  # out of 256 bits: only near-identical rescans are skipped


def render_pdf_pages(ws: Workspace, doc_id: str, pdf_path: Path, pages: list[int], dpi: int = RENDER_DPI) -> list[dict[str, Any]]:
    import pymupdf
    from PIL import Image

    ws.media_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    hashes: list[tuple[int, int]] = []
    doc = pymupdf.open(str(pdf_path))
    try:
        for pno in pages:
            if pno < 1 or pno > doc.page_count:
                continue
            pix = doc[pno - 1].get_pixmap(dpi=dpi)
            png = pix.tobytes("png")
            img = Image.open(io.BytesIO(png))
            h = dhash(img)
            dup = next((p for p, x in hashes if hamming(h, x) <= PAGE_DUP_DISTANCE), None)
            name = f"{doc_id}_p{pno}.png"
            if dup is not None:
                out.append({"page_no": pno, "duplicate_of_page": dup, "image_path": None})
                continue
            hashes.append((pno, h))
            (ws.media_dir / name).write_bytes(png)
            out.append({"page_no": pno, "image_path": name, "image_sha256": sha256_bytes(png),
                        "width": pix.width, "height": pix.height, "dpi": dpi})
    finally:
        doc.close()
    return out


def local_ocr(ws: Workspace, doc_id: str, pdf_path: Path, pages: list[int], language: str = "eng") -> dict[str, Any]:
    """OCR pages with Tesseract via PyMuPDF. Raises RuntimeError with an install hint when unavailable."""
    import pymupdf

    if shutil.which("tesseract") is None:
        raise RuntimeError("local_ocr needs the Tesseract binary on PATH (and TESSDATA_PREFIX set). "
                           "Install it (e.g. `winget install UB-Mannheim.TesseractOCR`, `brew install tesseract`, "
                           "`apt install tesseract-ocr`) or choose the harness_vlm / byok_vlm remediation instead.")
    texts: dict[str, str] = {}
    doc = pymupdf.open(str(pdf_path))
    try:
        for pno in pages:
            page = doc[pno - 1]
            try:
                tp = page.get_textpage_ocr(language=language, dpi=300, full=True)
            except Exception as exc:  # tesseract present but misconfigured
                raise RuntimeError(f"Tesseract OCR failed on page {pno}: {exc}") from exc
            texts[str(pno)] = page.get_text("text", textpage=tp)
    finally:
        doc.close()
    payload = {"doc_id": doc_id, "engine": "tesseract", "language": language, "created_at": utcnow_iso(), "pages": texts}
    write_json(ws.p(f"ocr/{doc_id}.json"), payload)
    return {"doc_id": doc_id, "pages": len(texts), "chars": sum(len(t) for t in texts.values())}


def prepare_media(ws: Workspace, strategy: str = "render", images_per_batch: int = 3, dpi: int = RENDER_DPI) -> dict[str, Any]:
    """Build ``vlm_batches.jsonl`` (strategy ``render``) or OCR flagged pages (strategy ``local_ocr``)."""
    ws.require()
    report = read_json(ws.audit_path, {}) or {}
    manifest = ws.load_manifest()
    if strategy == "local_ocr":
        results = []
        for n in report.get("needs_ocr_or_vlm", []):
            rec = manifest.source_by_id(n["doc_id"])
            if rec:
                results.append(local_ocr(ws, n["doc_id"], Path(rec.path), n["pages"]))
        return {"strategy": "local_ocr", "documents": results, "next": "kumokg chunk  (OCR text is picked up automatically)"}
    if strategy != "render":
        raise ValueError("strategy must be 'render' or 'local_ocr'")
    images_per_batch = max(2, min(4, images_per_batch))
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for n in report.get("needs_ocr_or_vlm", []):
        rec = manifest.source_by_id(n["doc_id"])
        if rec is None:
            continue
        for r in render_pdf_pages(ws, n["doc_id"], Path(rec.path), n["pages"], dpi=dpi):
            if r.get("image_path") is None:
                skipped.append({"doc_id": n["doc_id"], "page_no": r["page_no"],
                                "reason": f"perceptual duplicate of page {r['duplicate_of_page']}"})
                continue
            items.append({"item_id": f"{n['doc_id']}:p{r['page_no']}", "doc_id": n["doc_id"], "source_kind": "scanned_pdf_page",
                          "page_no": r["page_no"], "image_path": r["image_path"], "image_sha256": r["image_sha256"],
                          "width": r["width"], "height": r["height"], "source_name": rec.name})
    for f in report.get("files", []):
        if f.get("modality") == "image" and f["status"] == "usable":
            d = f["details"]
            items.append({"item_id": f"{f['doc_id']}:image", "doc_id": f["doc_id"], "source_kind": "image", "page_no": None,
                          "image_path": d["media_path"], "image_sha256": d["media_sha256"],
                          "width": d["normalized_size"][0], "height": d["normalized_size"][1], "source_name": f["name"]})
        if f.get("ext") == ".ipynb" and f["status"] == "usable":
            for m in f["details"].get("embedded_images", []):
                items.append({"item_id": f"{f['doc_id']}:cell{m['cell_index']}", "doc_id": f["doc_id"],
                              "source_kind": "notebook_output", "page_no": None, "cell_index": m["cell_index"],
                              "image_path": m["media_path"], "image_sha256": m["sha256"], "source_name": f["name"]})
    for i, it in enumerate(items):
        it["batch_id"] = f"vlm-{i // images_per_batch + 1:04d}"
    write_jsonl(ws.vlm_batches_path, items)
    manifest.set_phase("media_prepared")
    ws.save_manifest(manifest)
    return {"strategy": "render", "items": len(items), "batches": len({it["batch_id"] for it in items}),
            "rendered_pages": sum(1 for it in items if it["source_kind"] == "scanned_pdf_page"),
            "skipped_duplicate_pages": skipped, "manifest": str(ws.vlm_batches_path)}

"""Universal source audit, modality routing, PDF text-density/OCR triage, deterministic chunking and
stratified sampling.

Six modalities are detected: ``structured``, ``semi_structured``, ``notebook_vault``, ``unstructured``,
``image`` and ``web``. ``audit`` never sends anything to an LLM; it only inspects files locally (plus
optional HTTP fetches for URL sources) and writes ``audit-report.json`` + ``manifest.json``.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import sqlite3
import statistics
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .ir import (
    Chunk, Manifest, SourceRecord, Workspace, fold_text, read_json, sha256_bytes, sha256_file, sha256_text,
    slugify, utcnow_iso, write_json, write_jsonl,
)

STRUCTURED_EXT = {".csv", ".tsv", ".xlsx", ".xlsm", ".parquet", ".sqlite", ".sqlite3", ".db"}
SEMI_EXT = {".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".xml"}
NOTEBOOK_VAULT_EXT = {".ipynb", ".md", ".markdown"}
UNSTRUCTURED_EXT = {".pdf", ".docx", ".pptx", ".epub", ".html", ".htm", ".txt", ".text", ".rst"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".bmp"}
URL_MANIFEST_EXT = {".urls"}
SKIP_DIRS = {".kumokg", ".git", "__pycache__", "node_modules", ".venv", ".ipynb_checkpoints"}

PDF_MIN_CHARS_PER_PAGE = 80
PDF_IMAGE_COVERAGE_HIGH = 0.5
VECTOR_ART_MIN_DRAWINGS = 25
PROSE_AVG_CHARS = 120
NEAR_DUP_JACCARD = 0.5
EXACT_DUP_JACCARD = 0.98
IMAGE_MAX_DIM = 1600
IMAGE_DHASH_DUP_DISTANCE = 10  # out of 256 bits (16x16 dHash)
PAYWALL_MARKERS = (
    "subscribe to continue", "subscribe to read", "subscribers only", "this content is for subscribers",
    "create a free account to continue", "sign in to continue reading", "paywall",
)

REMEDIATION_OPTIONS = [
    {"id": "harness_vlm", "label": "Harness VLM (Tier 1, zero-config)",
     "description": "Render the pages to PNG and let the active coding agent read them as image tasks.",
     "command": "kumokg media-prep --workspace {ws} && kumokg export-batches --workspace {ws}"},
    {"id": "byok_vlm", "label": "BYOK vision endpoint (Tier 2)",
     "description": "Render the pages and send them to the KUMOKG_VLM_MODEL configured in .kumokg/.env.",
     "command": "kumokg media-prep --workspace {ws} && kumokg extract --workspace {ws} --extractor byok"},
    {"id": "local_ocr", "label": "Local OCR (Tesseract via PyMuPDF)",
     "description": "Run Tesseract locally; requires the tesseract binary on PATH (TESSDATA_PREFIX set).",
     "command": "kumokg media-prep --workspace {ws} --strategy local_ocr"},
    {"id": "skip", "label": "Skip",
     "description": "Leave these pages out of the graph; they stay listed in the audit report.",
     "command": "kumokg audit <sources> --workspace {ws} --skip {doc_id}"},
]


# --------------------------------------------------------------------------------------------
# Source discovery
# --------------------------------------------------------------------------------------------


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def classify_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in STRUCTURED_EXT:
        return "structured"
    if ext in SEMI_EXT:
        return "semi_structured"
    if ext in NOTEBOOK_VAULT_EXT:
        return "notebook_vault"
    if ext in UNSTRUCTURED_EXT:
        return "unstructured"
    if ext in IMAGE_EXT:
        return "image"
    return None


def discover(inputs: Iterable[str]) -> tuple[list[tuple[Path, Path]], list[str], list[dict]]:
    """Expand directories, file lists and URL manifests into (file, base_dir) pairs and URLs."""
    files: list[tuple[Path, Path]] = []
    urls: list[str] = []
    missing: list[dict] = []
    for raw in inputs:
        raw = str(raw).strip()
        if not raw:
            continue
        if is_url(raw):
            urls.append(raw)
            continue
        p = Path(raw).expanduser()
        if not p.exists():
            missing.append({"path": raw, "reason": "path does not exist", "detail": ""})
            continue
        if p.is_dir():
            base = p.resolve()
            for f in sorted(base.rglob("*")):
                rel_parts = f.relative_to(base).parts
                if any(part in SKIP_DIRS or part.startswith(".") for part in rel_parts):
                    continue
                if f.is_file():
                    if f.suffix.lower() in URL_MANIFEST_EXT:
                        urls.extend(_read_url_manifest(f))
                    else:
                        files.append((f.resolve(), base))
        elif p.suffix.lower() in URL_MANIFEST_EXT:
            urls.extend(_read_url_manifest(p))
        else:
            files.append((p.resolve(), p.resolve().parent))
    seen: set[Path] = set()
    unique = []
    for f, base in files:
        if f not in seen:
            seen.add(f)
            unique.append((f, base))
    return unique, list(dict.fromkeys(urls)), missing


def _read_url_manifest(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and is_url(line):
            out.append(line)
    return out


def make_doc_id(name: str, content_key: str) -> str:
    return f"{slugify(Path(name).stem, 40)}-{content_key[:8]}"


# --------------------------------------------------------------------------------------------
# Readers shared by the compiler and the verifier
# --------------------------------------------------------------------------------------------


def _coerce_cell(v: Any) -> Any:
    if isinstance(v, str):
        return v
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def list_tables(path: Path) -> list[str]:
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv", ".parquet"):
        return [path.stem]
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    if ext in (".sqlite", ".sqlite3", ".db"):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            return [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        finally:
            con.close()
    return []


def read_table(path: Path, table: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (columns, rows) for CSV/TSV/XLSX/Parquet/SQLite. Row indices are 0-based data rows."""
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        delim = "\t" if ext == ".tsv" else ","
        if ext == ".csv":
            try:
                delim = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|").delimiter
            except csv.Error:
                delim = ","
        reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
        rows = [dict(r) for r in reader]
        return list(reader.fieldnames or []), rows
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[table] if table else wb[wb.sheetnames[0]]
            it = ws.iter_rows(values_only=True)
            header = next(it, None) or []
            cols = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(header)]
            rows = []
            for values in it:
                if values is None or all(v is None for v in values):
                    continue
                rows.append({cols[i]: _coerce_cell(values[i]) if i < len(values) else None for i in range(len(cols))})
            return cols, rows
        finally:
            wb.close()
    if ext == ".parquet":
        import duckdb

        con = duckdb.connect()
        try:
            rel = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
            cols = [d[0] for d in rel.description]
            rows = [{c: _coerce_cell(v) for c, v in zip(cols, r)} for r in rel.fetchall()]
            return cols, rows
        finally:
            con.close()
    if ext in (".sqlite", ".sqlite3", ".db"):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            name = table or list_tables(path)[0]
            cur = con.execute(f'SELECT * FROM "{name}"')
            cols = [d[0] for d in cur.description]
            rows = [{c: _coerce_cell(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
            return cols, rows
        finally:
            con.close()
    raise ValueError(f"not a table source: {path}")


def _xml_to_obj(elem: ET.Element) -> Any:
    children = list(elem)
    obj: dict[str, Any] = {f"@{k}": v for k, v in elem.attrib.items()}
    if not children:
        text = (elem.text or "").strip()
        if not obj:
            return text
        if text:
            obj["#text"] = text
        return obj
    groups: dict[str, list] = {}
    for ch in children:
        groups.setdefault(ch.tag.split("}")[-1], []).append(_xml_to_obj(ch))
    for k, v in groups.items():
        obj[k] = v if len(v) > 1 else v[0]
    return obj


def load_semistructured(path: Path) -> Any:
    ext = path.suffix.lower()
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    if ext == ".json":
        return json.loads(text)
    if ext in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if ext in (".yaml", ".yml"):
        import yaml

        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        return docs[0] if len(docs) == 1 else docs
    if ext == ".xml":
        root = ET.fromstring(text)
        return {root.tag.split("}")[-1]: _xml_to_obj(root)}
    raise ValueError(f"not a semi-structured source: {path}")


def escape_pointer_token(tok: str) -> str:
    return str(tok).replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer; raises KeyError/IndexError on failure."""
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise KeyError(f"invalid JSON pointer {pointer!r}")
    cur = doc
    for raw in pointer.split("/")[1:]:
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not tok.isdigit():
                raise KeyError(f"non-numeric index {tok!r} for array")
            cur = cur[int(tok)]
        elif isinstance(cur, dict):
            if tok not in cur:
                raise KeyError(tok)
            cur = cur[tok]
        else:
            raise KeyError(f"cannot descend into scalar at {tok!r}")
    return cur


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
INLINE_FIELD_RE = re.compile(r"^\s*([A-Za-z][\w \-]*?)::\s*(.+)$", re.M)


def parse_markdown_note(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    frontmatter: dict[str, Any] = {}
    body = raw
    body_offset = 0
    m = FRONTMATTER_RE.match(raw)
    if m:
        import yaml

        try:
            fm = yaml.safe_load(m.group(1))
            frontmatter = fm if isinstance(fm, dict) else {}
        except Exception:
            frontmatter = {}
        body = raw[m.end():]
        body_offset = m.end()
    headings = [{"level": len(h.group(1)), "text": h.group(2).strip(), "char": h.start()}
                for h in re.finditer(r"^(#{1,6})\s+(.+)$", body, re.M)]
    links = [{"target": w.group(1).strip(), "alias": (w.group(2) or "").strip(), "char_start": w.start(), "char_end": w.end()}
             for w in WIKILINK_RE.finditer(body)]
    fields = [{"key": f.group(1).strip(), "value": f.group(2).strip(), "char_start": f.start(), "char_end": f.end()}
              for f in INLINE_FIELD_RE.finditer(body)]
    return {"frontmatter": frontmatter, "body": body, "body_offset": body_offset, "headings": headings,
            "wikilinks": links, "inline_fields": fields}


def load_notebook(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def cell_source(cell: dict) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else str(src)


# --------------------------------------------------------------------------------------------
# Per-modality inspectors
# --------------------------------------------------------------------------------------------


def _infer_type(values: list[Any]) -> str:
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return "string"
    def all_match(fn):
        try:
            return all(fn(v) for v in vals)
        except Exception:
            return False
    if all(isinstance(v, bool) for v in vals) or all_match(lambda v: str(v).lower() in ("true", "false")):
        return "bool"
    if all_match(lambda v: isinstance(v, int) or re.fullmatch(r"-?\d+", str(v).strip()) is not None):
        return "int"
    if all_match(lambda v: isinstance(v, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?([eE]-?\d+)?", str(v).strip()) is not None):
        return "float"
    if all_match(lambda v: re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", str(v).strip()) is not None):
        return "date"
    return "string"


def profile_columns(columns: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    n = len(rows)
    for c in columns:
        vals = [r.get(c) for r in rows]
        non_null = [v for v in vals if v not in (None, "")]
        strs = [str(v) for v in non_null]
        avg_len = round(sum(len(s) for s in strs) / len(strs), 1) if strs else 0.0
        uniq = len(set(strs))
        out.append({
            "name": c,
            "type": _infer_type(non_null),
            "non_null": len(non_null),
            "unique": uniq,
            "unique_ratio": round(uniq / len(non_null), 3) if non_null else 0.0,
            "avg_chars": avg_len,
            "is_prose": avg_len > PROSE_AVG_CHARS,
            "sample": strs[:3],
            "rows": n,
        })
    return out


def guess_primary_key(table: str, cols: list[dict[str, Any]]) -> str | None:
    singular = table.lower().rstrip("s")
    ordered = sorted(cols, key=lambda c: (
        0 if c["name"].lower() in ("id", f"{singular}_id", f"{table.lower()}_id") else
        1 if c["name"].lower().endswith("_id") or c["name"].lower().endswith("id") else 2,
        cols.index(c)))
    for c in ordered:
        name = c["name"].lower()
        if c["rows"] and c["non_null"] == c["rows"] and c["unique"] == c["rows"] and (
                name == "id" or name.endswith("_id") or name.endswith("id") or name.endswith("key")):
            return c["name"]
    return None


def guess_name_column(cols: list[dict[str, Any]], pk: str | None) -> str | None:
    preferred = ("name", "title", "label", "full_name", "display_name", "company", "person")
    for p in preferred:
        for c in cols:
            if c["name"].lower() == p:
                return c["name"]
    for c in cols:
        if c["name"] != pk and c["type"] == "string" and not c["is_prose"] and c["unique_ratio"] >= 0.9:
            return c["name"]
    return pk


def inspect_table_source(path: Path) -> dict[str, Any]:
    tables = []
    sqlite_fks: list[dict] = []
    for t in list_tables(path):
        cols, rows = read_table(path, t)
        prof = profile_columns(cols, rows)
        pk = guess_primary_key(t, prof)
        tables.append({
            "table": t, "row_count": len(rows), "columns": prof, "primary_key": pk,
            "name_column": guess_name_column(prof, pk),
            "prose_columns": [c["name"] for c in prof if c["is_prose"]],
            "foreign_keys": [],
        })
    if path.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            for t in tables:
                for fk in con.execute(f'PRAGMA foreign_key_list("{t["table"]}")').fetchall():
                    t["foreign_keys"].append({"column": fk[3], "ref_table": fk[2], "ref_column": fk[4], "method": "declared"})
                    sqlite_fks.append(fk)
        finally:
            con.close()
    return {"tables": tables}


SELF_REF_HINTS = ("parent", "manager", "reports_to", "supervisor", "mentor")
FK_SUFFIXES = ("id", "ids", "_ref", "_refs", "_key")


def link_foreign_keys(structured: list[dict[str, Any]]) -> None:
    """Detect FK candidates across every table: ``*_id`` columns whose values are a subset of another PK."""
    rows_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def rows_of(src: dict, table: str) -> list[dict[str, Any]]:
        key = (src["doc_id"], table)
        if key not in rows_cache:
            rows_cache[key] = read_table(Path(src["path"]), table)[1]
        return rows_cache[key]

    pk_index: list[tuple[str, str, str, set[str]]] = []
    for src in structured:
        for t in src["details"].get("tables", []):
            if t["primary_key"]:
                pk_index.append((src["doc_id"], t["table"], t["primary_key"],
                                 {str(r.get(t["primary_key"])) for r in rows_of(src, t["table"])}))
    for src in structured:
        for t in src["details"].get("tables", []):
            declared = {fk["column"] for fk in t["foreign_keys"]}
            rows = rows_of(src, t["table"])
            for c in t["columns"]:
                lname = c["name"].lower()
                if c["name"] == t["primary_key"] or c["name"] in declared or c["non_null"] == 0:
                    continue
                if not lname.endswith(FK_SUFFIXES):
                    continue
                raw_values = [str(r.get(c["name"])) for r in rows if r.get(c["name"]) not in (None, "")]
                values = {x.strip() for v in raw_values for x in re.split(r"[;|]", v) if x.strip()}
                for doc, table, pk, pks in pk_index:
                    if doc == src["doc_id"] and table == t["table"] and not any(h in lname for h in SELF_REF_HINTS):
                        continue
                    if values and values <= pks:
                        t["foreign_keys"].append({"column": c["name"], "ref_table": table, "ref_doc_id": doc,
                                                  "ref_column": pk, "method": "value-subset",
                                                  "multi": any(re.search(r"[;|]", v) for v in raw_values)})
                        break


def _walk_json(obj: Any, pointer: str, arrays: dict[str, dict], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, list):
        pattern = pointer + "/*"
        objs = [o for o in obj if isinstance(o, dict)]
        if objs:
            info = arrays.setdefault(pattern, {"array_path": pattern, "count": 0, "keys": Counter(), "str_lens": {}})
            info["count"] += len(objs)
            for o in objs:
                for k, v in o.items():
                    info["keys"][k] += 1
                    if isinstance(v, str):
                        info["str_lens"].setdefault(k, []).append(len(v))
        for i, item in enumerate(obj):
            _walk_json(item, pointer + "/*", arrays, depth + 1)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_json(v, f"{pointer}/{escape_pointer_token(k)}", arrays, depth + 1)


def inspect_semistructured(path: Path) -> dict[str, Any]:
    doc = load_semistructured(path)
    arrays: dict[str, dict] = {}
    _walk_json(doc, "", arrays)
    out = []
    for pattern, info in sorted(arrays.items()):
        keys = [k for k, _ in info["keys"].most_common()]
        avg = {k: round(sum(v) / len(v), 1) for k, v in info["str_lens"].items() if v}
        id_keys = [k for k in keys if k.lower() in ("id", "key", "uid", "slug") or k.lower().endswith(("_id", "_ids", "_ref", "_refs"))]
        name_key = next((k for k in keys if k.lower() in ("name", "title", "label", "full_name")), None)
        out.append({
            "array_path": pattern, "count": info["count"], "keys": keys,
            "id_keys": id_keys, "name_key": name_key,
            "prose_fields": [k for k, a in avg.items() if a > PROSE_AVG_CHARS],
        })
    return {"root_type": type(doc).__name__, "object_arrays": out}


def inspect_markdown(path: Path) -> dict[str, Any]:
    note = parse_markdown_note(path)
    body = note["body"]
    return {
        "frontmatter_keys": sorted(note["frontmatter"].keys()),
        "headings": len(note["headings"]),
        "heading_outline": [h["text"] for h in note["headings"][:12]],
        "wikilinks": len(note["wikilinks"]),
        "inline_fields": len(note["inline_fields"]),
        "prose_chars": len(body),
        "is_vault_note": bool(note["frontmatter"] or note["wikilinks"] or note["inline_fields"]),
    }


def extract_notebook_media(ws: Workspace, doc_id: str, nb: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    ws.media_dir.mkdir(parents=True, exist_ok=True)
    for ci, cell in enumerate(nb.get("cells", [])):
        for oi, output in enumerate(cell.get("outputs", []) or []):
            data = (output or {}).get("data", {}) or {}
            for mime, ext in (("image/png", "png"), ("image/jpeg", "jpg")):
                if mime in data:
                    payload = data[mime]
                    if isinstance(payload, list):
                        payload = "".join(payload)
                    try:
                        raw = base64.b64decode(payload)
                    except Exception:
                        continue
                    dest = ws.media_dir / f"{doc_id}_cell{ci}_out{oi}.{ext}"
                    dest.write_bytes(raw)
                    out.append({"cell_index": ci, "output_index": oi, "mime": mime,
                                "media_path": dest.name, "sha256": sha256_bytes(raw)})
    return out


def inspect_notebook(ws: Workspace, doc_id: str, path: Path) -> dict[str, Any]:
    nb = load_notebook(path)
    cells = nb.get("cells", [])
    kinds = Counter(c.get("cell_type", "raw") for c in cells)
    media = extract_notebook_media(ws, doc_id, nb)
    lang = (nb.get("metadata", {}).get("kernelspec", {}) or {}).get("language") or \
        (nb.get("metadata", {}).get("language_info", {}) or {}).get("name")
    return {"cells": len(cells), "markdown_cells": kinds.get("markdown", 0), "code_cells": kinds.get("code", 0),
            "raw_cells": kinds.get("raw", 0), "language": lang, "embedded_images": media}


def _page_image_coverage(page) -> float:
    area = abs(page.rect)
    if not area:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        import pymupdf

        r = pymupdf.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            covered += abs(r)
    return round(min(1.0, covered / area), 3)


def inspect_pdf(path: Path) -> tuple[str, dict[str, Any], str | None]:
    """Return (status, details, exclusion_reason)."""
    import pymupdf

    if path.stat().st_size == 0:
        return "excluded", {}, "empty file (0 bytes)"
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # pymupdf raises FileDataError / RuntimeError on garbage
        return "excluded", {}, f"corrupt or unreadable PDF ({type(exc).__name__}: {exc})"
    try:
        if doc.needs_pass or doc.is_encrypted:
            return "excluded", {"page_count": doc.page_count}, "encrypted PDF (password required)"
        if doc.page_count == 0:
            repaired = " after structural repair" if getattr(doc, "is_repaired", False) else ""
            return "excluded", {}, f"corrupt or unreadable PDF (no recoverable pages{repaired})"
        chars, coverage, drawings = [], [], []
        for page in doc:
            try:
                chars.append(len(page.get_text("text").strip()))
                coverage.append(_page_image_coverage(page))
                drawings.append(len(page.get_drawings()) if chars[-1] < PDF_MIN_CHARS_PER_PAGE else 0)
            except Exception as exc:
                return "excluded", {"page_count": doc.page_count}, f"unreadable page {len(chars) + 1} ({exc})"
        median_chars = statistics.median(chars)
        median_cov = statistics.median(coverage)
        low_pages = [i + 1 for i, c in enumerate(chars) if c < PDF_MIN_CHARS_PER_PAGE]
        visual_pages = [i + 1 for i, c in enumerate(chars)
                        if c < PDF_MIN_CHARS_PER_PAGE and (coverage[i] >= 0.25 or drawings[i] >= VECTOR_ART_MIN_DRAWINGS)]
        details = {
            "page_count": doc.page_count,
            "chars_per_page": chars,
            "median_chars_per_page": median_chars,
            "mean_chars_per_page": round(statistics.mean(chars), 1),
            "image_coverage_per_page": coverage,
            "median_image_coverage": median_cov,
            "low_text_pages": low_pages,
            "scanned_pages": visual_pages,
            "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
        }
        if median_chars < PDF_MIN_CHARS_PER_PAGE:
            if median_cov >= PDF_IMAGE_COVERAGE_HIGH or len(visual_pages) * 2 >= doc.page_count:
                details["ocr_reason"] = (f"median {median_chars} chars/page < {PDF_MIN_CHARS_PER_PAGE} with median image "
                                         f"coverage {median_cov:.2f} (scanned / image-only PDF)")
                return "needs_ocr_or_vlm", details, None
            return "excluded", details, f"no extractable text and no page images (median {median_chars} chars/page)"
        return "usable", details, None
    finally:
        doc.close()


def dhash(img, size: int = 16) -> int:
    from PIL import Image

    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def load_image(path: Path):
    from PIL import Image

    if path.suffix.lower() == ".svg":
        import pymupdf

        doc = pymupdf.open(str(path))
        try:
            pix = doc[0].get_pixmap(dpi=150)
            return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        finally:
            doc.close()
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def normalize_image(ws: Workspace, doc_id: str, path: Path) -> dict[str, Any]:
    img = load_image(path)
    w, h = img.size
    scale = min(1.0, IMAGE_MAX_DIM / max(w, h))
    if scale < 1.0:
        from PIL import Image

        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    ws.media_dir.mkdir(parents=True, exist_ok=True)
    dest = ws.media_dir / f"{doc_id}.png"
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    dest.write_bytes(buf.getvalue())
    return {"width": w, "height": h, "normalized_size": list(img.size), "media_path": dest.name,
            "media_sha256": sha256_bytes(buf.getvalue()), "dhash": f"{dhash(img):064x}"}


# --------------------------------------------------------------------------------------------
# Text extraction (pages) for unstructured / vault / web / notebooks
# --------------------------------------------------------------------------------------------


@dataclass
class PageText:
    page_no: int | None
    text: str
    span_label: str = ""
    modality: str = "text"
    meta: dict[str, Any] = field(default_factory=dict)


def _html_text(html: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "aside", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return title, "\n".join(out).strip()


def _epub_pages(path: Path) -> list[PageText]:
    pages = []
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = next(el.attrib["full-path"] for el in container.iter() if el.tag.endswith("rootfile"))
        opf = ET.fromstring(z.read(rootfile))
        base = str(Path(rootfile).parent).replace("\\", "/")
        manifest = {el.attrib["id"]: el.attrib["href"] for el in opf.iter() if el.tag.endswith("item")}
        spine = [el.attrib["idref"] for el in opf.iter() if el.tag.endswith("itemref")]
        for i, idref in enumerate(spine):
            href = manifest.get(idref)
            if not href:
                continue
            name = f"{base}/{href}" if base not in ("", ".") else href
            try:
                _, text = _html_text(z.read(name).decode("utf-8", errors="replace"))
            except KeyError:
                continue
            if text.strip():
                pages.append(PageText(page_no=i + 1, text=text, span_label=f"section {i + 1}"))
    return pages


def _strip_boilerplate(pages: list[PageText]) -> list[PageText]:
    if len(pages) < 3:
        return pages
    counts: Counter = Counter()
    for p in pages:
        keys = {re.sub(r"\d+", "#", ln.strip()) for ln in p.text.splitlines() if ln.strip()}
        counts.update(keys)
    threshold = max(3, int(len(pages) * 0.4))
    boiler = {k for k, c in counts.items() if c >= threshold and len(k) <= 80}
    if not boiler:
        return pages
    out = []
    for p in pages:
        kept = [ln for ln in p.text.splitlines() if re.sub(r"\d+", "#", ln.strip()) not in boiler]
        out.append(PageText(p.page_no, "\n".join(kept).strip("\n"), p.span_label, p.modality,
                            {**p.meta, "boilerplate_removed": True}))
    return out


def extract_pages(ws: Workspace, rec: SourceRecord, details: dict[str, Any] | None = None) -> list[PageText]:
    """Return clean text pages for any text-bearing source."""
    path = Path(rec.path)
    ext = rec.ext
    details = details or {}
    if rec.modality == "web":
        meta = read_json(Path(rec.path).with_suffix(".json"), {}) or {}
        text = path.read_text(encoding="utf-8")
        return [PageText(None, text, "web", "web", {"url": meta.get("url", rec.rel_path),
                                                    "fetched_at": meta.get("fetched_at", ""),
                                                    "content_sha256": meta.get("content_sha256", "")})]
    if ext == ".pdf":
        import pymupdf

        ocr = read_json(ws.p(f"ocr/{rec.doc_id}.json"), None)
        pages: list[PageText] = []
        doc = pymupdf.open(str(path))
        try:
            for i, page in enumerate(doc):
                text = page.get_text("text")
                if ocr and str(i + 1) in ocr.get("pages", {}) and len(text.strip()) < PDF_MIN_CHARS_PER_PAGE:
                    text = ocr["pages"][str(i + 1)]
                    pages.append(PageText(i + 1, text, f"p{i + 1}", "ocr", {"ocr": True}))
                elif text.strip():
                    pages.append(PageText(i + 1, text, f"p{i + 1}"))
        finally:
            doc.close()
        return _strip_boilerplate(pages)
    if ext in (".txt", ".text", ".rst"):
        return [PageText(None, path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n"), "text")]
    if ext in (".md", ".markdown"):
        note = parse_markdown_note(path)
        return [PageText(None, note["body"], "body", "text", {"body_offset": note["body_offset"]})]
    if ext in (".html", ".htm"):
        _, text = _html_text(path.read_bytes().decode("utf-8", errors="replace"))
        return [PageText(None, text, "html")]
    if ext == ".docx":
        import docx

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return [PageText(None, "\n".join(parts).strip(), "docx")]
    if ext == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        pages = []
        for i, slide in enumerate(prs.slides):
            texts = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                    texts.append(shape.text_frame.text)
            if any(t.strip() for t in texts):
                pages.append(PageText(i + 1, "\n".join(texts).strip(), f"slide {i + 1}"))
        return pages
    if ext == ".epub":
        return _epub_pages(path)
    if ext == ".ipynb":
        nb = load_notebook(path)
        pages = []
        for ci, cell in enumerate(nb.get("cells", [])):
            ctype = cell.get("cell_type", "raw")
            if ctype in ("markdown", "raw"):
                src = cell_source(cell)
                if src.strip():
                    pages.append(PageText(None, src, f"cell {ci}", "notebook",
                                          {"cell_index": ci, "cell_type": ctype}))
        return pages
    if rec.modality == "structured":
        pages = []
        for t in details.get("tables", []):
            if not t.get("prose_columns"):
                continue
            _, rows = read_table(path, t["table"])
            for ri, row in enumerate(rows):
                for col in t["prose_columns"]:
                    val = row.get(col)
                    if isinstance(val, str) and val.strip():
                        pages.append(PageText(None, val, f"{t['table']}[{ri}].{col}", "table_prose",
                                              {"table": t["table"], "row_index": ri, "column": col}))
        return pages
    if rec.modality == "semi_structured":
        doc = load_semistructured(path)
        prose: set[str] = set()
        for arr in details.get("object_arrays", []):
            prose.update(arr.get("prose_fields", []))
        pages = []

        def walk(o: Any, ptr: str) -> None:
            if isinstance(o, dict):
                for k, v in o.items():
                    p = f"{ptr}/{escape_pointer_token(k)}"
                    if k in prose and isinstance(v, str) and v.strip():
                        pages.append(PageText(None, v, p, "json_prose", {"json_pointer": p}))
                    else:
                        walk(v, p)
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{ptr}/{i}")

        walk(doc, "")
        return pages
    return []


# --------------------------------------------------------------------------------------------
# Web fetching
# --------------------------------------------------------------------------------------------


def fetch_url(ws: Workspace, url: str, transport: Any = None) -> tuple[SourceRecord | None, dict[str, Any] | None]:
    """Fetch a URL into ``.kumokg/web/``. Returns (record, None) or (None, exclusion)."""
    import httpx

    doc_id = make_doc_id(slugify((urlparse(url).netloc + urlparse(url).path)[:60] or "page"), sha256_text(url))
    try:
        with httpx.Client(transport=transport, follow_redirects=True, timeout=20.0,
                          headers={"User-Agent": "KumoKG/0.1 (local knowledge-graph builder)"}) as client:
            resp = client.get(url)
    except Exception as exc:
        return None, {"path": url, "reason": f"network error ({type(exc).__name__})", "detail": str(exc)[:200]}
    if resp.status_code in (401, 403):
        return None, {"path": url, "reason": f"access denied (HTTP {resp.status_code})",
                      "detail": "authentication or authorisation required"}
    if resp.status_code in (402, 451):
        return None, {"path": url, "reason": f"paywall or legal block (HTTP {resp.status_code})", "detail": ""}
    if resp.status_code >= 400:
        return None, {"path": url, "reason": f"HTTP {resp.status_code}", "detail": resp.reason_phrase}
    ctype = resp.headers.get("content-type", "")
    body = resp.text
    if "html" in ctype or body.lstrip().startswith("<"):
        title, text = _html_text(body)
    else:
        title, text = "", body.strip()
    lower = text.lower()
    if any(m in lower for m in PAYWALL_MARKERS) and len(text) < 3000:
        return None, {"path": url, "reason": "paywall detected", "detail": f"{len(text)} chars of teaser text"}
    if len(text) < 200:
        return None, {"path": url, "reason": "empty or script-rendered page",
                      "detail": f"only {len(text)} chars of readable text"}
    ws.web_dir.mkdir(parents=True, exist_ok=True)
    dest = ws.web_dir / f"{doc_id}.txt"
    dest.write_text(text, encoding="utf-8")
    meta = {"url": url, "fetched_at": utcnow_iso(), "content_sha256": sha256_text(text), "title": title,
            "status": resp.status_code, "content_type": ctype}
    write_json(dest.with_suffix(".json"), meta)
    rec = SourceRecord(doc_id=doc_id, path=str(dest), rel_path=url, name=title or url, ext=".html",
                       modality="web", size_bytes=len(text.encode()), sha256=meta["content_sha256"])
    return rec, None


# --------------------------------------------------------------------------------------------
# Near-duplicate detection
# --------------------------------------------------------------------------------------------


def shingles(text: str, k: int = 5, mod: int = 4) -> set[int]:
    words = re.findall(r"[a-z0-9]+", fold_text(text).lower())
    out = set()
    for i in range(max(0, len(words) - k + 1)):
        h = int(sha256_text(" ".join(words[i:i + k]))[:12], 16)
        if h % mod == 0:
            out.add(h)
    return out


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------------


def audit(ws: Workspace, inputs: list[str], mode: str = "hitl", tier: str = "harness", dedup: str = "flag",
          skip: Iterable[str] = (), transport: Any = None) -> dict[str, Any]:
    manifest = ws.ensure(mode=mode, tier=tier)
    manifest.mode = mode or manifest.mode
    manifest.extraction_tier = tier or manifest.extraction_tier
    skip = set(skip)
    files, urls, missing = discover(inputs)
    entries: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = list(missing)
    needs_ocr: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    text_shingles: dict[str, set[int]] = {}
    seen_sha: dict[str, str] = {}
    image_hashes: list[tuple[str, int]] = []

    for path, base in files:
        ext = path.suffix.lower()
        modality = classify_ext(ext)
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            rel = path.name
        size = path.stat().st_size
        digest = sha256_file(path)
        doc_id = make_doc_id(path.name, digest)
        entry = {"doc_id": doc_id, "path": str(path), "rel_path": rel, "name": path.name, "ext": ext,
                 "modality": modality or "unknown", "size_bytes": size, "sha256": digest, "status": "usable",
                 "details": {}}
        entries.append(entry)
        if modality is None:
            entry["status"] = "excluded"
            excluded.append({"doc_id": doc_id, "path": str(path), "reason": f"unsupported file type '{ext or 'none'}'",
                             "detail": "no KumoKG modality handles this extension"})
            continue
        if doc_id in skip or path.name in skip:
            entry["status"] = "excluded"
            excluded.append({"doc_id": doc_id, "path": str(path), "reason": "skipped by user (--skip)", "detail": ""})
            continue
        if size == 0:
            entry["status"] = "excluded"
            excluded.append({"doc_id": doc_id, "path": str(path), "reason": "empty file (0 bytes)", "detail": ""})
            continue
        if digest in seen_sha:
            entry["status"] = "duplicate"
            duplicates.append({"doc_id": doc_id, "duplicate_of": seen_sha[digest], "similarity": 1.0, "method": "sha256",
                               "action": "excluded"})
            excluded.append({"doc_id": doc_id, "path": str(path), "reason": f"exact duplicate of {seen_sha[digest]}",
                             "detail": "identical SHA-256 content hash"})
            continue
        seen_sha[digest] = doc_id
        try:
            if modality == "structured":
                entry["details"] = inspect_table_source(path)
            elif modality == "semi_structured":
                entry["details"] = inspect_semistructured(path)
            elif modality == "notebook_vault":
                entry["details"] = inspect_notebook(ws, doc_id, path) if ext == ".ipynb" else inspect_markdown(path)
                if ext != ".ipynb":
                    text_shingles[doc_id] = shingles(parse_markdown_note(path)["body"])
            elif modality == "image":
                info = normalize_image(ws, doc_id, path)
                entry["details"] = info
                h = int(info["dhash"], 16)
                dup = next(((d, hamming(h, x)) for d, x in image_hashes if hamming(h, x) <= IMAGE_DHASH_DUP_DISTANCE), None)
                if dup:
                    entry["status"] = "duplicate"
                    duplicates.append({"doc_id": doc_id, "duplicate_of": dup[0], "similarity": round(1 - dup[1] / 256, 3),
                                       "method": f"dHash hamming {dup[1]}", "action": "excluded"})
                    excluded.append({"doc_id": doc_id, "path": str(path),
                                     "reason": f"near-identical image of {dup[0]} (dHash distance {dup[1]})",
                                     "detail": "perceptual dedup avoids redundant VLM calls"})
                else:
                    image_hashes.append((doc_id, h))
            elif ext == ".pdf":
                status, details, reason = inspect_pdf(path)
                entry["details"] = details
                entry["status"] = status
                if status == "excluded":
                    excluded.append({"doc_id": doc_id, "path": str(path), "reason": reason, "detail": ""})
                elif status == "needs_ocr_or_vlm":
                    needs_ocr.append({
                        "doc_id": doc_id, "path": str(path), "pages": details["scanned_pages"] or details["low_text_pages"],
                        "page_count": details["page_count"], "median_chars_per_page": details["median_chars_per_page"],
                        "median_image_coverage": details["median_image_coverage"], "reason": details.get("ocr_reason", ""),
                        "remediation_options": [
                            {**o, "command": o["command"].format(ws=str(ws.root), doc_id=doc_id)} for o in REMEDIATION_OPTIONS],
                    })
                else:
                    if details.get("scanned_pages"):
                        needs_ocr.append({
                            "doc_id": doc_id, "path": str(path), "pages": details["scanned_pages"], "partial": True,
                            "page_count": details["page_count"], "median_chars_per_page": details["median_chars_per_page"],
                            "median_image_coverage": details["median_image_coverage"],
                            "reason": "mixed PDF: some pages are image-only",
                            "remediation_options": [
                                {**o, "command": o["command"].format(ws=str(ws.root), doc_id=doc_id)} for o in REMEDIATION_OPTIONS],
                        })
                    rec = SourceRecord(doc_id=doc_id, path=str(path), rel_path=rel, name=path.name, ext=ext,
                                       modality=modality, size_bytes=size, sha256=digest)
                    text_shingles[doc_id] = shingles(" ".join(p.text for p in extract_pages(ws, rec)))
            else:
                rec = SourceRecord(doc_id=doc_id, path=str(path), rel_path=rel, name=path.name, ext=ext,
                                   modality=modality, size_bytes=size, sha256=digest)
                pages = extract_pages(ws, rec)
                total = sum(len(p.text) for p in pages)
                entry["details"] = {"pages": len(pages), "chars": total}
                if total < 20:
                    entry["status"] = "excluded"
                    excluded.append({"doc_id": doc_id, "path": str(path), "reason": "no extractable text",
                                     "detail": f"{total} chars"})
                else:
                    text_shingles[doc_id] = shingles(" ".join(p.text for p in pages))
        except Exception as exc:
            entry["status"] = "excluded"
            excluded.append({"doc_id": doc_id, "path": str(path),
                             "reason": f"unreadable {ext} file ({type(exc).__name__})", "detail": str(exc)[:240]})

    for url in urls:
        rec, exc_info = fetch_url(ws, url, transport=transport)
        if exc_info:
            excluded.append(exc_info)
            entries.append({"doc_id": None, "path": url, "rel_path": url, "name": url, "ext": "", "modality": "web",
                            "size_bytes": 0, "sha256": "", "status": "excluded", "details": {}})
            continue
        assert rec is not None
        entries.append({**rec.model_dump(), "details": {"chars": rec.size_bytes, "url": url}})
        text_shingles[rec.doc_id] = shingles(Path(rec.path).read_text(encoding="utf-8"))

    link_foreign_keys([e for e in entries if e["modality"] == "structured" and e["status"] == "usable"])

    # near-duplicate text documents
    by_id = {e["doc_id"]: e for e in entries if e.get("doc_id")}
    ids = sorted(d for d in text_shingles if by_id.get(d, {}).get("status") == "usable")
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if by_id[b]["status"] != "usable" or by_id[a]["status"] != "usable":
                continue
            sim = jaccard(text_shingles[a], text_shingles[b])
            if sim >= NEAR_DUP_JACCARD:
                primary, secondary = _rank_duplicate(by_id[a], by_id[b])
                exact = sim >= EXACT_DUP_JACCARD
                action = "excluded" if (dedup == "skip" or exact) else "kept (cross-source corroboration)"
                duplicates.append({"doc_id": secondary["doc_id"], "duplicate_of": primary["doc_id"],
                                   "similarity": round(sim, 3), "method": "word 5-gram shingle Jaccard", "action": action})
                secondary["details"]["near_duplicate_of"] = primary["doc_id"]
                secondary["details"]["near_duplicate_similarity"] = round(sim, 3)
                if action == "excluded":
                    secondary["status"] = "duplicate"
                    excluded.append({"doc_id": secondary["doc_id"], "path": secondary["path"],
                                     "reason": f"near-duplicate of {primary['doc_id']} ({sim:.0%} shingle overlap)",
                                     "detail": "re-run with --dedup flag to keep both"})

    breakdown = Counter(e["modality"] for e in entries)
    report = {
        "generated_at": utcnow_iso(),
        "workspace": str(ws.root),
        "inputs": [str(i) for i in inputs],
        "sources_scanned": len(entries),
        "modality_breakdown": {m: breakdown.get(m, 0) for m in
                               ("structured", "semi_structured", "notebook_vault", "unstructured", "image", "web")},
        "status_breakdown": dict(Counter(e["status"] for e in entries)),
        "files": entries,
        "usable": [e["doc_id"] for e in entries if e["status"] == "usable"],
        "needs_ocr_or_vlm": needs_ocr,
        "excluded_sources": excluded,
        "duplicates": duplicates,
        "dedup_policy": dedup,
    }
    write_json(ws.audit_path, report)
    manifest.sources = [SourceRecord(**{k: e[k] for k in SourceRecord.model_fields if k in e})
                        for e in entries if e.get("doc_id")]
    manifest.content_hashes = {e["doc_id"]: e["sha256"] for e in entries if e.get("doc_id")}
    manifest.set_phase("audited")
    ws.save_manifest(manifest)
    return report


def _rank_duplicate(a: dict, b: dict) -> tuple[dict, dict]:
    """Primary = source with page anchors (PDF) first, then larger text."""
    def score(e):
        return (1 if e["ext"] == ".pdf" else 0, e["size_bytes"], e["doc_id"])
    return (a, b) if score(a) >= score(b) else (b, a)


def compact_audit(report: dict[str, Any]) -> dict[str, Any]:
    files = []
    for e in report["files"]:
        d = e.get("details", {})
        row = {"doc_id": e["doc_id"], "name": e["name"], "modality": e["modality"], "status": e["status"]}
        if "page_count" in d:
            row.update(pages=d["page_count"], median_chars_per_page=d.get("median_chars_per_page"),
                       median_image_coverage=d.get("median_image_coverage"))
        if d.get("near_duplicate_of"):
            row["near_duplicate_of"] = d["near_duplicate_of"]
            row["similarity"] = d.get("near_duplicate_similarity")
        files.append(row)
    return {
        "sources_scanned": report["sources_scanned"], "modality_breakdown": report["modality_breakdown"],
        "status_breakdown": report["status_breakdown"], "files": files,
        "needs_ocr_or_vlm": [{"doc_id": n["doc_id"], "pages": n["pages"], "options": [o["id"] for o in n["remediation_options"]]}
                             for n in report["needs_ocr_or_vlm"]],
        "excluded_sources": [{"path": Path(x["path"]).name if not is_url(x["path"]) else x["path"], "reason": x["reason"]}
                             for x in report["excluded_sources"]],
        "duplicates": report["duplicates"],
    }


# --------------------------------------------------------------------------------------------
# Chunking & sampling
# --------------------------------------------------------------------------------------------


def split_text(text: str, size: int = 1500, overlap: int = 200) -> list[tuple[int, int]]:
    """Deterministic overlapping spans that prefer paragraph, sentence, line then word boundaries."""
    n = len(text)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + size, n)
        if end < n:
            window_lo = start + int(size * 0.6)
            cut = -1
            for sep in ("\n\n", ". ", "! ", "? ", ".\n", "\n", " "):
                idx = text.rfind(sep, window_lo, end)
                if idx != -1:
                    cut = idx + len(sep)
                    break
            if cut > start:
                end = cut
        s, e = start, end
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            spans.append((s, e))
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)
        while nxt < end and not text[nxt - 1].isspace():
            nxt += 1
        start = nxt if nxt < end else end
    return spans


def chunk_workspace(ws: Workspace, size: int = 1500, overlap: int = 200) -> dict[str, Any]:
    ws.require()
    manifest = ws.load_manifest()
    report = read_json(ws.audit_path, {}) or {}
    details_by_id = {e["doc_id"]: e.get("details", {}) for e in report.get("files", []) if e.get("doc_id")}
    chunks: list[Chunk] = []
    per_doc: dict[str, int] = {}
    for rec in manifest.sources:
        if rec.status not in ("usable", "needs_ocr_or_vlm"):
            continue
        details = details_by_id.get(rec.doc_id, {})
        if rec.modality == "notebook_vault" and rec.ext != ".ipynb" and details.get("is_vault_note") and \
                details.get("prose_chars", 0) == 0:
            continue
        pages = extract_pages(ws, rec, details)
        idx = 0
        for page in pages:
            for s, e in split_text(page.text, size, overlap):
                text = page.text[s:e]
                modality = page.modality
                if rec.modality == "web":
                    modality = "web"
                meta = dict(page.meta)
                label = page.span_label or "span"
                if page.page_no is not None and rec.ext in (".pdf", ".pptx", ".epub"):
                    span = f"p{page.page_no}:{s}-{e}"
                else:
                    span = f"{label}:{s}-{e}"
                chunks.append(Chunk(
                    chunk_id=f"{rec.doc_id}::c{idx:04d}", doc_id=rec.doc_id, modality=modality, page_or_span=span,
                    page_no=page.page_no, char_start=s, char_end=e, text=text, media_path=None,
                    sha256=sha256_text(text), meta=meta,
                ))
                idx += 1
        per_doc[rec.doc_id] = idx
    write_jsonl(ws.chunks_path, chunks)
    ws._chunk_cache = None
    manifest.set_phase("chunked")
    ws.save_manifest(manifest)
    return {"chunks": len(chunks), "per_doc": per_doc, "size": size, "overlap": overlap}


CAP_PHRASE_RE = re.compile(r"\b([A-Z][a-z]+(?:[ \-](?:of |the |de |von )?[A-Z][a-z]+){0,3})\b")
_COMMON_CAPS = set("""The A An And But Or So If When Then There This That These Those It Its He She They We You I
    His Her Their Our Your My What Who Why How Which Where While After Before Oh Well Yes No Not Now Here
    Let Do Does Did Is Are Was Were Be Been Being Have Has Had Shall Will Would Could Should May Might Must
    Chapter In On At Of To For With By From As Up Down Out All Some Any One Two Three Very Just Only Even
    Mr Mrs Ms Dr Sir Miss Four Five Six Seven Eight Nine Ten Eleven Twelve Hundred Thousand First Second Third Last
    Said Says Say Asked Replied Answered Thought Cried Shouted Exclaimed Added Remarked Continued Whispered Went Came
    Looked Began Called Told Ah Alas Hush Hm Hmm Ugh Pray Please Indeed Perhaps Never Nothing Something Everything
    Everybody Nobody Somebody Anything Why Soo Ootiful Beau Ahem""".split())


def discover_terms(chunks: Iterable[Chunk], top: int = 40, min_count: int = 3) -> list[dict[str, Any]]:
    counts: Counter = Counter()
    first_chunk: dict[str, str] = {}
    for ch in chunks:
        text = fold_text(ch.text)
        for m in CAP_PHRASE_RE.finditer(text):
            phrase = m.group(1)
            words = phrase.split()
            if words[0] in _COMMON_CAPS:
                if len(words) == 1:
                    continue
                phrase = " ".join(words[1:])
                if phrase.split()[0] in _COMMON_CAPS:
                    continue
            prev = text[max(0, m.start() - 2):m.start()]
            if len(phrase.split()) == 1 and (m.start() == 0 or re.search(r"[.!?\"'(]\s*$", prev)):
                continue
            counts[phrase] += 1
            first_chunk.setdefault(phrase, ch.chunk_id)
    out = []
    for phrase, c in counts.most_common():
        if c < min_count:
            break
        out.append({"term": phrase, "count": c, "first_chunk": first_chunk[phrase]})
        if len(out) >= top:
            break
    return out


def stratified_sample(ws: Workspace, n: int = 12, preview_chars: int = 700) -> dict[str, Any]:
    """Beginning / middle / end chunks per document plus the highest term-diversity chunks across modalities."""
    chunks = list(ws.load_chunks().values())
    by_doc: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.doc_id, []).append(c)
    picked: list[tuple[Chunk, str]] = []
    seen: set[str] = set()

    def add(c: Chunk, why: str) -> None:
        if c.chunk_id not in seen and len(picked) < n:
            seen.add(c.chunk_id)
            picked.append((c, why))

    docs = sorted(by_doc, key=lambda d: (-len(by_doc[d]), d))
    positional_budget = max(1, n - max(1, n // 3))
    for why in ("beginning", "middle", "end"):
        for doc in docs:
            if len(picked) >= positional_budget:
                break
            lst = by_doc[doc]
            i = {"beginning": 0, "middle": len(lst) // 2, "end": len(lst) - 1}[why]
            add(lst[i], why)

    def diversity(c: Chunk) -> float:
        words = re.findall(r"[A-Za-z]{3,}", c.text)
        caps = {w for w in words if w[0].isupper()}
        return (len(set(w.lower() for w in words)) / (len(words) + 1)) + 0.02 * len(caps)

    by_modality: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_modality.setdefault(c.modality, []).append(c)
    ranked_lists = [sorted(v, key=lambda c: (-diversity(c), c.chunk_id)) for _, v in sorted(by_modality.items())]
    i = 0
    while len(picked) < n and any(i < len(lst) for lst in ranked_lists):
        for lst in ranked_lists:
            if i < len(lst):
                add(lst[i], "high-term-diversity")
        i += 1
    sample = [{"chunk_id": c.chunk_id, "doc_id": c.doc_id, "modality": c.modality, "page_or_span": c.page_or_span,
               "why": why, "text": c.text[:preview_chars] + ("…" if len(c.text) > preview_chars else "")}
              for c, why in picked]
    result = {"total_chunks": len(chunks), "documents": len(docs), "sample": sample,
              "discovered_terms": discover_terms(chunks)}
    write_json(ws.p("sample.json"), result)
    return result

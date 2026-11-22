"""Two-tier extraction.

Tier 1 (harness-native, zero config): ``export_batches`` writes tiny task files (5-8 chunks or 2-4 images,
with the approved ontology + calibration attached) that the active coding agent answers; ``import_candidates``
ingests its JSONL output, resumable by ``chunk_sha256``.

Tier 2 (BYOK): ``run_byok`` drives any OpenAI-compatible endpoint (Groq, OpenRouter, OpenAI, Ollama presets)
through a bounded ``asyncio.Semaphore`` pool with strict schema validation, retry-with-feedback and resume.

Offline baseline: ``extract_heuristic`` is a deterministic gazetteer + cue-phrase extractor so CI and smoke
tests run with zero network access.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .ir import (
    Candidate, Chunk, EntityRef, ImageEvidence, NotebookEvidence, Ontology, TextEvidence, WebEvidence, Workspace,
    append_jsonl, iter_jsonl, normalize_name, read_json, sha256_file, to_predicate, upsert_candidates, utcnow_iso,
    write_json,
)
from .modality_router import cell_source, load_notebook

TEXT_MODALITIES = {"text", "notebook", "table_prose", "json_prose", "web", "ocr"}
MAX_QUOTE_CHARS = 400

# --------------------------------------------------------------------------------------------
# Claim schema (shared by harness batches and BYOK responses)
# --------------------------------------------------------------------------------------------


class ClaimRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    label: str | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chunk_id: str | None = None
    image_item_id: str | None = None
    kind: Literal["node", "edge", "attribute"] = "edge"
    subject: ClaimRef
    predicate: str | None = None
    object: ClaimRef | None = None
    value: Any = None
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    quote: str = ""
    element_description: str | None = None
    bbox_norm: list[float] | None = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    valid_from: str | None = None
    valid_to: str | None = None

    @field_validator("subject", "object", mode="before")
    @classmethod
    def _coerce_ref(cls, v):
        if isinstance(v, str):
            return {"name": v}
        return v

    @model_validator(mode="after")
    def _shape(self):
        if self.kind == "edge" and (not self.predicate or self.object is None):
            raise ValueError("edge claims need 'predicate' and 'object'")
        if self.kind == "attribute" and not self.predicate:
            raise ValueError("attribute claims need 'predicate' (property name) and 'value'")
        if not self.image_item_id and not self.quote.strip():
            raise ValueError("text claims need a verbatim 'quote'")
        if self.bbox_norm is not None and (len(self.bbox_norm) != 4 or not all(0 <= x <= 1 for x in self.bbox_norm)):
            raise ValueError("bbox_norm must be 4 floats in [0,1]")
        return self


class ClaimsPayload(BaseModel):
    claims: list[Claim]


CLAIM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "properties": {"claims": {"type": "array", "items": {
        "type": "object", "required": ["kind", "subject"],
        "properties": {
            "chunk_id": {"type": "string", "description": "id of the chunk the quote comes from (text tasks)"},
            "image_item_id": {"type": "string", "description": "id of the image item (image tasks)"},
            "kind": {"enum": ["node", "edge", "attribute"]},
            "subject": {"type": "object", "required": ["name"],
                        "properties": {"name": {"type": "string"}, "label": {"type": "string"}}},
            "predicate": {"type": "string", "description": "UPPER_SNAKE_CASE relation (edge) or property name (attribute)"},
            "object": {"type": "object", "required": ["name"],
                       "properties": {"name": {"type": "string"}, "label": {"type": "string"}}},
            "value": {"description": "attribute value"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "quote": {"type": "string", "description": "EXACT verbatim substring of the chunk (<= 300 chars)"},
            "element_description": {"type": "string", "description": "image tasks: what visual element shows the fact"},
            "bbox_norm": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}, "minItems": 4, "maxItems": 4},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "valid_from": {"type": ["string", "null"]},
            "valid_to": {"type": ["string", "null"]},
        }}}},
}

TEXT_INSTRUCTIONS = """You are a meticulous knowledge-graph extractor working for KumoKG.
For EVERY chunk in `items`, extract the entities and relations it states, following `ontology`.
Hard rules:
1. Every claim MUST carry `chunk_id` and `quote`: an EXACT, contiguous, verbatim substring of that chunk's text
   (copy characters exactly, <= 300 chars) that states the fact. Claims whose quote is not found are quarantined.
2. Use ontology classes as `label` and ontology predicates (UPPER_SNAKE_CASE) for edges. If an important fact does
   not fit, emit it anyway with your best predicate: it is routed to schema review, never silently added.
3. Use canonical names (see `ontology.aliases`); do not invent entities; only resolve pronouns when the quote names
   the entity.
4. Never repeat anything listed in `calibration.negative_examples`; apply `calibration.predicate_corrections`.
5. `confidence` in [0,1] reflects how explicitly the quote states the claim.
Write one JSON object per line ({"chunk_id": ..., "kind": "edge", ...}) to `output_file`, or a single
{"claims": [...]} object. Then run: kumokg import-candidates --workspace <ws> <output_file>
"""

IMAGE_INSTRUCTIONS = """You are a visual knowledge-graph extractor working for KumoKG.
Open every image in `items` (paths are relative to `media_dir`). Extract entities/relations that are visibly shown
(diagram boxes and arrows, chart labels, scanned text). Every claim MUST carry `image_item_id`,
`element_description` (what you see that proves the claim) and, when possible, `bbox_norm` [x0,y0,x1,y1] in 0..1.
Use `quote` for any text you read verbatim from the image. Follow `ontology` and `calibration` exactly like text tasks.
Write JSONL to `output_file`, then run: kumokg import-candidates --workspace <ws> <output_file>
"""


def extract_json_object(text: str) -> str:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    if t.startswith("{") or t.startswith("["):
        return t
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        return t[s:e + 1]
    return t


# --------------------------------------------------------------------------------------------
# Quote location & evidence construction
# --------------------------------------------------------------------------------------------

_PRESERVE = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "­": "-", "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"', " ": " ", " ": " ",
    "​": " ",
})


def fold_preserving(text: str) -> str:
    """Length-preserving punctuation fold (1 char -> 1 char) so regex offsets map back to the raw text."""
    return text.translate(_PRESERVE)


def find_span(raw: str, quote: str) -> tuple[int, int] | None:
    q = quote.strip()
    if not q:
        return None
    i = raw.find(q)
    if i != -1:
        return i, i + len(q)
    tokens = fold_preserving(q).lower().split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    m = re.search(pattern, fold_preserving(raw).lower())
    return (m.start(), m.end()) if m else None


class SourceIndex:
    """Lazy lookups the extractors need (chunks, source records, vlm items)."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.chunks = ws.load_chunks()
        self.manifest = ws.load_manifest()
        self._vlm: dict[str, dict] | None = None
        self._nb_cache: dict[str, dict] = {}

    def vlm_items(self) -> dict[str, dict]:
        if self._vlm is None:
            self._vlm = {r["item_id"]: r for r in iter_jsonl(self.ws.vlm_batches_path)}
        return self._vlm

    def file_sha(self, doc_id: str) -> str:
        rec = self.manifest.source_by_id(doc_id)
        return rec.sha256 if rec else ""

    def notebook_cell(self, doc_id: str, cell_index: int) -> str:
        rec = self.manifest.source_by_id(doc_id)
        if rec is None:
            return ""
        if doc_id not in self._nb_cache:
            self._nb_cache[doc_id] = load_notebook(Path(rec.path))
        cells = self._nb_cache[doc_id].get("cells", [])
        return cell_source(cells[cell_index]) if 0 <= cell_index < len(cells) else ""

    def locate_chunk(self, quote: str, doc_id: str | None = None) -> Chunk | None:
        for ch in self.chunks.values():
            if doc_id and ch.doc_id != doc_id:
                continue
            if find_span(ch.text, quote):
                return ch
        return None


def make_text_evidence(idx: SourceIndex, chunk: Chunk, quote: str) -> Any:
    """Typed evidence for a quote taken from ``chunk`` (Text, Notebook or Web evidence by modality)."""
    span = find_span(chunk.text, quote)
    q = quote.strip()
    if span:
        q = chunk.text[span[0]:span[1]]
    if chunk.modality == "notebook":
        cell_index = int(chunk.meta.get("cell_index", 0))
        cell = idx.notebook_cell(chunk.doc_id, cell_index)
        abs_start = chunk.char_start + (span[0] if span else 0)
        first = cell[:abs_start].count("\n") + 1
        last = first + q.count("\n")
        return NotebookEvidence(doc_id=chunk.doc_id, cell_index=cell_index,
                                cell_type=chunk.meta.get("cell_type", "markdown"), line_span=(first, last),
                                verbatim_quote=q, file_sha256=idx.file_sha(chunk.doc_id))
    if chunk.modality == "web":
        return WebEvidence(doc_id=chunk.doc_id, url=chunk.meta.get("url", ""), fetched_at=chunk.meta.get("fetched_at", ""),
                           verbatim_quote=q, content_sha256=chunk.meta.get("content_sha256", ""))
    return TextEvidence(doc_id=chunk.doc_id, chunk_id=chunk.chunk_id, page_no=chunk.page_no,
                        char_start=(chunk.char_start + span[0]) if span else None,
                        char_end=(chunk.char_start + span[1]) if span else None,
                        verbatim_quote=q, chunk_sha256=chunk.sha256)


def claim_to_candidate(idx: SourceIndex, claim: Claim, extractor: str, default_chunk_id: str | None = None,
                       default_item_id: str | None = None) -> tuple[Candidate | None, str | None]:
    evidence: list[Any] = []
    chunk_sha = None
    item_id = claim.image_item_id or default_item_id
    chunk_id = claim.chunk_id or default_chunk_id
    if item_id and (claim.image_item_id or not chunk_id):
        item = idx.vlm_items().get(item_id)
        if item is None:
            return None, f"unknown image_item_id {item_id!r}"
        desc = claim.element_description or claim.quote or "visual element"
        evidence.append(ImageEvidence(doc_id=item["doc_id"], image_path=item["image_path"], page_no=item.get("page_no"),
                                      element_description=desc[:500],
                                      bbox_norm=tuple(claim.bbox_norm) if claim.bbox_norm else None,
                                      image_sha256=item["image_sha256"]))
        chunk_sha = item["image_sha256"]
    else:
        chunk = idx.chunks.get(chunk_id) if chunk_id and chunk_id != "auto" else None
        if chunk is None:
            if chunk_id and chunk_id != "auto":
                return None, f"unknown chunk_id {chunk_id!r}"
            chunk = idx.locate_chunk(claim.quote)
            if chunk is None:
                return None, "quote could not be located in any chunk (chunk_id missing)"
        evidence.append(make_text_evidence(idx, chunk, claim.quote[:MAX_QUOTE_CHARS]))
        chunk_sha = chunk.sha256
    cand = Candidate(
        kind=claim.kind, subject=EntityRef(name=claim.subject.name.strip(), label=claim.subject.label),
        predicate=claim.predicate,
        object=EntityRef(name=claim.object.name.strip(), label=claim.object.label) if claim.object else None,
        value=claim.value, aliases=claim.aliases, properties=claim.properties, confidence=claim.confidence,
        valid_from=claim.valid_from, valid_to=claim.valid_to, evidence=evidence, extractor=extractor,
        chunk_sha256=chunk_sha,
    )
    return cand.finalize(), None


# --------------------------------------------------------------------------------------------
# Processed ledger (resume)
# --------------------------------------------------------------------------------------------


def processed_ledger(ws: Workspace, extractors: Iterable[str] | None = None) -> set[str]:
    wanted = set(extractors) if extractors else None
    return {r["sha256"] for r in iter_jsonl(ws.p("processed.jsonl")) if wanted is None or r.get("extractor") in wanted}


def mark_processed(ws: Workspace, shas: Iterable[str], extractor: str, claims: int = 0) -> None:
    rows = [{"sha256": s, "extractor": extractor, "at": utcnow_iso(), "claims": claims} for s in shas]
    if rows:
        append_jsonl(ws.p("processed.jsonl"), rows)


# --------------------------------------------------------------------------------------------
# Tier 1: harness batches
# --------------------------------------------------------------------------------------------


def compact_ontology(ont: Ontology) -> dict[str, Any]:
    return {
        "closed_world": ont.closed_world,
        "classes": [{"name": c.name, "description": c.description,
                     "properties": {k: v.type for k, v in c.properties.items()},
                     "examples": [e.name for e in c.examples][:5], "seed_terms": c.seed_terms[:15]}
                    for c in ont.entity_classes],
        "relations": [{"source": r.source_class, "predicate": r.predicate, "target": r.target_class,
                       "functional": r.functional, "symmetric": r.symmetric, "description": r.description}
                      for r in ont.relations],
        "aliases": [{"canonical": a.canonical, "aliases": a.aliases, "class": a.entity_class} for a in ont.alias_rules],
    }


def compact_calibration(cal: dict[str, Any]) -> dict[str, Any]:
    return {"negative_examples": cal.get("negative_examples", [])[-30:],
            "predicate_corrections": cal.get("predicate_corrections", []),
            "direction_corrections": cal.get("direction_corrections", []),
            "alias_merges": cal.get("alias_merges", [])}


def export_batches(ws: Workspace, chunks_per_batch: int = 6, images_per_batch: int = 3, kind: str = "all",
                   include_processed: bool = False) -> dict[str, Any]:
    ws.require()
    ont = ws.require_ontology()
    cal = ws.load_calibration()
    chunks_per_batch = max(5, min(8, chunks_per_batch))
    images_per_batch = max(2, min(4, images_per_batch))
    done = set() if include_processed else processed_ledger(ws, {"harness"})
    ws.batches_dir.mkdir(parents=True, exist_ok=True)
    for old in ws.batches_dir.glob("batch-*.json"):
        old.unlink()
    index: list[dict[str, Any]] = []
    common = {"ontology": compact_ontology(ont), "calibration": compact_calibration(cal),
              "output_schema": CLAIM_JSON_SCHEMA, "created_at": utcnow_iso()}
    n = 0
    if kind in ("all", "text"):
        pending = [c for c in ws.load_chunks().values() if c.modality in TEXT_MODALITIES and c.sha256 not in done]
        for i in range(0, len(pending), chunks_per_batch):
            n += 1
            group = pending[i:i + chunks_per_batch]
            bid = f"batch-{n:04d}"
            out_file = ws.batches_dir / f"{bid}.out.jsonl"
            payload = {"batch_id": bid, "kind": "text", "instructions": TEXT_INSTRUCTIONS, "output_file": str(out_file),
                       **common,
                       "items": [{"chunk_id": c.chunk_id, "chunk_sha256": c.sha256, "doc_id": c.doc_id,
                                  "page_no": c.page_no, "modality": c.modality, "text": c.text} for c in group]}
            write_json(ws.batches_dir / f"{bid}.json", payload)
            index.append({"batch_id": bid, "kind": "text", "file": str(ws.batches_dir / f"{bid}.json"),
                          "output_file": str(out_file), "items": len(group),
                          "item_ids": [c.chunk_id for c in group], "shas": [c.sha256 for c in group]})
    if kind in ("all", "image"):
        items = [it for it in iter_jsonl(ws.vlm_batches_path) if it["image_sha256"] not in done]
        for i in range(0, len(items), images_per_batch):
            n += 1
            group = items[i:i + images_per_batch]
            bid = f"batch-{n:04d}"
            out_file = ws.batches_dir / f"{bid}.out.jsonl"
            payload = {"batch_id": bid, "kind": "image", "instructions": IMAGE_INSTRUCTIONS, "output_file": str(out_file),
                       "media_dir": str(ws.media_dir), **common, "items": group}
            write_json(ws.batches_dir / f"{bid}.json", payload)
            index.append({"batch_id": bid, "kind": "image", "file": str(ws.batches_dir / f"{bid}.json"),
                          "output_file": str(out_file), "items": len(group),
                          "item_ids": [g["item_id"] for g in group], "shas": [g["image_sha256"] for g in group]})
    write_json(ws.batches_dir / "index.json", {"created_at": utcnow_iso(), "batches": index})
    return {"batches": len(index), "text_batches": sum(1 for b in index if b["kind"] == "text"),
            "image_batches": sum(1 for b in index if b["kind"] == "image"),
            "items": sum(b["items"] for b in index), "index": str(ws.batches_dir / "index.json"),
            "files": [b["file"] for b in index]}


def _iter_claim_lines(path: Path) -> Iterable[tuple[int, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return
    if text.startswith("{") and '"claims"' in text[:200] and "\n{" not in text:
        try:
            obj = json.loads(text)
            yield 1, obj
            return
        except json.JSONDecodeError:
            pass
    for ln, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if line:
            try:
                yield ln, json.loads(line)
            except json.JSONDecodeError as exc:
                yield ln, exc


def import_candidates(ws: Workspace, files: list[str | Path], extractor: str = "harness") -> dict[str, Any]:
    ws.require()
    idx = SourceIndex(ws)
    index = read_json(ws.batches_dir / "index.json", {}) or {}
    by_out = {str(Path(b["output_file"]).resolve()): b for b in index.get("batches", [])}
    existing = {c.candidate_id for c in ws.load_candidates()}
    new: list[Candidate] = []
    errors: list[dict[str, Any]] = []
    touched: set[str] = set()
    for f in files:
        path = Path(f)
        batch = by_out.get(str(path.resolve()))
        default_chunk = batch["item_ids"][0] if batch and batch["kind"] == "text" and len(batch["item_ids"]) == 1 else None
        for ln, obj in _iter_claim_lines(path):
            if isinstance(obj, Exception):
                errors.append({"file": path.name, "line": ln, "error": f"invalid JSON: {obj}"})
                continue
            groups: list[tuple[dict, str | None]] = []
            if isinstance(obj, dict) and isinstance(obj.get("claims"), list):
                groups = [(c, obj.get("chunk_id")) for c in obj["claims"]]
                if obj.get("chunk_id") in idx.chunks:
                    touched.add(idx.chunks[obj["chunk_id"]].sha256)
            elif isinstance(obj, dict):
                groups = [(obj, None)]
            for raw, chunk_hint in groups:
                try:
                    claim = Claim.model_validate(raw)
                except ValidationError as exc:
                    errors.append({"file": path.name, "line": ln, "error": exc.errors()[0]["msg"]})
                    continue
                cand, err = claim_to_candidate(idx, claim, extractor, default_chunk_id=chunk_hint or default_chunk)
                if err:
                    errors.append({"file": path.name, "line": ln, "error": err})
                    continue
                assert cand is not None
                new.append(cand)
                if cand.chunk_sha256:
                    touched.add(cand.chunk_sha256)
        if batch:
            touched.update(batch["shas"])
    fresh = [c for c in new if c.candidate_id not in existing]
    res = upsert_candidates(ws, new)
    already = processed_ledger(ws, {extractor})
    mark_processed(ws, sorted(touched - already), extractor)
    m = ws.load_manifest()
    m.set_phase("extracted")
    ws.save_manifest(m)
    return {"claims_read": len(new) + len(errors), "imported": len(fresh), "duplicates": len(new) - len(fresh),
            "errors": errors, "candidates_total": res["total"], "chunks_marked_processed": len(touched - already)}


# --------------------------------------------------------------------------------------------
# Deterministic heuristic extractor (offline baseline)
# --------------------------------------------------------------------------------------------

SENT_BOUNDARY = re.compile(r"[.!?]['\"’”)]*(?=\s)|\n\s*\n")
MAX_PAIR_GAP = 160


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in SENT_BOUNDARY.finditer(text):
        end = m.end()
        if end > start:
            spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    out = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e - s >= 3:
            out.append((s, e))
    return out


def _surface_regex(surface: str) -> str:
    parts = [re.escape(p) for p in fold_preserving(surface).split()]
    return r"\s+".join(parts)


class Gazetteer:
    """Surface form -> (canonical name, class), compiled into two longest-first regexes."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, bool], tuple[str, str]] = {}

    def add(self, surface: str, canonical: str, cls: str) -> None:
        surface = surface.strip()
        if len(surface) < 2 or not cls:
            return
        cs = any(ch.isupper() for ch in surface)
        key = (fold_preserving(surface) if cs else fold_preserving(surface).lower(), cs)
        self.entries.setdefault(key, (canonical, cls))

    def compile(self) -> None:
        def build(case_sensitive: bool):
            surfaces = sorted({k for k, cs in self.entries if cs == case_sensitive}, key=lambda s: (-len(s), s))
            if not surfaces:
                return None
            alt = "|".join(_surface_regex(s) for s in surfaces)
            return re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", 0 if case_sensitive else re.I)
        self.rx_cs = build(True)
        self.rx_ci = build(False)

    def mentions(self, text: str) -> list[dict[str, Any]]:
        folded = fold_preserving(text)
        found = []
        for rx, cs in ((self.rx_cs, True), (self.rx_ci, False)):
            if rx is None:
                continue
            for m in rx.finditer(folded):
                surf = " ".join(m.group(0).split())
                key = (surf if cs else surf.lower(), cs)
                if key not in self.entries:
                    continue
                canonical, cls = self.entries[key]
                found.append({"start": m.start(), "end": m.end(), "surface": text[m.start():m.end()],
                              "canonical": canonical, "class": cls})
        found.sort(key=lambda x: (x["start"], -(x["end"] - x["start"])))
        out, last_end = [], -1
        for f in found:
            if f["start"] >= last_end:
                out.append(f)
                last_end = f["end"]
        return out


def build_gazetteer(ws: Workspace, ont: Ontology, cal: dict[str, Any]) -> Gazetteer:
    gz = Gazetteer()
    classes = ont.class_names()
    for a in ont.alias_rules:
        cls = a.entity_class or _class_for_term(ont, a.canonical)
        if cls in classes:
            gz.add(a.canonical, a.canonical, cls)
            for al in a.aliases:
                gz.add(al, a.canonical, cls)
    for merge in cal.get("alias_merges", []):
        cls = merge.get("label") or _class_for_term(ont, merge["canonical"])
        if cls in classes:
            gz.add(merge["alias"], merge["canonical"], cls)
    for c in ont.entity_classes:
        for t in c.seed_terms:
            gz.add(t, t, c.name)
    for cand in ws.load_candidates():
        if cand.kind == "node" and cand.extractor == "structured" and cand.subject.label in classes:
            gz.add(cand.subject.name, cand.subject.name, cand.subject.label)
            for al in cand.aliases:
                gz.add(al, cand.subject.name, cand.subject.label)
    gz.compile()
    return gz


def _class_for_term(ont: Ontology, term: str) -> str | None:
    key = normalize_name(term)
    for c in ont.entity_classes:
        if any(normalize_name(t) == key for t in c.seed_terms):
            return c.name
    return None


def _cue_regex(cues: list[str]):
    cues = [c.strip() for c in cues if c.strip()]
    if not cues:
        return None
    alt = "|".join(r"\s+".join(re.escape(w) for w in fold_preserving(c).lower().split()) for c in sorted(cues, key=len, reverse=True))
    return re.compile(rf"(?<![a-z])(?:{alt})(?![a-z])")


def _quote_span(text: str, s_start: int, s_end: int, a: int, b: int, limit: int = 320) -> tuple[int, int]:
    if s_end - s_start <= limit:
        return s_start, s_end
    lo = max(s_start, a - 60)
    hi = min(s_end, b + 60)
    while lo > s_start and not text[lo - 1].isspace():
        lo -= 1
    while hi < s_end and not text[hi].isspace():
        hi += 1
    return lo, hi


def extract_heuristic(ws: Workspace, chunk_ids: Iterable[str] | None = None) -> dict[str, Any]:
    ws.require()
    ont = ws.require_ontology()
    cal = ws.load_calibration()
    idx = SourceIndex(ws)
    gz = build_gazetteer(ws, ont, cal)
    fwd: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
    inv: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
    for r in ont.relations:
        rx = _cue_regex(r.cue_phrases)
        if rx is not None:
            fwd.setdefault((r.source_class, r.target_class), []).append((r, rx))
            if r.symmetric and r.source_class != r.target_class:
                fwd.setdefault((r.target_class, r.source_class), []).append((r, rx))
        irx = _cue_regex(r.inverse_cue_phrases)
        if irx is not None:
            inv.setdefault((r.target_class, r.source_class), []).append((r, irx))
    corrections = {(c["from"], c.get("source_class"), c.get("target_class")): c["to"]
                   for c in cal.get("predicate_corrections", [])}
    wanted = set(chunk_ids) if chunk_ids else None
    cands: list[Candidate] = []
    n_chunks = 0
    shas = []
    for chunk in idx.chunks.values():
        if chunk.modality not in TEXT_MODALITIES or (wanted and chunk.chunk_id not in wanted):
            continue
        n_chunks += 1
        shas.append(chunk.sha256)
        text = chunk.text
        folded_lower = fold_preserving(text).lower()
        node_seen: set[tuple[str, str]] = set()
        for s_start, s_end in sentence_spans(text):
            ms = gz.mentions(text[s_start:s_end])
            for m in ms:
                m["start"] += s_start
                m["end"] += s_start
            for m in ms:
                key = (m["class"], m["canonical"])
                if key in node_seen:
                    continue
                node_seen.add(key)
                qs, qe = _quote_span(text, s_start, s_end, m["start"], m["end"], limit=280)
                ev = make_text_evidence(idx, chunk, text[qs:qe])
                aliases = [m["surface"]] if normalize_name(m["surface"]) != normalize_name(m["canonical"]) else []
                cands.append(Candidate(kind="node", subject=EntityRef(name=m["canonical"], label=m["class"]),
                                       aliases=aliases, confidence=0.9, evidence=[ev], extractor="heuristic",
                                       chunk_sha256=chunk.sha256).finalize())
            for i, left in enumerate(ms):
                for j in range(i + 1, len(ms)):
                    right = ms[j]
                    gap = right["start"] - left["end"]
                    if gap > MAX_PAIR_GAP:
                        break
                    if left["canonical"] == right["canonical"] and left["class"] == right["class"]:
                        continue
                    between = folded_lower[left["end"]:right["start"]]
                    for table, flip in ((fwd, False), (inv, True)):
                        for rule, rx in table.get((left["class"], right["class"]), []):
                            cue = rx.search(between)
                            if not cue:
                                continue
                            cue_abs = left["end"] + cue.start()
                            if any(k["class"] == left["class"] and left["end"] <= k["start"] < cue_abs for k in ms[i + 1:j]):
                                continue
                            if any(k["class"] == right["class"] and cue_abs < k["start"] < right["start"] for k in ms[i + 1:j]):
                                continue
                            src, tgt = (right, left) if flip else (left, right)
                            if not flip and rule.symmetric and (rule.source_class, rule.target_class) != (src["class"], tgt["class"]):
                                src, tgt = tgt, src
                            pred = corrections.get((rule.predicate, src["class"], tgt["class"]),
                                                   corrections.get((rule.predicate, None, None), rule.predicate))
                            tight = cue.start() <= 3 and (len(between) - cue.end()) <= 14
                            conf = 0.55 + (0.25 if tight else 0.0)
                            conf += 0.1 if normalize_name(left["surface"]) == normalize_name(left["canonical"]) and \
                                normalize_name(right["surface"]) == normalize_name(right["canonical"]) else 0.0
                            conf -= 0.15 if gap > 80 else 0.0
                            qs, qe = _quote_span(text, s_start, s_end, left["start"], right["end"])
                            ev = make_text_evidence(idx, chunk, text[qs:qe])
                            cands.append(Candidate(
                                kind="edge", subject=EntityRef(name=src["canonical"], label=src["class"]),
                                predicate=to_predicate(pred), object=EntityRef(name=tgt["canonical"], label=tgt["class"]),
                                confidence=round(max(0.3, min(0.95, conf)), 2), evidence=[ev], extractor="heuristic",
                                chunk_sha256=chunk.sha256, properties={"cue": cue.group(0)}).finalize())
    res = upsert_candidates(ws, cands, replace_extractors={"heuristic"} if wanted is None else ())
    already = processed_ledger(ws, {"heuristic"})
    mark_processed(ws, sorted(set(shas) - already), "heuristic")
    m = ws.load_manifest()
    m.set_phase("extracted")
    ws.save_manifest(m)
    return {"extractor": "heuristic", "chunks": n_chunks, "candidates": len(cands),
            "edges": sum(1 for c in cands if c.kind == "edge"), "nodes": sum(1 for c in cands if c.kind == "node"),
            "candidates_total": res["total"]}


# --------------------------------------------------------------------------------------------
# Tier 2: BYOK async pool
# --------------------------------------------------------------------------------------------

PRESETS: dict[str, dict[str, str]] = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
             "vlm_model": "meta-llama/llama-4-scout-17b-16e-instruct"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct",
                   "vlm_model": "qwen/qwen2.5-vl-72b-instruct"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "vlm_model": "gpt-4o-mini"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "llama3.1", "vlm_model": "llava"},
}


def load_byok_config(ws: Workspace, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    from dotenv import dotenv_values

    file_env = {k: v for k, v in (dotenv_values(ws.env_path) if ws.env_path.exists() else {}).items() if v is not None}
    env = {**file_env, **{k: v for k, v in os.environ.items() if k.startswith("KUMOKG_")}, **(overrides or {})}
    preset_name = (env.get("KUMOKG_LLM_PRESET") or "").lower()
    preset = PRESETS.get(preset_name, {})
    cfg = {
        "preset": preset_name or None,
        "base_url": env.get("KUMOKG_LLM_BASE_URL") or preset.get("base_url"),
        "api_key": env.get("KUMOKG_LLM_API_KEY", ""),
        "model": env.get("KUMOKG_LLM_MODEL") or preset.get("model"),
        "vlm_model": env.get("KUMOKG_VLM_MODEL") or preset.get("vlm_model") or env.get("KUMOKG_LLM_MODEL"),
        "concurrency": int(env.get("KUMOKG_LLM_CONCURRENCY", "4")),
        "timeout": float(env.get("KUMOKG_LLM_TIMEOUT", "60")),
    }
    missing = [k for k in ("base_url", "model") if not cfg[k]]
    local = bool(cfg["base_url"]) and ("localhost" in cfg["base_url"] or "127.0.0.1" in cfg["base_url"])
    if not cfg["api_key"] and not local:
        missing.append("api_key")
    cfg["missing"] = missing
    return cfg


def _text_messages(ont_c: dict, cal_c: dict, chunk: Chunk) -> list[dict[str, Any]]:
    user = {"ontology": ont_c, "calibration": cal_c, "output_schema": CLAIM_JSON_SCHEMA,
            "items": [{"chunk_id": chunk.chunk_id, "text": chunk.text}]}
    return [{"role": "system", "content": TEXT_INSTRUCTIONS + "\nReply with a single JSON object {\"claims\": [...]} and nothing else."},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


def _image_messages(ws: Workspace, ont_c: dict, cal_c: dict, item: dict) -> list[dict[str, Any]]:
    raw = (ws.media_dir / item["image_path"]).read_bytes()
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    text = json.dumps({"ontology": ont_c, "calibration": cal_c, "output_schema": CLAIM_JSON_SCHEMA,
                       "items": [{"image_item_id": item["item_id"], "source": item.get("source_name"),
                                  "page_no": item.get("page_no")}]}, ensure_ascii=False)
    return [{"role": "system", "content": IMAGE_INSTRUCTIONS + "\nReply with a single JSON object {\"claims\": [...]} and nothing else."},
            {"role": "user", "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": data_url}}]}]


async def _byok_pool(ws: Workspace, cfg: dict[str, Any], tasks: list[dict[str, Any]], transport: Any, max_retries: int,
                     backoff: float, concurrency: int) -> dict[str, Any]:
    import httpx

    idx = SourceIndex(ws)
    ont_c = compact_ontology(ws.require_ontology())
    cal_c = compact_calibration(ws.load_calibration())
    sem = asyncio.Semaphore(max(1, concurrency))
    stats = {"requests": 0, "retries": 0, "ok": 0, "failed": 0, "claims": 0, "invalid_claims": 0, "max_in_flight": 0}
    in_flight = 0
    failures: list[dict[str, Any]] = []
    partial = ws.p("byok_partial.jsonl")
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    async with httpx.AsyncClient(base_url=cfg["base_url"].rstrip("/") + "/", transport=transport, headers=headers,
                                 timeout=cfg["timeout"]) as client:

        async def run(task: dict[str, Any]) -> None:
            nonlocal in_flight
            async with sem:
                in_flight += 1
                stats["max_in_flight"] = max(stats["max_in_flight"], in_flight)
                try:
                    if task["kind"] == "text":
                        messages = _text_messages(ont_c, cal_c, task["chunk"])
                        model = cfg["model"]
                    else:
                        messages = _image_messages(ws, ont_c, cal_c, task["item"])
                        model = cfg["vlm_model"]
                    last_error = "no attempt made"
                    for attempt in range(max_retries + 1):
                        if attempt:
                            stats["retries"] += 1
                            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
                        stats["requests"] += 1
                        try:
                            resp = await client.post("chat/completions", json={
                                "model": model, "messages": messages, "temperature": 0,
                                "response_format": {"type": "json_object"}})
                        except httpx.HTTPError as exc:
                            last_error = f"transport error: {exc}"
                            continue
                        if resp.status_code == 429 or resp.status_code >= 500:
                            last_error = f"HTTP {resp.status_code}"
                            continue
                        if resp.status_code >= 400:
                            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                            break
                        try:
                            content = resp.json()["choices"][0]["message"]["content"]
                            payload = ClaimsPayload.model_validate_json(extract_json_object(content))
                        except (ValidationError, KeyError, IndexError, ValueError) as exc:
                            last_error = f"schema validation failed: {str(exc)[:300]}"
                            messages = messages + [
                                {"role": "assistant", "content": str(resp.text)[:2000]},
                                {"role": "user", "content": "Your previous reply did not match the required JSON schema "
                                 f"({last_error}). Reply again with ONLY a valid JSON object {{\"claims\": [...]}}."}]
                            continue
                        cands = []
                        for claim in payload.claims:
                            if task["kind"] == "text":
                                cand, err = claim_to_candidate(idx, claim, "byok", default_chunk_id=task["chunk"].chunk_id)
                            else:
                                cand, err = claim_to_candidate(idx, claim, "byok", default_item_id=task["item"]["item_id"])
                            if cand is None:
                                stats["invalid_claims"] += 1
                            else:
                                cands.append(cand)
                        append_jsonl(partial, cands)
                        mark_processed(ws, [task["sha"]], "byok", len(cands))
                        stats["ok"] += 1
                        stats["claims"] += len(cands)
                        return
                    stats["failed"] += 1
                    failures.append({"item": task["id"], "error": last_error})
                finally:
                    in_flight -= 1

        await asyncio.gather(*(run(t) for t in tasks))
    stats["failures"] = failures
    return stats


def run_byok(ws: Workspace, kind: str = "text", concurrency: int | None = None, transport: Any = None,
             max_retries: int = 3, backoff: float = 0.5, overrides: dict[str, str] | None = None,
             limit: int | None = None) -> dict[str, Any]:
    ws.require()
    cfg = load_byok_config(ws, overrides)
    if cfg["missing"]:
        raise RuntimeError(f"BYOK config incomplete: missing {', '.join(cfg['missing'])}. Copy "
                           "assets/templates/env.example to <workspace>/.kumokg/.env and set KUMOKG_LLM_PRESET "
                           "(groq|openrouter|openai|ollama) + KUMOKG_LLM_API_KEY, or use the harness tier "
                           "(`kumokg export-batches`).")
    partial = ws.p("byok_partial.jsonl")
    if partial.exists():
        upsert_candidates(ws, [Candidate.model_validate(r) for r in iter_jsonl(partial)])
        partial.unlink()
    done = processed_ledger(ws, {"byok"})
    tasks: list[dict[str, Any]] = []
    if kind in ("text", "all"):
        for ch in ws.load_chunks().values():
            if ch.modality in TEXT_MODALITIES and ch.sha256 not in done:
                tasks.append({"kind": "text", "chunk": ch, "sha": ch.sha256, "id": ch.chunk_id})
    if kind in ("image", "all"):
        for it in iter_jsonl(ws.vlm_batches_path):
            if it["image_sha256"] not in done:
                tasks.append({"kind": "image", "item": it, "sha": it["image_sha256"], "id": it["item_id"]})
    if limit is not None:
        tasks = tasks[:limit]
    skipped = len(done)
    t0 = time.perf_counter()
    stats = asyncio.run(_byok_pool(ws, cfg, tasks, transport, max_retries, backoff, concurrency or cfg["concurrency"]))
    if partial.exists():
        upsert_candidates(ws, [Candidate.model_validate(r) for r in iter_jsonl(partial)])
        partial.unlink()
    m = ws.load_manifest()
    m.set_phase("extracted")
    ws.save_manifest(m)
    return {"extractor": "byok", "model": cfg["model"], "tasks": len(tasks), "skipped_already_processed": skipped,
            "seconds": round(time.perf_counter() - t0, 3), **stats}

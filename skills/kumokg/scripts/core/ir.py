"""Bi-temporal Kumo-IR: Pydantic v2 models, workspace layout, deterministic ids and the SQLite FTS5 store.

Everything KumoKG persists lives under ``<workspace>/.kumokg/``. All writes are atomic (temp file +
``os.replace`` with a short retry loop for Windows file locks) and every identifier is derived
deterministically from content so that re-running a phase is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable, Iterator, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SCHEMA_VERSION = "1.0"
STATE_DIR = ".kumokg"

# --------------------------------------------------------------------------------------------
# Hashing, slugs, normalisation
# --------------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utcnow_iso() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_TRANSLATE = str.maketrans(
    {
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
        "−": "-", "­": "",
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'", "´": "'",
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
        " ": " ", " ": " ", "​": "",
    }
)
_WS = re.compile(r"\s+")


def fold_text(text: str) -> str:
    """Unicode-fold punctuation (dashes, quotes, backticks) without changing case or spacing."""
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATE)


def normalize_text(text: str) -> str:
    """Whitespace/case/punctuation-normalised form used for grounding comparisons."""
    return _WS.sub(" ", fold_text(text)).strip().lower()


_ARTICLES = re.compile(r"^(the|a|an)\s+", re.I)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_name(name: str) -> str:
    """Canonical comparison key for entity names (articles, possessives and punctuation removed)."""
    s = normalize_text(name)
    s = re.sub(r"'s\b", "", s)
    s = _ARTICLES.sub("", s)
    s = _NON_ALNUM.sub(" ", s)
    return _WS.sub(" ", s).strip()


def slugify(text: str, max_len: int = 80) -> str:
    s = unicodedata.normalize("NFKD", fold_text(str(text))).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    if not s:
        s = "x" + sha256_text(str(text))[:10]
    if len(s) > max_len:
        s = s[: max_len - 9].rstrip("-") + "-" + sha256_text(text)[:8]
    return s


def to_predicate(text: str) -> str:
    """Normalise any phrase to an UPPER_SNAKE_CASE relation type."""
    s = unicodedata.normalize("NFKD", fold_text(str(text))).encode("ascii", "ignore").decode()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    if not s:
        s = "RELATED_TO"
    if s[0].isdigit():
        s = "R_" + s
    return s


def to_class_name(text: str) -> str:
    """Normalise any phrase to a PascalCase entity class name."""
    s = unicodedata.normalize("NFKD", fold_text(str(text))).encode("ascii", "ignore").decode()
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", s) if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts)
    if not out:
        out = "Entity"
    if out[0].isdigit():
        out = "C" + out
    return out


def make_node_id(label: str, name: str) -> str:
    return f"{slugify(label, 40)}:{slugify(name)}"


def make_edge_id(source_id: str, edge_type: str, target_id: str, valid_from: str | None = None) -> str:
    return "e-" + sha256_text("|".join([source_id, edge_type, target_id, valid_from or ""]))[:16]


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------------------------
# Evidence discriminated union
# --------------------------------------------------------------------------------------------


class _EvidenceBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str


class TextEvidence(_EvidenceBase):
    kind: Literal["text"] = "text"
    chunk_id: str
    page_no: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    verbatim_quote: str
    chunk_sha256: str = ""


class TableEvidence(_EvidenceBase):
    kind: Literal["table"] = "table"
    sheet_or_table: str
    row_index: int
    column_name: str
    cell_value: Any = None
    file_sha256: str = ""


class JsonEvidence(_EvidenceBase):
    kind: Literal["json"] = "json"
    json_pointer: str
    raw_value: Any = None
    file_sha256: str = ""

    @field_validator("json_pointer")
    @classmethod
    def _pointer(cls, v: str) -> str:
        if v and not v.startswith("/"):
            raise ValueError("json_pointer must be an RFC 6901 pointer starting with '/'")
        return v


class NotebookEvidence(_EvidenceBase):
    kind: Literal["notebook"] = "notebook"
    cell_index: int
    cell_type: Literal["markdown", "code", "raw"]
    line_span: tuple[int, int]
    verbatim_quote: str
    file_sha256: str = ""


class ImageEvidence(_EvidenceBase):
    kind: Literal["image"] = "image"
    image_path: str
    page_no: int | None = None
    element_description: str
    bbox_norm: tuple[float, float, float, float] | None = None
    image_sha256: str = ""

    @field_validator("bbox_norm")
    @classmethod
    def _bbox(cls, v):
        if v is not None and not all(0.0 <= float(x) <= 1.0 for x in v):
            raise ValueError("bbox_norm values must be normalised to [0, 1]")
        return v


class WebEvidence(_EvidenceBase):
    kind: Literal["web"] = "web"
    url: str
    fetched_at: str
    verbatim_quote: str
    content_sha256: str = ""


Evidence = Annotated[
    Union[TextEvidence, TableEvidence, JsonEvidence, NotebookEvidence, ImageEvidence, WebEvidence],
    Field(discriminator="kind"),
]
EvidenceAdapter: TypeAdapter = TypeAdapter(Evidence)
EVIDENCE_KINDS = ("text", "table", "json", "notebook", "image", "web")


def parse_evidence(data: dict) -> Any:
    return EvidenceAdapter.validate_python(data)


def evidence_text(ev: Any) -> str:
    """Human-readable grounding string for any evidence kind."""
    kind = ev.kind
    if kind in ("text", "notebook", "web"):
        return ev.verbatim_quote
    if kind == "table":
        return f"{ev.sheet_or_table}[{ev.row_index}].{ev.column_name} = {ev.cell_value}"
    if kind == "json":
        return f"{ev.json_pointer} = {json.dumps(ev.raw_value, ensure_ascii=False, default=str)}"
    return ev.element_description


def evidence_key(ev: Any) -> str:
    d = ev.model_dump(mode="json")
    return sha256_text(stable_json(d))[:20]


# --------------------------------------------------------------------------------------------
# Graph models
# --------------------------------------------------------------------------------------------

EdgeStatus = Literal["approved", "pending", "quarantined", "superseded"]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    labels: list[str]
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=utcnow)
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return self.labels[0] if self.labels else "Entity"


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_id: str
    target_id: str
    type: str
    directed: bool = True
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    valid_from: str | None = None
    valid_to: str | None = None
    ingested_at: datetime = Field(default_factory=utcnow)
    superseded_by: str | None = None
    status: EdgeStatus = "approved"
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def _upper_snake(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", v):
            raise ValueError(f"edge type must be UPPER_SNAKE_CASE, got {v!r}")
        return v

    def active_at(self, as_of: str | None) -> bool:
        if as_of is None:
            return self.status == "approved" and self.valid_to is None
        if self.status not in ("approved", "superseded"):
            return False
        if self.valid_from and self.valid_from > as_of:
            return False
        if self.valid_to and self.valid_to <= as_of:
            return False
        return True


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def node_map(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def edge_map(self) -> dict[str, Edge]:
        return {e.id: e for e in self.edges}

    def sort(self) -> "Graph":
        self.nodes.sort(key=lambda n: n.id)
        self.edges.sort(key=lambda e: e.id)
        return self

    def content_hash(self) -> str:
        payload = {
            "n": [(n.id, n.labels, n.canonical_name, sorted(n.aliases)) for n in sorted(self.nodes, key=lambda x: x.id)],
            "e": [
                (e.id, e.source_id, e.type, e.target_id, e.status, e.valid_from, e.valid_to)
                for e in sorted(self.edges, key=lambda x: x.id)
            ],
        }
        return sha256_text(stable_json(payload))[:16]


# --------------------------------------------------------------------------------------------
# Ontology models
# --------------------------------------------------------------------------------------------

PropType = Literal["string", "int", "float", "bool", "date"]


class PropertySchema(BaseModel):
    type: PropType = "string"
    functional: bool = True
    description: str = ""


class GroundedExample(BaseModel):
    name: str
    quote: str
    doc_id: str | None = None
    chunk_id: str | None = None
    page_no: int | None = None
    surface: str | None = None


class EntityClass(BaseModel):
    name: str
    description: str = ""
    properties: dict[str, PropertySchema] = Field(default_factory=dict)
    examples: list[GroundedExample] = Field(default_factory=list)
    seed_terms: list[str] = Field(default_factory=list)


class RelationRule(BaseModel):
    source_class: str
    predicate: str
    target_class: str
    functional: bool = False
    symmetric: bool = False
    description: str = ""
    cue_phrases: list[str] = Field(default_factory=list)
    inverse_cue_phrases: list[str] = Field(default_factory=list)

    @field_validator("predicate")
    @classmethod
    def _pred(cls, v: str) -> str:
        return to_predicate(v)


class AliasRule(BaseModel):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    entity_class: str | None = None


class FieldRelation(BaseModel):
    field: str
    predicate: str
    target_class: str
    target_match: Literal["id", "name"] = "name"
    target_mapping: str | None = None
    separator: str | None = None
    valid_from_field: str | None = None
    valid_to_field: str | None = None

    @field_validator("predicate")
    @classmethod
    def _pred(cls, v: str) -> str:
        return to_predicate(v)


class ParentRelation(BaseModel):
    predicate: str
    parent_class: str

    @field_validator("predicate")
    @classmethod
    def _pred(cls, v: str) -> str:
        return to_predicate(v)


class StructuredMapping(BaseModel):
    mapping_id: str
    kind: Literal["table", "json", "notebook", "vault"]
    source: str
    table: str
    entity_class: str
    id_field: str | None = None
    name_field: str
    property_fields: dict[str, PropType] = Field(default_factory=dict)
    prose_fields: list[str] = Field(default_factory=list)
    relations: list[FieldRelation] = Field(default_factory=list)
    parent_relation: ParentRelation | None = None
    row_count: int = 0
    enabled: bool = True


class Ontology(BaseModel):
    schema_version: str = SCHEMA_VERSION
    namespace: str = "https://kumokg.dev/kg/"
    prefix: str = "kg"
    closed_world: bool = True
    entity_classes: list[EntityClass] = Field(default_factory=list)
    relations: list[RelationRule] = Field(default_factory=list)
    alias_rules: list[AliasRule] = Field(default_factory=list)
    structured_mappings: list[StructuredMapping] = Field(default_factory=list)
    discovered_terms: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""

    def class_names(self) -> set[str]:
        return {c.name for c in self.entity_classes}

    def get_class(self, name: str) -> EntityClass | None:
        for c in self.entity_classes:
            if c.name == name:
                return c
        return None

    def rules_for(self, predicate: str) -> list[RelationRule]:
        return [r for r in self.relations if r.predicate == predicate]

    def allows(self, source_class: str, predicate: str, target_class: str) -> bool:
        for r in self.relations:
            if r.predicate != predicate:
                continue
            if r.source_class == source_class and r.target_class == target_class:
                return True
            if r.symmetric and r.source_class == target_class and r.target_class == source_class:
                return True
        return False

    def is_functional(self, predicate: str, source_class: str | None = None) -> bool:
        for r in self.relations:
            if r.predicate == predicate and (source_class is None or r.source_class == source_class) and r.functional:
                return True
        return False

    def ensure_class(self, name: str, description: str = "") -> EntityClass:
        existing = self.get_class(name)
        if existing:
            return existing
        cls = EntityClass(name=name, description=description)
        self.entity_classes.append(cls)
        return cls

    def ensure_relation(self, source_class: str, predicate: str, target_class: str, functional: bool = False,
                        description: str = "") -> RelationRule:
        predicate = to_predicate(predicate)
        for r in self.relations:
            if (r.source_class, r.predicate, r.target_class) == (source_class, predicate, target_class):
                return r
        rule = RelationRule(source_class=source_class, predicate=predicate, target_class=target_class,
                            functional=functional, description=description)
        self.relations.append(rule)
        return rule

    def fingerprint(self) -> str:
        core = {
            "c": sorted(self.class_names()),
            "r": sorted((r.source_class, r.predicate, r.target_class, r.functional) for r in self.relations),
            "cw": self.closed_world,
        }
        return sha256_text(stable_json(core))[:16]


# --------------------------------------------------------------------------------------------
# Manifest, chunks, candidates
# --------------------------------------------------------------------------------------------

Modality = Literal["structured", "semi_structured", "notebook_vault", "unstructured", "image", "web"]


class SourceRecord(BaseModel):
    doc_id: str
    path: str
    rel_path: str
    name: str
    ext: str
    modality: str
    size_bytes: int = 0
    sha256: str = ""
    status: str = "usable"


class Manifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    workspace: str
    mode: Literal["hitl", "autonomous"] = "hitl"
    extraction_tier: Literal["harness", "byok"] = "harness"
    phase: str = "init"
    sources: list[SourceRecord] = Field(default_factory=list)
    content_hashes: dict[str, str] = Field(default_factory=dict)
    gates: dict[str, str | None] = Field(default_factory=lambda: {"gate1": None, "gate2": None})
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)

    def source_by_id(self, doc_id: str) -> SourceRecord | None:
        for s in self.sources:
            if s.doc_id == doc_id:
                return s
        return None

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.updated_at = utcnow_iso()
        self.history.append({"phase": phase, "at": self.updated_at})


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    modality: str
    page_or_span: str
    page_no: int | None = None
    char_start: int
    char_end: int
    text: str
    media_path: str | None = None
    sha256: str
    meta: dict[str, Any] = Field(default_factory=dict)


class EntityRef(BaseModel):
    name: str
    label: str | None = None
    id: str | None = None


class Candidate(BaseModel):
    candidate_id: str = ""
    kind: Literal["node", "edge", "attribute"]
    subject: EntityRef
    predicate: str | None = None
    object: EntityRef | None = None
    value: Any = None
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.5
    valid_from: str | None = None
    valid_to: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    extractor: str = "heuristic"
    chunk_sha256: str | None = None

    def compute_id(self) -> str:
        payload = [
            self.extractor,
            self.kind,
            self.subject.label or "",
            normalize_name(self.subject.name),
            self.predicate or "",
            (self.object.label or "") if self.object else "",
            normalize_name(self.object.name) if self.object else "",
            stable_json(self.value),
            self.valid_from or "",
            sorted(evidence_key(e) for e in self.evidence),
        ]
        return "c-" + sha256_text(stable_json(payload))[:16]

    def finalize(self) -> "Candidate":
        if self.predicate and self.kind == "edge":
            self.predicate = to_predicate(self.predicate)
        if self.subject.label:
            self.subject.label = to_class_name(self.subject.label)
        if self.object and self.object.label:
            self.object.label = to_class_name(self.object.label)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if not self.candidate_id:
            self.candidate_id = self.compute_id()
        return self


# --------------------------------------------------------------------------------------------
# Atomic file IO
# --------------------------------------------------------------------------------------------


def _replace_with_retry(tmp: str, dest: Path) -> None:
    for attempt in range(40):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, dest)


def atomic_write_text(path: str | Path, text: str) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=dest.suffix, dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        _replace_with_retry(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path: str | Path, obj: Any) -> None:
    if isinstance(obj, BaseModel):
        text = obj.model_dump_json(indent=2)
    else:
        text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    atomic_write_text(path, text + "\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    for attempt in range(20):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(0.05)
    return json.loads(p.read_text(encoding="utf-8"))


def write_jsonl(path: str | Path, rows: Iterable[Any]) -> int:
    lines = []
    for r in rows:
        if isinstance(r, BaseModel):
            lines.append(r.model_dump_json())
        else:
            lines.append(json.dumps(r, ensure_ascii=False, default=str))
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return len(lines)


def append_jsonl(path: str | Path, rows: Iterable[Any]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write((r.model_dump_json() if isinstance(r, BaseModel) else json.dumps(r, ensure_ascii=False, default=str)) + "\n")
            n += 1
    return n


def iter_jsonl(path: str | Path) -> Iterator[dict]:
    p = Path(path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


# --------------------------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------------------------


class Workspace:
    """Paths and typed accessors for ``<root>/.kumokg/``."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.dir = self.root / STATE_DIR
        self._chunk_cache: dict[str, Chunk] | None = None

    # paths -----------------------------------------------------------------------------
    def p(self, name: str) -> Path:
        return self.dir / name

    @property
    def manifest_path(self) -> Path: return self.p("manifest.json")
    @property
    def audit_path(self) -> Path: return self.p("audit-report.json")
    @property
    def ontology_draft_path(self) -> Path: return self.p("ontology.draft.json")
    @property
    def ontology_approved_path(self) -> Path: return self.p("ontology.approved.json")
    @property
    def chunks_path(self) -> Path: return self.p("chunks.jsonl")
    @property
    def candidates_path(self) -> Path: return self.p("candidates.jsonl")
    @property
    def verify_path(self) -> Path: return self.p("verify-report.json")
    @property
    def decisions_path(self) -> Path: return self.p("decisions.json")
    @property
    def calibration_path(self) -> Path: return self.p("calibration.json")
    @property
    def graph_path(self) -> Path: return self.p("graph.json")
    @property
    def sqlite_path(self) -> Path: return self.p("graph.sqlite")
    @property
    def changelog_path(self) -> Path: return self.p("changelog.jsonl")
    @property
    def env_path(self) -> Path: return self.p(".env")
    @property
    def media_dir(self) -> Path: return self.p("media")
    @property
    def web_dir(self) -> Path: return self.p("web")
    @property
    def exports_dir(self) -> Path: return self.p("exports")
    @property
    def batches_dir(self) -> Path: return self.p("batches")
    @property
    def vlm_batches_path(self) -> Path: return self.p("vlm_batches.jsonl")
    @property
    def studio_path(self) -> Path: return self.p("studio.json")

    # lifecycle -------------------------------------------------------------------------
    def exists(self) -> bool:
        return self.manifest_path.exists()

    def ensure(self, mode: str = "hitl", tier: str = "harness") -> Manifest:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            atomic_write_text(gi, ".env\nmedia/\nweb/\nexports/\nbatches/\n*.sqlite-journal\n")
        if self.manifest_path.exists():
            return self.load_manifest()
        m = Manifest(workspace=str(self.root), mode=mode, extraction_tier=tier)
        m.set_phase("init")
        self.save_manifest(m)
        return m

    def require(self) -> None:
        if not self.exists():
            raise FileNotFoundError(f"No KumoKG workspace at {self.root} (run `kumokg init` or `kumokg audit` first)")

    # typed accessors -------------------------------------------------------------------
    def load_manifest(self) -> Manifest:
        return Manifest.model_validate(read_json(self.manifest_path))

    def save_manifest(self, m: Manifest) -> None:
        m.updated_at = utcnow_iso()
        write_json(self.manifest_path, m)

    def load_ontology(self, approved: bool = True) -> Ontology | None:
        path = self.ontology_approved_path if approved else self.ontology_draft_path
        data = read_json(path)
        return Ontology.model_validate(data) if data else None

    def require_ontology(self) -> Ontology:
        ont = self.load_ontology(True)
        if ont is None:
            raise FileNotFoundError("ontology.approved.json is missing: approve the ontology at Gate 1 first "
                                    "(`kumokg studio --gate gate1 --blocking` or `kumokg approve-ontology`)")
        return ont

    def load_chunks(self, refresh: bool = False) -> dict[str, Chunk]:
        try:
            st = self.chunks_path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if self._chunk_cache is None or refresh or stamp != getattr(self, "_chunk_stamp", None):
            self._chunk_cache = {r["chunk_id"]: Chunk.model_validate(r) for r in iter_jsonl(self.chunks_path)}
            self._chunk_stamp = stamp
        return self._chunk_cache

    def load_candidates(self) -> list[Candidate]:
        return [Candidate.model_validate(r) for r in iter_jsonl(self.candidates_path)]

    def load_graph(self) -> Graph:
        data = read_json(self.graph_path)
        return Graph.model_validate(data) if data else Graph()

    def save_graph(self, graph: Graph) -> None:
        graph.sort()
        graph.meta["content_hash"] = graph.content_hash()
        graph.meta["updated_at"] = utcnow_iso()
        write_json(self.graph_path, graph)

    def load_decisions(self) -> dict[str, Any]:
        return read_json(self.decisions_path, {}) or {}

    def load_calibration(self) -> dict[str, Any]:
        cal = read_json(self.calibration_path, None) or {}
        cal.setdefault("negative_examples", [])
        cal.setdefault("alias_merges", [])
        cal.setdefault("predicate_corrections", [])
        cal.setdefault("direction_corrections", [])
        cal.setdefault("thresholds", {"auto_approve": 0.75})
        return cal

    def source_path(self, doc_id: str) -> Path | None:
        if not self.exists():
            return None
        rec = self.load_manifest().source_by_id(doc_id)
        return Path(rec.path) if rec else None


# --------------------------------------------------------------------------------------------
# SQLite FTS5 graph store
# --------------------------------------------------------------------------------------------

_STOP = set(
    """a an and are as at be but by for from has have how i in is it its of on or that the this to was
    were what when where which who whom whose why will with does did do via then than into onto
    about which whose there their they them he she him her his hers you your our we us me my""".split()
)


def fts_query(text: str, max_terms: int = 24) -> str:
    """Build a safe FTS5 MATCH expression (OR of quoted terms) from free text."""
    terms: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9]+", fold_text(text).lower()):
        if len(tok) < 2 or tok in _STOP or tok in terms:
            continue
        terms.append(tok)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in terms)


class GraphStore:
    """Indexed local store backed by SQLite with FTS5 virtual tables (rebuilt transactionally)."""

    DDL = [
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        """CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, label TEXT, labels TEXT, canonical_name TEXT,
            aliases TEXT, properties TEXT, confidence REAL, created_at TEXT, evidence TEXT)""",
        """CREATE TABLE IF NOT EXISTS edges (id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT, type TEXT,
            directed INTEGER, properties TEXT, confidence REAL, valid_from TEXT, valid_to TEXT, ingested_at TEXT,
            superseded_by TEXT, status TEXT, evidence TEXT)""",
        "CREATE INDEX IF NOT EXISTS edges_src ON edges(source_id)",
        "CREATE INDEX IF NOT EXISTS edges_tgt ON edges(target_id)",
        """CREATE TABLE IF NOT EXISTS evidence (rowid INTEGER PRIMARY KEY, owner_kind TEXT, owner_id TEXT,
            kind TEXT, doc_id TEXT, quote TEXT, payload TEXT)""",
        "CREATE INDEX IF NOT EXISTS evidence_owner ON evidence(owner_id)",
        "CREATE TABLE IF NOT EXISTS communities (graph_hash TEXT, community_id INTEGER, node_id TEXT)",
        "CREATE TABLE IF NOT EXISTS community_meta (graph_hash TEXT, community_id INTEGER, size INTEGER, payload TEXT)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(id UNINDEXED, name, aliases, labels, quotes)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS edges_fts USING fts5(id UNINDEXED, type, source_name, target_name, quotes)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(owner_id UNINDEXED, owner_kind UNINDEXED, quote)",
    ]

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        for stmt in self.DDL:
            con.execute(stmt)
        return con

    def rebuild(self, graph: Graph) -> dict[str, int]:
        names = {n.id: n.canonical_name for n in graph.nodes}
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            for table in ("nodes", "edges", "evidence", "nodes_fts", "edges_fts", "evidence_fts"):
                con.execute(f"DELETE FROM {table}")
            n_ev = 0
            for n in graph.nodes:
                quotes = " ".join(evidence_text(e) for e in n.evidence[:8])
                con.execute(
                    "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                    (n.id, n.label, json.dumps(n.labels), n.canonical_name, json.dumps(n.aliases, ensure_ascii=False),
                     json.dumps(n.properties, ensure_ascii=False, default=str), n.confidence, n.created_at.isoformat(),
                     json.dumps([e.model_dump(mode="json") for e in n.evidence], ensure_ascii=False)),
                )
                con.execute("INSERT INTO nodes_fts VALUES (?,?,?,?,?)",
                            (n.id, n.canonical_name, " ".join(n.aliases), " ".join(n.labels), quotes))
                for e in n.evidence:
                    con.execute("INSERT INTO evidence (owner_kind, owner_id, kind, doc_id, quote, payload) VALUES (?,?,?,?,?,?)",
                                ("node", n.id, e.kind, e.doc_id, evidence_text(e), e.model_dump_json()))
                    con.execute("INSERT INTO evidence_fts VALUES (?,?,?)", (n.id, "node", evidence_text(e)))
                    n_ev += 1
            for e in graph.edges:
                quotes = " ".join(evidence_text(x) for x in e.evidence[:8])
                con.execute(
                    "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e.id, e.source_id, e.target_id, e.type, int(e.directed),
                     json.dumps(e.properties, ensure_ascii=False, default=str), e.confidence, e.valid_from, e.valid_to,
                     e.ingested_at.isoformat(), e.superseded_by, e.status,
                     json.dumps([x.model_dump(mode="json") for x in e.evidence], ensure_ascii=False)),
                )
                type_words = e.type.replace("_", " ").lower()
                con.execute("INSERT INTO edges_fts VALUES (?,?,?,?,?)",
                            (e.id, type_words, names.get(e.source_id, e.source_id), names.get(e.target_id, e.target_id), quotes))
                for x in e.evidence:
                    con.execute("INSERT INTO evidence (owner_kind, owner_id, kind, doc_id, quote, payload) VALUES (?,?,?,?,?,?)",
                                ("edge", e.id, x.kind, x.doc_id, evidence_text(x), x.model_dump_json()))
                    con.execute("INSERT INTO evidence_fts VALUES (?,?,?)", (e.id, "edge", evidence_text(x)))
                    n_ev += 1
            con.execute("INSERT OR REPLACE INTO meta VALUES ('graph_hash', ?)", (graph.content_hash(),))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('rebuilt_at', ?)", (utcnow_iso(),))
            con.commit()
            return {"nodes": len(graph.nodes), "edges": len(graph.edges), "evidence": n_ev}
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _search(self, table: str, id_col: str, query: str, limit: int) -> list[tuple[str, float]]:
        expr = fts_query(query)
        if not expr or not self.path.exists():
            return []
        con = self.connect()
        try:
            rows = con.execute(
                f"SELECT {id_col} AS oid, bm25({table}) AS score FROM {table} WHERE {table} MATCH ? ORDER BY score LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        out: dict[str, float] = {}
        for r in rows:
            s = -float(r["score"])
            if r["oid"] not in out or s > out[r["oid"]]:
                out[r["oid"]] = s
        return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

    def search_nodes(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        return self._search("nodes_fts", "id", query, limit)

    def search_edges(self, query: str, limit: int = 40) -> list[tuple[str, float]]:
        return self._search("edges_fts", "id", query, limit)

    def search_evidence(self, query: str, limit: int = 40) -> list[tuple[str, float]]:
        return self._search("evidence_fts", "owner_id", query, limit)

    def meta(self, key: str) -> str | None:
        if not self.path.exists():
            return None
        con = self.connect()
        try:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            con.close()

    def save_communities(self, graph_hash: str, communities: list[dict[str, Any]]) -> None:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM communities")
            con.execute("DELETE FROM community_meta")
            for c in communities:
                for nid in c["members"]:
                    con.execute("INSERT INTO communities VALUES (?,?,?)", (graph_hash, c["community_id"], nid))
                con.execute("INSERT INTO community_meta VALUES (?,?,?,?)",
                            (graph_hash, c["community_id"], len(c["members"]), json.dumps(c, ensure_ascii=False)))
            con.commit()
        finally:
            con.close()

    def load_communities(self, graph_hash: str) -> list[dict[str, Any]] | None:
        if not self.path.exists():
            return None
        con = self.connect()
        try:
            rows = con.execute("SELECT payload FROM community_meta WHERE graph_hash = ? ORDER BY community_id",
                               (graph_hash,)).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        return [json.loads(r["payload"]) for r in rows]

    def counts(self) -> dict[str, int]:
        if not self.path.exists():
            return {"nodes": 0, "edges": 0, "evidence": 0}
        con = self.connect()
        try:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("nodes", "edges", "evidence")}
        finally:
            con.close()


def upsert_candidates(ws: Workspace, new: Iterable[Candidate], replace_extractors: Iterable[str] = ()) -> dict[str, int]:
    """Merge candidates into ``candidates.jsonl`` (dedup by id); optionally drop a previous extractor's output."""
    replace = set(replace_extractors)
    existing = [c for c in ws.load_candidates() if c.extractor not in replace]
    by_id: dict[str, Candidate] = {c.candidate_id: c for c in existing}
    added = 0
    for c in new:
        c.finalize()
        if c.candidate_id not in by_id:
            added += 1
        by_id[c.candidate_id] = c
    rows = sorted(by_id.values(), key=lambda c: c.candidate_id)
    write_jsonl(ws.candidates_path, rows)
    return {"added": added, "total": len(rows), "replaced_extractors": len(replace)}


def commit_graph(ws: Workspace, graph: Graph) -> dict[str, int]:
    """Persist ``graph.json`` atomically and rebuild ``graph.sqlite`` in one SQLite transaction."""
    ws.save_graph(graph)
    return GraphStore(ws.sqlite_path).rebuild(graph)

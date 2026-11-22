"""Adapter foundation: deterministic sanitizer + symbol map, provenance projection, bundle manifest, structural
syntax validators, parity verification and the ``run_export`` entrypoint shared by the CLI, Studio and MCP.

Every adapter supports:
  Mode A  ``export_bundle``  deterministic files under ``.kumokg/exports/<target>/`` + ``symbol_map.json`` +
                             ``README_IMPORT.md`` + ``export_manifest.json`` (SHA-256 checksums + expected counts)
  Mode B  ``push_live``      lazily imports its driver, pushes in batches and runs a native count-parity query
  Parity  ``verify_parity``  re-parses the exported files and asserts node/edge parity, zero dangling endpoints and
                             valid syntax (checked against the manifest *and* a fresh projection of ``graph.json``)
"""
from __future__ import annotations

import csv
import importlib
import io
import json
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.ir import Graph, Workspace, evidence_key, evidence_text, read_json, sha256_file, sha256_text, utcnow_iso, write_json

REGISTRY_PATH = Path(__file__).with_name("registry.json")
PROVENANCE_MODES = ("props", "nodes", "reified")
EVIDENCE_LABEL = "KumoEvidence"
STATEMENT_LABEL = "KumoStatement"
SYS_HAS_EVIDENCE = "KUMO_HAS_EVIDENCE"
SYS_SUBJECT = "KUMO_SUBJECT"
SYS_OBJECT = "KUMO_OBJECT"
SYS_SUPPORTED_BY = "KUMO_SUPPORTED_BY"


class AdapterError(RuntimeError):
    pass


class DriverMissing(AdapterError):
    pass


def load_registry() -> list[dict[str, Any]]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["adapters"]


def registry_entry(name: str) -> dict[str, Any]:
    for a in load_registry():
        if a["name"] == name:
            return a
    raise AdapterError(f"unknown export target {name!r}; choose one of {[a['name'] for a in load_registry()]}")


def get_adapter(name: str, ws: Workspace, options: dict[str, Any] | None = None) -> "BaseAdapter":
    entry = registry_entry(name)
    mod = importlib.import_module(f"adapters.{entry['module']}")
    return getattr(mod, entry["class"])(ws, options or {})


def require_driver(module: str, extra: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise DriverMissing(f"live push needs the '{module}' driver: re-run with `uv run --project <skill_dir> --extra {extra} "
                            f"python <skill_dir>/scripts/kumokg_cli.py export --target ... --push`") from exc


# --------------------------------------------------------------------------------------------
# Sanitizer
# --------------------------------------------------------------------------------------------


class Sanitizer:
    """Deterministic, collision-free identifier mapping; every rename is recorded in ``symbol_map.json``."""

    ARANGO_KEY = re.compile(r"[^A-Za-z0-9_\-:.@()+,=;$!*'%]")

    def __init__(self, id_style: str = "keep", reserved: Iterable[str] = (), reserved_suffix: str = "_k"):
        self.id_style = id_style
        self.reserved = {r.upper() for r in reserved}
        self.reserved_suffix = reserved_suffix
        self.maps: dict[str, dict[str, str]] = {"labels": {}, "types": {}, "properties": {}, "node_ids": {}, "edge_ids": {}}
        self._used: dict[str, set[str]] = {k: set() for k in self.maps}

    def _assign(self, space: str, original: str, candidate: str) -> str:
        m = self.maps[space]
        if original in m:
            return m[original]
        if candidate.upper() in self.reserved:
            candidate = candidate + self.reserved_suffix
        base, i = candidate, 2
        while candidate in self._used[space]:
            candidate = f"{base}_{i}"
            i += 1
        m[original] = candidate
        self._used[space].add(candidate)
        return candidate

    def label(self, name: str) -> str:
        parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
        cand = "".join(p[:1].upper() + p[1:] for p in parts) or "Entity"
        if cand[0].isdigit():
            cand = "L" + cand
        return self._assign("labels", name, cand)

    def rel_type(self, name: str) -> str:
        cand = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "RELATED_TO"
        if cand[0].isdigit():
            cand = "R_" + cand
        return self._assign("types", name, cand)

    def prop(self, name: str) -> str:
        cand = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
        cand = re.sub(r"[^A-Za-z0-9]+", "_", cand).strip("_").lower() or "value"
        if cand[0].isdigit():
            cand = "p_" + cand
        return self._assign("properties", name, cand)

    def _id(self, space: str, original: str) -> str:
        if self.id_style == "keep":
            cand = original
        elif self.id_style == "arango_key":
            cand = self.ARANGO_KEY.sub("_", original)[:200] or "k"
            if cand != original:
                cand = f"{cand}~{sha256_text(original)[:6]}".replace("~", "_")
        elif self.id_style == "alnum":
            cand = re.sub(r"[^A-Za-z0-9_]", "_", original)[:200]
            if cand != original:
                cand = f"{cand}_{sha256_text(original)[:6]}"
        else:
            raise ValueError(self.id_style)
        m = self.maps[space]
        if original in m:
            return m[original]
        base, i = cand, 2
        while cand in self._used[space]:
            cand = f"{base}_{i}"
            i += 1
        m[original] = cand
        self._used[space].add(cand)
        return cand

    def node_id(self, original: str) -> str:
        return self._id("node_ids", original)

    def edge_id(self, original: str) -> str:
        return self._id("edge_ids", original)

    def symbol_map(self) -> dict[str, Any]:
        return {space: {k: v for k, v in sorted(m.items()) if k != v} for space, m in self.maps.items()} | {
            "id_style": self.id_style, "identity_mappings_omitted": True}


# --------------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------------


def _flat(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def evidence_locator(ev: Any) -> str:
    k = ev.kind
    if k == "text":
        return f"{ev.chunk_id}" + (f"#p{ev.page_no}" if ev.page_no else "") + (f"@{ev.char_start}-{ev.char_end}" if ev.char_start is not None else "")
    if k == "table":
        return f"{ev.sheet_or_table}[{ev.row_index}].{ev.column_name}"
    if k == "json":
        return ev.json_pointer
    if k == "notebook":
        return f"cell{ev.cell_index}:L{ev.line_span[0]}-{ev.line_span[1]}"
    if k == "image":
        return ev.image_path + (f"#bbox={','.join(str(x) for x in ev.bbox_norm)}" if ev.bbox_norm else "")
    return ev.url


@dataclass
class PNode:
    id: str
    kumo_id: str
    label: str
    labels: list[str]
    props: dict[str, Any]
    system: bool = False


@dataclass
class PEdge:
    id: str
    kumo_id: str
    source: str
    target: str
    type: str
    props: dict[str, Any]
    system: bool = False
    source_label: str = ""
    target_label: str = ""


@dataclass
class Projection:
    nodes: list[PNode]
    edges: list[PEdge]
    provenance_mode: str
    sanitizer: Sanitizer
    graph_hash: str
    evidence_rows: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "domain_nodes": sum(1 for n in self.nodes if not n.system),
                "domain_edges": sum(1 for e in self.edges if not e.system),
                "evidence": len(self.evidence_rows)}

    def labels(self) -> list[str]:
        return sorted({n.label for n in self.nodes})

    def types(self) -> list[str]:
        return sorted({e.type for e in self.edges})

    def node_by_id(self) -> dict[str, PNode]:
        return {n.id: n for n in self.nodes}


def project(graph: Graph, mode: str, sanitizer: Sanitizer) -> Projection:
    if mode not in PROVENANCE_MODES:
        raise AdapterError(f"provenance mode must be one of {PROVENANCE_MODES}")
    graph = Graph.model_validate(json.loads(graph.model_dump_json())).sort()
    s = sanitizer
    sys_keys = ["kumo_id", "name", "aliases", "labels", "confidence", "created_at"]
    edge_sys = ["kumo_id", "confidence", "valid_from", "valid_to", "ingested_at", "status", "superseded_by", "directed"]
    for k in sys_keys + edge_sys + ["evidence_count", "evidence_json", "primary_quote", "primary_source", "evidence_ids",
                                    "statement_id", "kind", "doc_id", "quote", "locator", "payload", "statement_type",
                                    "edge_kumo_id"]:
        s.prop(k)
    nodes: list[PNode] = []
    edges: list[PEdge] = []
    ev_nodes: dict[str, PNode] = {}
    evidence_rows: list[dict[str, Any]] = []
    sys_edges: list[PEdge] = []

    def ev_node(ev: Any) -> PNode:
        key = evidence_key(ev)
        if key not in ev_nodes:
            kid = f"ev:{key}"
            txt = evidence_text(ev)
            ev_nodes[key] = PNode(id=s.node_id(kid), kumo_id=kid, label=s.label(EVIDENCE_LABEL), labels=[s.label(EVIDENCE_LABEL)], system=True,
                                  props={s.prop("kumo_id"): kid, s.prop("name"): f"{ev.kind}: {txt[:60]}", s.prop("kind"): ev.kind,
                                         s.prop("doc_id"): ev.doc_id, s.prop("quote"): txt[:2000], s.prop("locator"): evidence_locator(ev),
                                         s.prop("payload"): json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)})
        return ev_nodes[key]

    def provenance(props: dict[str, Any], evidence: list) -> None:
        props[s.prop("evidence_count")] = len(evidence)
        if mode == "props":
            props[s.prop("evidence_json")] = json.dumps([e.model_dump(mode="json") for e in evidence], ensure_ascii=False, sort_keys=True)
            props[s.prop("primary_quote")] = evidence_text(evidence[0])[:500] if evidence else None
            props[s.prop("primary_source")] = evidence[0].doc_id if evidence else None

    label_of: dict[str, str] = {}
    for n in graph.nodes:
        nid = s.node_id(n.id)
        label = s.label(n.label)
        label_of[n.id] = label
        props: dict[str, Any] = {s.prop("kumo_id"): n.id, s.prop("name"): n.canonical_name,
                                 s.prop("aliases"): json.dumps(sorted(n.aliases), ensure_ascii=False),
                                 s.prop("labels"): ";".join(n.labels), s.prop("confidence"): float(n.confidence),
                                 s.prop("created_at"): n.created_at.isoformat()}
        for k, v in sorted(n.properties.items()):
            key = s.prop(f"p_{k}" if s.prop(k) in props else k)
            props[key] = _flat(v)
        provenance(props, n.evidence)
        nodes.append(PNode(id=nid, kumo_id=n.id, label=label, labels=[s.label(x) for x in n.labels], props=props))
        for ev in n.evidence:
            evidence_rows.append({"id": "evr-" + sha256_text(f"{n.id}|{evidence_key(ev)}")[:16], "owner_kind": "node", "owner_id": n.id,
                                  "kind": ev.kind, "doc_id": ev.doc_id, "quote": evidence_text(ev), "locator": evidence_locator(ev),
                                  "payload": json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)})
            if mode in ("nodes", "reified"):
                en = ev_node(ev)
                kid = f"hev:{n.id}|{en.kumo_id}"
                sys_edges.append(PEdge(id=s.edge_id("x-" + sha256_text(kid)[:16]), kumo_id=kid, source=nid, target=en.id,
                                       type=s.rel_type(SYS_HAS_EVIDENCE), props={s.prop("kumo_id"): kid}, system=True,
                                       source_label=label, target_label=en.label))
    node_ids = {n.id for n in graph.nodes}
    for e in graph.edges:
        if e.status not in ("approved", "superseded") or e.source_id not in node_ids or e.target_id not in node_ids:
            continue
        props = {s.prop("kumo_id"): e.id, s.prop("confidence"): float(e.confidence), s.prop("valid_from"): e.valid_from,
                 s.prop("valid_to"): e.valid_to, s.prop("ingested_at"): e.ingested_at.isoformat(), s.prop("status"): e.status,
                 s.prop("superseded_by"): e.superseded_by, s.prop("directed"): bool(e.directed)}
        for k, v in sorted(e.properties.items()):
            key = s.prop(f"p_{k}" if s.prop(k) in props else k)
            props[key] = _flat(v)
        provenance(props, e.evidence)
        for ev in e.evidence:
            evidence_rows.append({"id": "evr-" + sha256_text(f"{e.id}|{evidence_key(ev)}")[:16], "owner_kind": "edge", "owner_id": e.id,
                                  "kind": ev.kind, "doc_id": ev.doc_id, "quote": evidence_text(ev), "locator": evidence_locator(ev),
                                  "payload": json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)})
        src, tgt = s.node_id(e.source_id), s.node_id(e.target_id)
        if mode in ("nodes", "reified"):
            props[s.prop("evidence_ids")] = json.dumps([ev_node(ev).kumo_id for ev in e.evidence])
        if mode == "reified":
            st_kid = f"st:{e.id}"
            props[s.prop("statement_id")] = st_kid
            st = PNode(id=s.node_id(st_kid), kumo_id=st_kid, label=s.label(STATEMENT_LABEL), labels=[s.label(STATEMENT_LABEL)], system=True,
                       props={s.prop("kumo_id"): st_kid, s.prop("name"): f"{e.source_id} {e.type} {e.target_id}",
                              s.prop("statement_type"): e.type, s.prop("edge_kumo_id"): e.id, s.prop("valid_from"): e.valid_from,
                              s.prop("valid_to"): e.valid_to, s.prop("status"): e.status, s.prop("confidence"): float(e.confidence)})
            nodes.append(st)
            for t, other, olabel in ((SYS_SUBJECT, src, label_of[e.source_id]), (SYS_OBJECT, tgt, label_of[e.target_id])):
                kid = f"{t.lower()}:{e.id}"
                sys_edges.append(PEdge(id=s.edge_id("x-" + sha256_text(kid)[:16]), kumo_id=kid, source=st.id, target=other,
                                       type=s.rel_type(t), props={s.prop("kumo_id"): kid}, system=True,
                                       source_label=st.label, target_label=olabel))
            for ev in e.evidence:
                en = ev_node(ev)
                kid = f"sup:{e.id}|{en.kumo_id}"
                sys_edges.append(PEdge(id=s.edge_id("x-" + sha256_text(kid)[:16]), kumo_id=kid, source=st.id, target=en.id,
                                       type=s.rel_type(SYS_SUPPORTED_BY), props={s.prop("kumo_id"): kid}, system=True,
                                       source_label=st.label, target_label=en.label))
        edges.append(PEdge(id=s.edge_id(e.id), kumo_id=e.id, source=src, target=tgt, type=s.rel_type(e.type), props=props,
                           source_label=label_of[e.source_id], target_label=label_of[e.target_id]))
    nodes.extend(ev_nodes[k] for k in sorted(ev_nodes))
    edges.extend(sorted(sys_edges, key=lambda x: x.kumo_id))
    nodes.sort(key=lambda n: (n.system, n.label, n.kumo_id))
    edges.sort(key=lambda e: (e.system, e.type, e.kumo_id))
    return Projection(nodes=nodes, edges=edges, provenance_mode=mode, sanitizer=s, graph_hash=graph.content_hash(),
                      evidence_rows=sorted(evidence_rows, key=lambda r: r["id"]))


# --------------------------------------------------------------------------------------------
# Relational rows (PostgreSQL, DuckDB)
# --------------------------------------------------------------------------------------------


def relational_rows(graph: Graph, mode: str) -> dict[str, list[dict[str, Any]]]:
    """Domain nodes, committed edges (incl. superseded history) and normalised evidence rows."""
    graph = Graph.model_validate(json.loads(graph.model_dump_json())).sort()
    node_ids = {n.id for n in graph.nodes}
    dump = lambda evs: json.dumps([e.model_dump(mode="json") for e in evs], ensure_ascii=False, sort_keys=True)  # noqa: E731
    nodes = [{"id": n.id, "label": n.label, "labels": list(n.labels), "name": n.canonical_name,
              "aliases": json.dumps(sorted(n.aliases), ensure_ascii=False),
              "properties": json.dumps(n.properties, ensure_ascii=False, sort_keys=True, default=str),
              "confidence": float(n.confidence), "created_at": n.created_at.isoformat(),
              **({"evidence": dump(n.evidence)} if mode == "props" else {})} for n in graph.nodes]
    edges, evidence = [], []
    for e in graph.edges:
        if e.status not in ("approved", "superseded") or e.source_id not in node_ids or e.target_id not in node_ids:
            continue
        edges.append({"id": e.id, "source_id": e.source_id, "target_id": e.target_id, "type": e.type, "directed": bool(e.directed),
                      "properties": json.dumps(e.properties, ensure_ascii=False, sort_keys=True, default=str),
                      "confidence": float(e.confidence), "valid_from": e.valid_from, "valid_to": e.valid_to,
                      "ingested_at": e.ingested_at.isoformat(), "superseded_by": e.superseded_by, "status": e.status,
                      **({"evidence": dump(e.evidence)} if mode == "props" else {})})
    for kind, items in (("node", graph.nodes), ("edge", [x for x in graph.edges if x.id in {r["id"] for r in edges}])):
        for it in items:
            for ev in it.evidence:
                evidence.append({"id": "evr-" + sha256_text(f"{it.id}|{evidence_key(ev)}")[:16], "owner_kind": kind, "owner_id": it.id,
                                 "kind": ev.kind, "doc_id": ev.doc_id, "quote": evidence_text(ev), "locator": evidence_locator(ev),
                                 "payload": json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)})
    evidence.sort(key=lambda r: r["id"])
    return {"nodes": nodes, "edges": edges, "evidence": evidence}


# --------------------------------------------------------------------------------------------
# Typed columns & CSV helpers
# --------------------------------------------------------------------------------------------


def infer_type(values: Iterable[Any]) -> str:
    vals = [v for v in values if v is not None and v != ""]
    if not vals:
        return "string"
    if all(isinstance(v, bool) for v in vals):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return "int"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "float"
    return "string"


def columns_for(items: list[Any], first: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Stable column order (``first`` then the remaining property keys sorted) with inferred types."""
    head = list(dict.fromkeys(first))
    rest = sorted({k for it in items for k in it.props} - set(head))
    return [(k, infer_type(it.props.get(k) for it in items)) for k in head + rest]


def cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([cell(v) for v in r])
    path.write_text(buf.getvalue(), encoding="utf-8", newline="")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        rows = list(r)
        return list(r.fieldnames or []), rows


def group_by(items: Iterable[Any], key) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for it in items:
        out.setdefault(key(it), []).append(it)
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------------------------
# Structural syntax validators
# --------------------------------------------------------------------------------------------


def split_statements(text: str, lang: str) -> list[str]:
    """Split on top-level ``;`` honouring quotes, backticks, dollar-quotes and comments. Raises on imbalance."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {v: k for k, v in pairs.items()}
    line_comments = {"sql": ("--",), "cypher": ("//",), "groovy": ("//",), "gsql": ("#", "//"), "shell": ("#",)}[lang]
    while i < n:
        ch = text[i]
        if any(text.startswith(c, i) for c in line_comments):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if lang in ("cypher", "groovy", "gsql", "sql") and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise SyntaxError("unterminated block comment")
            i = j + 2
            continue
        if lang == "sql" and ch == "$":
            m = re.match(r"\$([A-Za-z_]\w*)?\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                if j == -1:
                    raise SyntaxError(f"unterminated dollar-quoted string {tag}")
                buf.append(text[i:j + len(tag)])
                i = j + len(tag)
                continue
        if ch in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if text[j] == "\\" and lang != "sql":
                    j += 2
                    continue
                if text[j] == ch:
                    if lang == "sql" and ch == "'" and j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise SyntaxError(f"unterminated {ch} quote near: {text[i:i + 60]!r}")
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if ch in pairs:
            stack.append(ch)
        elif ch in closers:
            if not stack or stack[-1] != closers[ch]:
                raise SyntaxError(f"unbalanced {ch!r} near: {text[max(0, i - 60):i + 1]!r}")
            stack.pop()
        if ch == ";" and not stack:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if stack:
        raise SyntaxError(f"unclosed {stack[-1]!r}")
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


CYPHER_START = ("CREATE", "MERGE", "MATCH", "LOAD", "UNWIND", "CALL", "WITH", "RETURN", "OPTIONAL", "DROP", "SHOW", "USING", ":AUTO")
SQL_START = ("CREATE", "ALTER", "SELECT", "SET", "LOAD", "INSERT", "\\COPY", "COPY", "BEGIN", "COMMIT", "COMMENT", "DROP",
             "INSTALL", "WITH", "ANALYZE", "VACUUM", "FROM", "DO", "TRUNCATE")


def validate_statements(text: str, lang: str, starts: tuple[str, ...]) -> tuple[bool, str, int]:
    try:
        stmts = split_statements(text, lang)
    except SyntaxError as exc:
        return False, str(exc), 0
    for st in stmts:
        head = st.lstrip().split(None, 1)[0].upper() if st.strip() else ""
        if not head.startswith(starts):
            return False, f"unexpected statement start {head!r}: {st[:80]!r}", len(stmts)
    return True, "ok", len(stmts)


def validate_cypher(text: str) -> tuple[bool, str, int]:
    return validate_statements(text, "cypher", CYPHER_START)


def validate_sql(text: str) -> tuple[bool, str, int]:
    return validate_statements(text, "sql", SQL_START)


def validate_balanced(text: str, lang: str) -> tuple[bool, str, int]:
    try:
        return True, "ok", len(split_statements(text, lang))
    except SyntaxError as exc:
        return False, str(exc), 0


def cypher_str(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return f"'{s}'"


def cypher_map(props: dict[str, Any]) -> str:
    return "{" + ", ".join(f"`{k}`: {cypher_str(v)}" for k, v in props.items() if v is not None) + "}"


# --------------------------------------------------------------------------------------------
# Base adapter
# --------------------------------------------------------------------------------------------


@dataclass
class Parsed:
    node_ids: list[str]
    edges: list[tuple[str, str, str]]
    syntax: dict[str, dict[str, Any]]
    extra: dict[str, Any] = field(default_factory=dict)


class BaseAdapter(ABC):
    name = "base"
    title = ""
    extra = ""
    id_style = "keep"
    reserved: tuple[str, ...] = ()
    reserved_suffix = "_k"
    env_vars: tuple[str, ...] = ()

    def __init__(self, ws: Workspace, options: dict[str, Any] | None = None):
        self.ws = ws
        self.options = dict(options or {})

    @property
    def out_dir(self) -> Path:
        return self.ws.exports_dir / self.name

    def mode_suffix(self) -> str:
        return ""

    def make_projection(self, graph: Graph | None = None, provenance_mode: str = "props") -> Projection:
        graph = graph or self.ws.load_graph()
        return project(graph, provenance_mode, Sanitizer(self.id_style, self.reserved, self.reserved_suffix))

    # Mode A ------------------------------------------------------------------------------
    @abstractmethod
    def write_files(self, proj: Projection, out: Path) -> None: ...

    @abstractmethod
    def readme(self, proj: Projection) -> str: ...

    @abstractmethod
    def parse_bundle(self, out: Path) -> Parsed: ...

    def expected_counts(self, proj: Projection) -> dict[str, int]:
        return {"nodes": len(proj.nodes), "edges": len(proj.edges)}

    def export_bundle(self, graph: Graph | None = None, provenance_mode: str = "props") -> dict[str, Any]:
        self.ws.require()
        proj = self.make_projection(graph, provenance_mode)
        out = self.out_dir
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        self.write_files(proj, out)
        write_json(out / "symbol_map.json", proj.sanitizer.symbol_map())
        (out / "README_IMPORT.md").write_text(self.readme(proj), encoding="utf-8", newline="\n")
        files = []
        for p in sorted(out.rglob("*")):
            if p.is_file() and p.name != "export_manifest.json":
                files.append({"path": p.relative_to(out).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)})
        manifest = {"target": self.name, "title": self.title, "generated_at": utcnow_iso(), "graph_hash": proj.graph_hash,
                    "provenance_mode": provenance_mode, "options": self.options, "expected": self.expected_counts(proj),
                    "projection": proj.counts(), "files": files}
        write_json(out / "export_manifest.json", manifest)
        return {"target": self.name, "out_dir": str(out), "files": files, "expected": manifest["expected"],
                "provenance_mode": provenance_mode}

    def verify_parity(self) -> dict[str, Any]:
        out = self.out_dir
        manifest = read_json(out / "export_manifest.json")
        errors: list[str] = []
        if manifest is None:
            return {"ok": False, "target": self.name, "errors": ["no export_manifest.json: run export first"],
                    "nodes_expected": 0, "nodes_found": 0, "edges_expected": 0, "edges_found": 0, "dangling": 0}
        for f in manifest["files"]:
            p = out / f["path"]
            if not p.exists():
                errors.append(f"missing file {f['path']}")
            elif sha256_file(p) != f["sha256"]:
                errors.append(f"checksum mismatch {f['path']}")
        self.options = manifest.get("options", {}) or self.options
        parsed = self.parse_bundle(out)
        fresh = self.expected_counts(self.make_projection(None, manifest["provenance_mode"]))
        exp = manifest["expected"]
        if fresh != exp:
            errors.append(f"graph.json changed since export (expected now {fresh}, manifest {exp})")
        node_set = set(parsed.node_ids)
        if len(node_set) != len(parsed.node_ids):
            errors.append(f"{len(parsed.node_ids) - len(node_set)} duplicate node ids")
        dangling = [e for e in parsed.edges if e[1] not in node_set or e[2] not in node_set]
        if len(parsed.node_ids) != exp["nodes"]:
            errors.append(f"node count {len(parsed.node_ids)} != expected {exp['nodes']}")
        if len(parsed.edges) != exp["edges"]:
            errors.append(f"edge count {len(parsed.edges)} != expected {exp['edges']}")
        if dangling:
            errors.append(f"{len(dangling)} dangling edge endpoints (e.g. {dangling[0]})")
        for fname, res in parsed.syntax.items():
            if not res.get("ok"):
                errors.append(f"syntax error in {fname}: {res.get('detail')}")
        return {"ok": not errors, "target": self.name, "provenance_mode": manifest["provenance_mode"],
                "nodes_expected": exp["nodes"], "nodes_found": len(parsed.node_ids),
                "edges_expected": exp["edges"], "edges_found": len(parsed.edges), "dangling": len(dangling),
                "syntax": parsed.syntax, "checksums_verified": len(manifest["files"]), "errors": errors, **parsed.extra}

    # Mode B ------------------------------------------------------------------------------
    def env(self) -> dict[str, str]:
        from dotenv import dotenv_values
        import os

        file_env = {k: v for k, v in (dotenv_values(self.ws.env_path) if self.ws.env_path.exists() else {}).items() if v is not None}
        return {**file_env, **{k: v for k, v in os.environ.items() if k.startswith("KUMOKG_")}}

    def require_env(self, env: dict[str, str], *keys: str) -> list[str]:
        missing = [k for k in keys if not env.get(k)]
        if missing:
            raise AdapterError(f"{self.name} live push needs {', '.join(missing)} in <workspace>/.kumokg/.env "
                               "(see assets/templates/env.example)")
        return [env[k] for k in keys]

    @abstractmethod
    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]: ...

    def push(self, provenance_mode: str = "props", client: Any = None, env: dict[str, str] | None = None) -> dict[str, Any]:
        proj = self.make_projection(None, provenance_mode)
        res = self.push_live(proj, env if env is not None else self.env(), client)
        exp = self.expected_counts(proj)
        res["expected"] = exp
        res["ok"] = res.get("nodes_in_db") == exp["nodes"] and res.get("edges_in_db") == exp["edges"]
        return res

    @staticmethod
    def batches(items: list[Any], size: int = 500) -> Iterable[list[Any]]:
        for i in range(0, len(items), size):
            yield items[i:i + size]


def run_export(ws: Workspace, target: str, provenance_mode: str = "props", options: dict[str, Any] | None = None,
               verify: bool = True, push: bool = False, client: Any = None) -> dict[str, Any]:
    ws.require()
    if not ws.graph_path.exists():
        raise FileNotFoundError("no graph.json: commit a graph before exporting")
    adapter = get_adapter(target, ws, options)
    res = adapter.export_bundle(provenance_mode=provenance_mode)
    if verify:
        res["parity"] = adapter.verify_parity()
    if push:
        res["push"] = adapter.push(provenance_mode, client=client)
    return res

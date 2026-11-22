"""Hard programmatic accuracy guardrails.

* Grounds every evidence item against its source: quotes inside chunks (exact span, normalised substring,
  fuzzy >= 0.92 => ``soft_verified``), table cells, RFC 6901 JSON pointers, notebook cells, image hashes and
  fetched web content. Ungrounded claims are quarantined.
* Validates labels and ``(source_class, predicate, target_class)`` triples against the approved ontology;
  closed-world violations become ``proposed_schema_extensions`` instead of graph facts.
* Resolves entities deterministically (normalised names, alias table, token-overlap clustering) and only flags
  genuinely ambiguous pairs.
* Groups candidates into reviewable proposals, detects cross-source contradictions on functional predicates and
  properties, and computes the ``doctor`` Graph Health Scorecard.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .ir import (
    Candidate, EntityRef, Graph, GraphStore, Ontology, Workspace, evidence_key, evidence_text, make_node_id,
    normalize_name, normalize_text, read_json, sha256_file, sha256_text, stable_json, utcnow_iso, write_json,
)
from .modality_router import (
    cell_source, extract_pages, load_notebook, load_semistructured, parse_markdown_note, read_table, resolve_pointer,
    split_text,
)

FUZZY_THRESHOLD = 0.92
GROUNDED = ("verified", "soft_verified", "needs_visual")
STATUS_RANK = {"verified": 3, "needs_visual": 2, "soft_verified": 1, "rejected": 0}
PRONOUNS = {"he", "she", "it", "they", "them", "him", "her", "i", "me", "we", "us", "you", "this", "that", "who",
            "someone", "something", "everyone", "everybody", "nobody", "one"}
_NAME_STOP = {"of", "the", "a", "an", "and", "de", "von", "la", "le"}


# --------------------------------------------------------------------------------------------
# Evidence grounding
# --------------------------------------------------------------------------------------------


def fuzzy_ratio(hay: str, needle: str) -> float:
    if not needle or not hay:
        return 0.0
    if len(needle) >= len(hay):
        return SequenceMatcher(None, hay, needle, autojunk=False).ratio()
    sm = SequenceMatcher(None, hay, needle, autojunk=False)
    blocks = sorted((b for b in sm.get_matching_blocks() if b.size >= 3), key=lambda b: -b.size)[:12]
    L = len(needle)
    best = 0.0
    for a, b, _ in blocks:
        start = a - b
        for shift in (0, -max(1, L // 10), max(1, L // 10)):
            s = max(0, start + shift)
            window = hay[s:s + L]
            if window:
                best = max(best, SequenceMatcher(None, window, needle, autojunk=False).ratio())
    return round(best, 4)


def _same_value(a: Any, b: Any) -> bool:
    if a == b:
        return True
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return stable_json(a) == stable_json(b)
    sa, sb = str(a).strip(), str(b).strip()
    if sa == sb:
        return True
    try:
        return float(sa) == float(sb)
    except ValueError:
        return sa.lower() in ("true", "false") and sa.lower() == sb.lower()


class VerifyContext:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.chunks = ws.load_chunks()
        self.manifest = ws.load_manifest() if ws.exists() else None
        self._tables: dict[tuple[str, str], list[dict]] = {}
        self._semi: dict[str, Any] = {}
        self._nb: dict[str, dict] = {}
        self._web: dict[str, str] = {}
        self._sha: dict[str, str] = {}

    def source(self, doc_id: str):
        return self.manifest.source_by_id(doc_id) if self.manifest else None

    def current_sha(self, doc_id: str) -> str:
        if doc_id not in self._sha:
            rec = self.source(doc_id)
            self._sha[doc_id] = sha256_file(rec.path) if rec and Path(rec.path).exists() else ""
        return self._sha[doc_id]

    def table_rows(self, doc_id: str, table: str) -> list[dict]:
        key = (doc_id, table)
        if key not in self._tables:
            rec = self.source(doc_id)
            path = Path(rec.path)
            if path.suffix.lower() in (".csv", ".tsv", ".parquet") and table != path.stem:
                raise KeyError(f"unknown table {table!r}")
            self._tables[key] = read_table(path, table if path.suffix.lower() not in (".csv", ".tsv", ".parquet") else None)[1]
        return self._tables[key]

    def semi_doc(self, doc_id: str) -> Any:
        if doc_id not in self._semi:
            rec = self.source(doc_id)
            path = Path(rec.path)
            self._semi[doc_id] = parse_markdown_note(path)["frontmatter"] if path.suffix.lower() in (".md", ".markdown") \
                else load_semistructured(path)
        return self._semi[doc_id]

    def notebook(self, doc_id: str) -> dict:
        if doc_id not in self._nb:
            self._nb[doc_id] = load_notebook(Path(self.source(doc_id).path))
        return self._nb[doc_id]

    def web_text(self, doc_id: str) -> str:
        if doc_id not in self._web:
            self._web[doc_id] = Path(self.source(doc_id).path).read_text(encoding="utf-8")
        return self._web[doc_id]

    def media_path(self, image_path: str) -> Path | None:
        p = Path(image_path)
        media = self.ws.media_dir.resolve()
        cand = p if p.is_absolute() else (media / p)
        try:
            cand = cand.resolve()
        except OSError:
            return None
        if not cand.is_relative_to(media) and not (p.is_absolute() and cand.exists()):
            return None
        return cand


def _check(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def _text_ground(hay_raw: str, quote: str) -> dict[str, Any]:
    q = normalize_text(quote)
    if len(q) < 3:
        return _check("rejected", "empty or trivial quote")
    hay = normalize_text(hay_raw)
    if q in hay:
        return _check("verified", "normalised substring match")
    ratio = fuzzy_ratio(hay, q)
    if ratio >= FUZZY_THRESHOLD:
        return _check("soft_verified", f"fuzzy match ratio {ratio:.3f} >= {FUZZY_THRESHOLD}", ratio=ratio)
    return _check("rejected", f"quote not found in source (best fuzzy ratio {ratio:.2f})", ratio=ratio)


def verify_evidence(ev: Any, ctx: VerifyContext) -> dict[str, Any]:
    kind = ev.kind
    try:
        if kind == "text":
            ch = ctx.chunks.get(ev.chunk_id)
            if ch is None:
                return _check("rejected", f"unknown chunk_id {ev.chunk_id}")
            if ch.doc_id != ev.doc_id:
                return _check("rejected", "chunk belongs to a different document")
            if ev.chunk_sha256 and ev.chunk_sha256 != ch.sha256:
                return _check("rejected", "chunk hash mismatch (source changed since extraction)")
            q = normalize_text(ev.verbatim_quote)
            if len(q) < 3:
                return _check("rejected", "empty or trivial quote")
            if ev.char_start is not None and ev.char_end is not None:
                rs, re_ = ev.char_start - ch.char_start, ev.char_end - ch.char_start
                if 0 <= rs < re_ <= len(ch.text) and normalize_text(ch.text[rs:re_]) == q:
                    return _check("verified", "exact span match")
            res = _text_ground(ch.text, ev.verbatim_quote)
            if res["status"] == "verified" and ev.char_start is not None:
                res["detail"] = "normalised substring match (offsets corrected)"
            return res
        rec = ctx.source(ev.doc_id)
        if rec is None:
            return _check("rejected", f"unknown document {ev.doc_id}")
        if kind == "table":
            if ev.file_sha256 and ctx.current_sha(ev.doc_id) != ev.file_sha256:
                return _check("rejected", "source file changed since extraction")
            try:
                rows = ctx.table_rows(ev.doc_id, ev.sheet_or_table)
            except KeyError:
                return _check("rejected", f"unknown sheet/table {ev.sheet_or_table!r}")
            if not 0 <= ev.row_index < len(rows):
                return _check("rejected", f"row {ev.row_index} out of range ({len(rows)} rows)")
            if ev.column_name not in rows[ev.row_index]:
                return _check("rejected", f"unknown column {ev.column_name!r}")
            actual = rows[ev.row_index][ev.column_name]
            if _same_value(actual, ev.cell_value):
                return _check("verified", "cell value matches source")
            return _check("rejected", f"cell mismatch: source has {actual!r}")
        if kind == "json":
            if ev.file_sha256 and ctx.current_sha(ev.doc_id) != ev.file_sha256:
                return _check("rejected", "source file changed since extraction")
            try:
                actual = resolve_pointer(ctx.semi_doc(ev.doc_id), ev.json_pointer)
            except (KeyError, IndexError, TypeError) as exc:
                return _check("rejected", f"JSON pointer does not resolve ({exc})")
            if _same_value(actual, ev.raw_value):
                return _check("verified", "JSON pointer value matches source")
            return _check("rejected", f"value mismatch at pointer: source has {json.dumps(actual, default=str)[:80]}")
        if kind == "notebook":
            if ev.file_sha256 and ctx.current_sha(ev.doc_id) != ev.file_sha256:
                return _check("rejected", "notebook changed since extraction")
            cells = ctx.notebook(ev.doc_id).get("cells", [])
            if not 0 <= ev.cell_index < len(cells):
                return _check("rejected", f"cell {ev.cell_index} out of range")
            cell = cells[ev.cell_index]
            if cell.get("cell_type", "raw") != ev.cell_type:
                return _check("rejected", f"cell type mismatch (source cell is {cell.get('cell_type')})")
            src = cell_source(cell)
            lines = src.splitlines()
            a, b = ev.line_span
            span_text = "\n".join(lines[max(0, a - 1):b])
            q = normalize_text(ev.verbatim_quote)
            if q and q in normalize_text(span_text):
                return _check("verified", "quote found in cited cell lines")
            res = _text_ground(src, ev.verbatim_quote)
            if res["status"] == "verified":
                res["detail"] = "quote found in cell (line span corrected)"
            return res
        if kind == "image":
            path = ctx.media_path(ev.image_path)
            if path is None or not path.exists():
                return _check("rejected", f"image not found: {ev.image_path}")
            if ev.image_sha256 and sha256_file(path) != ev.image_sha256:
                return _check("rejected", "image hash mismatch")
            return _check("needs_visual", "image file and hash verified; awaiting visual confirmation at Gate 2")
        if kind == "web":
            text = ctx.web_text(ev.doc_id)
            if ev.content_sha256 and sha256_text(text) != ev.content_sha256:
                return _check("rejected", "fetched content changed since extraction")
            return _text_ground(text, ev.verbatim_quote)
    except FileNotFoundError as exc:
        return _check("rejected", f"source file missing ({exc})")
    return _check("rejected", f"unsupported evidence kind {kind}")


# --------------------------------------------------------------------------------------------
# Entity resolution
# --------------------------------------------------------------------------------------------


def _tokens(norm: str) -> set[str]:
    return {t for t in norm.split() if t not in _NAME_STOP}


def display_name(name: str) -> str:
    n = " ".join(name.split())
    return re.sub(r"^(?:the|a|an)\s+", "", n, flags=re.I) or n


GENERIC_LABELS = {None, "", "Note", "Entity", "Thing"}


class EntityResolver:
    def __init__(self, ont: Ontology, cal: dict[str, Any] | None = None, graph: Graph | None = None):
        self.ont = ont
        self.by_label: dict[str, dict[str, str]] = defaultdict(dict)
        self.canon_aliases: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.ambiguous: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []
        self._amb_seen: set[tuple] = set()
        if graph:
            for n in graph.nodes:
                self.register(n.label, n.canonical_name, n.aliases)
        for a in ont.alias_rules:
            label = a.entity_class or self._label_for(a.canonical)
            if label:
                self.register(label, a.canonical, a.aliases)
        for m in (cal or {}).get("alias_merges", []):
            label = m.get("label") or self._label_for(m["canonical"])
            if label:
                self.register(label, m["canonical"], [m["alias"]])
        for c in ont.entity_classes:
            for t in c.seed_terms:
                self.register(c.name, t, [])

    def _label_for(self, name: str) -> str | None:
        key = normalize_name(name)
        hits = {lbl for lbl, names in self.by_label.items() if key in names}
        if len(hits) == 1:
            return hits.pop()
        for c in self.ont.entity_classes:
            if any(normalize_name(t) == key for t in c.seed_terms):
                return c.name
        return None

    def register(self, label: str, canonical: str, aliases: Iterable[str] = ()) -> str:
        canonical = display_name(canonical)
        names = self.by_label[label]
        key = normalize_name(canonical)
        if not key:
            return canonical
        canonical = names.setdefault(key, canonical)
        for a in aliases:
            ak = normalize_name(a)
            if ak and ak not in names:
                names[ak] = canonical
                self.canon_aliases[(label, canonical)].add(a)
        return canonical

    def resolve(self, ref: EntityRef) -> dict[str, Any]:
        name = display_name(ref.name)
        key = normalize_name(name)
        label = ref.label
        if not key or key in PRONOUNS:
            return {"id": None, "label": label, "name": name, "method": "pronoun"}
        if label in GENERIC_LABELS:
            hits = sorted({(lbl, names[key]) for lbl, names in self.by_label.items() if key in names})
            if len(hits) == 1:
                lbl, canon = hits[0]
                return {"id": make_node_id(lbl, canon), "label": lbl, "name": canon, "method": "cross_label"}
            label = label or self._label_for(name) or "Entity"
        names = self.by_label[label]
        if key in names:
            canon = names[key]
            method = "exact" if normalize_name(canon) == key else "alias"
            return {"id": make_node_id(label, canon), "label": label, "name": canon, "method": method}
        toks = _tokens(key)
        matches = []
        raw = normalize_text(name)
        if toks:
            for k, canon in names.items():
                ct = _tokens(k)
                if not ct:
                    continue
                inter = len(toks & ct)
                if not inter:
                    continue
                # a shorter mention may refer to a longer canonical name ("Voss" -> "Elena Voss"), never the reverse:
                # "Alice's Sister" is a different entity from "Alice"
                if re.search(rf"(?<![a-z0-9]){re.escape(normalize_text(canon))}'s\b", raw):
                    continue
                if toks <= ct or inter / len(toks | ct) >= 0.6:
                    matches.append(canon)
        matches = sorted(set(matches))
        if len(matches) == 1:
            canon = matches[0]
            names[key] = canon
            self.merges.append({"label": label, "alias": name, "canonical": canon, "method": "token_overlap"})
            return {"id": make_node_id(label, canon), "label": label, "name": canon, "method": "token_overlap"}
        if len(matches) > 1:
            sig = (label, key)
            if sig not in self._amb_seen:
                self._amb_seen.add(sig)
                self.ambiguous.append({"label": label, "name": name, "candidates": matches})
        canon = self.register(label, name)
        return {"id": make_node_id(label, canon), "label": label, "name": canon,
                "method": "ambiguous" if len(matches) > 1 else "new", "ambiguous_with": matches if len(matches) > 1 else []}


# --------------------------------------------------------------------------------------------
# Ontology validation
# --------------------------------------------------------------------------------------------


def check_ontology(kind: str, ont: Ontology, slabel: str | None, predicate: str | None, tlabel: str | None) -> dict[str, Any]:
    classes = ont.class_names()
    ext = "schema_extension" if ont.closed_world else "open_extension"
    if kind == "node":
        if slabel in classes:
            return {"status": "ok"}
        return {"status": ext, "detail": f"unknown class {slabel}", "proposal": {"class": slabel}}
    if kind == "attribute":
        cls = ont.get_class(slabel or "")
        if cls is None:
            return {"status": ext, "detail": f"unknown class {slabel}", "proposal": {"class": slabel}}
        if predicate in cls.properties:
            return {"status": "ok"}
        return {"status": ext, "detail": f"property {predicate} not defined on {slabel}",
                "proposal": {"class": slabel, "property": predicate}}
    unknown = [c for c in (slabel, tlabel) if c not in classes]
    if unknown:
        return {"status": ext, "detail": f"unknown class {', '.join(str(u) for u in unknown)}",
                "proposal": {"source_class": slabel, "predicate": predicate, "target_class": tlabel}}
    if ont.allows(slabel, predicate, tlabel):
        return {"status": "ok"}
    if ont.allows(tlabel, predicate, slabel):
        return {"status": "flip_suggested", "detail": f"{predicate} is defined {tlabel} -> {slabel}; direction looks reversed"}
    return {"status": ext, "detail": f"{slabel} -[{predicate}]-> {tlabel} is not in the relation grammar",
            "proposal": {"source_class": slabel, "predicate": predicate, "target_class": tlabel}}


# --------------------------------------------------------------------------------------------
# Contradictions
# --------------------------------------------------------------------------------------------


def _overlap(a_from, a_to, b_from, b_to) -> bool:
    lo = "\x00"
    hi = "\U0010ffff"
    return (a_from or lo) < (b_to or hi) and (b_from or lo) < (a_to or hi)


def committed_edge_for(ref: str, source_id: str, predicate: str, target_id: str | None,
                       by_ref: dict[str, Any], by_triple: dict[tuple, list]) -> Any:
    """The committed edge a proposal became: linked by proposal id, else the same triple (e.g. added by a patch)."""
    e = by_ref.get(ref)
    if e is None and target_id:
        same = by_triple.get((source_id, predicate, target_id), [])
        e = next((x for x in same if x.status == "approved" and not (x.properties or {}).get("retracted")),
                 same[0] if same else None)
    return e


def index_edges(edges: Iterable[Any]) -> tuple[dict[str, Any], dict[tuple, list]]:
    by_ref: dict[str, Any] = {}
    by_triple: dict[tuple, list] = defaultdict(list)
    for e in edges:
        pid = (e.properties or {}).get("proposal_id")
        if pid:
            by_ref.setdefault(pid, e)
        by_triple[(e.source_id, e.type, e.target_id)].append(e)
    return by_ref, by_triple


def contradiction_resolved(c: dict[str, Any], decisions: dict[str, Any], edges: dict[str, Any]) -> bool:
    """A contradiction is resolved once at most one of its values is still live (approved or undecided).

    `edges` is the graph's edge map; each value is matched to its committed edge by proposal id or triple, so a value
    whose edge was superseded (bi-temporal sequence) or retracted no longer counts."""
    by_ref, by_triple = index_edges(edges.values())
    live = 0
    for v in c["values"]:
        d = (decisions.get(v["ref"]) or {}).get("decision")
        if d in ("reject", "resolved"):
            continue
        e = committed_edge_for(v["ref"], c["source_id"], c["predicate"], v.get("target_id"), by_ref, by_triple)
        if e is not None and (e.status != "approved" or (e.properties or {}).get("retracted")):
            continue
        live += 1
    return live <= 1


def functional_conflicts(assertions: list[dict[str, Any]], ont: Ontology) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in assertions:
        if ont.is_functional(a["predicate"], a.get("source_label")):
            groups[(a["source_id"], a["predicate"])].append(a)
    out = []
    for (sid, pred), items in sorted(groups.items()):
        conflicting: dict[str, dict] = {}
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a["target_id"] != b["target_id"] and _overlap(a.get("valid_from"), a.get("valid_to"),
                                                                b.get("valid_from"), b.get("valid_to")):
                    conflicting[a["ref"]] = a
                    conflicting[b["ref"]] = b
        if conflicting:
            vals = sorted(conflicting.values(), key=lambda x: (x["target_id"], x["ref"]))
            scopes = {v["scope"] for v in vals}
            out.append({
                "contradiction_id": "x-" + sha256_text(f"{sid}|{pred}|" + "|".join(v["ref"] for v in vals))[:12],
                "kind": "functional_relation", "source_id": sid, "source_name": vals[0].get("source_name", sid),
                "predicate": pred, "scope": scopes.pop() if len(scopes) == 1 else "mixed",
                "values": [{k: v.get(k) for k in ("ref", "target_id", "target_name", "valid_from", "valid_to", "docs",
                                                   "modalities", "scope")} for v in vals],
                "modalities": sorted({m for v in vals for m in v.get("modalities", [])}),
            })
    return out


# --------------------------------------------------------------------------------------------
# Verification run
# --------------------------------------------------------------------------------------------


def _bucket_order(b: str) -> int:
    return {"conflict": 0, "review": 1, "auto": 2, "quarantine": 3}[b]


def verify(ws: Workspace, threshold: float | None = None) -> dict[str, Any]:
    ws.require()
    ont = ws.require_ontology()
    cal = ws.load_calibration()
    graph = ws.load_graph()
    threshold = float(threshold if threshold is not None else cal["thresholds"].get("auto_approve", 0.75))
    ctx = VerifyContext(ws)
    cands = ws.load_candidates()
    priority = {"structured": 0, "harness": 1, "byok": 2, "heuristic": 3}
    cands.sort(key=lambda c: (priority.get(c.extractor, 9), 0 if c.kind == "node" else 1, c.candidate_id))
    resolver = EntityResolver(ont, cal, graph)
    for c in cands:
        if c.kind == "node" and c.extractor == "structured" and c.subject.label:
            resolver.register(c.subject.label, c.subject.name, c.aliases)
    pred_fix = {(p["from"], p.get("source_class"), p.get("target_class")): p["to"] for p in cal["predicate_corrections"]}
    flips = {(d["predicate"], d["source_class"], d["target_class"]) for d in cal["direction_corrections"]}
    negatives = {(normalize_name(n["subject"]), n["predicate"], normalize_name(n["object"])) for n in cal["negative_examples"]}

    results: dict[str, dict[str, Any]] = {}
    edge_groups: dict[str, dict[str, Any]] = {}
    node_groups: dict[str, dict[str, Any]] = {}
    attr_claims: list[dict[str, Any]] = []
    ext_index: dict[tuple, dict[str, Any]] = {}
    counts: Counter = Counter()
    for c in cands:
        checks = [verify_evidence(ev, ctx) for ev in c.evidence]
        for ch in checks:
            counts[ch["status"]] += 1
        good = [(ev, ch) for ev, ch in zip(c.evidence, checks) if ch["status"] in GROUNDED]
        best = max((STATUS_RANK[ch["status"]] for ch in checks), default=0)
        verdict = next(k for k, v in STATUS_RANK.items() if v == best) if checks else "rejected"
        if not good:
            verdict = "rejected"
        subj = resolver.resolve(c.subject)
        obj = resolver.resolve(c.object) if c.object else None
        reasons: list[str] = []
        if any(ch["status"] == "rejected" for ch in checks) and good:
            reasons.append("some evidence dropped (ungrounded)")
        if not good:
            reasons.append("; ".join(sorted({ch["detail"] for ch in checks})) or "no evidence")
        pred = c.predicate
        if c.kind == "edge" and subj["label"] and obj:
            pred = pred_fix.get((pred, subj["label"], obj["label"]), pred_fix.get((pred, None, None), pred))
            if (pred, subj["label"], obj["label"]) in flips:
                subj, obj = obj, subj
                reasons.append("direction flipped by calibration rule")
        onto = check_ontology(c.kind, ont, subj["label"], pred, obj["label"] if obj else None)
        if subj["method"] == "pronoun" or (obj and obj["method"] == "pronoun"):
            verdict = "rejected"
            reasons.append("pronoun or empty entity name")
        res = {"candidate_id": c.candidate_id, "kind": c.kind, "extractor": c.extractor, "verdict": verdict,
               "checks": checks, "ontology": onto, "subject": subj, "object": obj, "predicate": pred,
               "confidence": c.confidence, "reasons": reasons}
        results[c.candidate_id] = res
        if onto["status"] in ("schema_extension", "open_extension") and verdict != "rejected":
            prop = onto.get("proposal", {})
            key = tuple(sorted(prop.items()))
            entry = ext_index.setdefault(key, {**prop, "count": 0, "candidate_ids": [], "example_quote": None,
                                               "closed_world": ont.closed_world})
            entry["count"] += 1
            entry["candidate_ids"].append(c.candidate_id)
            if entry["example_quote"] is None and good:
                entry["example_quote"] = evidence_text(good[0][0])[:240]
        evid = [ev for ev, _ in good]
        docs = sorted({ev.doc_id for ev in evid})
        modalities = sorted({ev.kind for ev in evid})
        if c.kind == "edge" and obj:
            if subj["id"] is None or obj["id"] is None:
                continue
            s_id, t_id = subj["id"], obj["id"]
            rule_sym = any(r.symmetric and {r.source_class, r.target_class} == {subj["label"], obj["label"]}
                           for r in ont.rules_for(pred))
            if rule_sym and t_id < s_id:
                subj, obj = obj, subj
                s_id, t_id = t_id, s_id
            gkey = stable_json([s_id, pred, t_id, c.valid_from or ""])
            g = edge_groups.setdefault(gkey, {
                "proposal_id": "p-" + sha256_text(gkey)[:14], "kind": "edge",
                "source": {k: subj[k] for k in ("id", "label", "name")},
                "target": {k: obj[k] for k in ("id", "label", "name")},
                "predicate": pred, "valid_from": c.valid_from, "valid_to": c.valid_to,
                "candidate_ids": [], "verdicts": [], "evidence": [], "evidence_checks": [], "docs": set(),
                "modalities": set(), "extractors": set(), "confidences": [], "ontology": onto, "reasons": set(),
                "resolution": set()})
            g["candidate_ids"].append(c.candidate_id)
            g["verdicts"].append(verdict)
            g["extractors"].add(c.extractor)
            g["reasons"].update(reasons)
            g["resolution"].update(m for m in (subj["method"], obj["method"]) if m in ("ambiguous", "token_overlap"))
            if verdict != "rejected":
                g["confidences"].append(c.confidence)
                seen = {evidence_key(e) for e in g["evidence"]}
                for ev, ch in good:
                    if evidence_key(ev) not in seen:
                        g["evidence"].append(ev)
                        g["evidence_checks"].append(ch)
                        seen.add(evidence_key(ev))
                g["docs"].update(docs)
                g["modalities"].update(modalities)
        elif c.kind == "node":
            if subj["id"] is None:
                continue
            g = node_groups.setdefault(subj["id"], {
                "proposal_id": "n-" + sha256_text(subj["id"])[:14], "kind": "node", "node_id": subj["id"],
                "label": subj["label"], "name": subj["name"], "aliases": set(), "properties": {}, "property_sources": defaultdict(list),
                "candidate_ids": [], "verdicts": [], "evidence": [], "docs": set(), "extractors": set(), "ontology": onto,
                "structured": False})
            g["candidate_ids"].append(c.candidate_id)
            g["verdicts"].append(verdict)
            g["extractors"].add(c.extractor)
            if c.extractor == "structured":
                g["structured"] = True
            if verdict != "rejected":
                g["aliases"].update(a for a in c.aliases if normalize_name(a) != normalize_name(subj["name"]))
                if normalize_name(c.subject.name) != normalize_name(subj["name"]):
                    g["aliases"].add(display_name(c.subject.name))
                for k, v in c.properties.items():
                    g["properties"].setdefault(k, v)
                    g["property_sources"][k].append({"value": v, "candidate_id": c.candidate_id, "docs": docs,
                                                     "modalities": modalities})
                seen = {evidence_key(e) for e in g["evidence"]}
                g["evidence"].extend(ev for ev in evid if evidence_key(ev) not in seen)
                g["docs"].update(docs)
        elif c.kind == "attribute" and subj["id"] and verdict != "rejected":
            attr_claims.append({"node_id": subj["id"], "label": subj["label"], "name": subj["name"], "property": pred,
                                "value": c.value, "candidate_id": c.candidate_id, "docs": docs, "modalities": modalities,
                                "evidence": evid})

    # undated claims corroborate the single dated version of the same fact ----------------
    by_spt: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for k, g in edge_groups.items():
        by_spt[(g["source"]["id"], g["predicate"], g["target"]["id"])].append(k)
    for keys in by_spt.values():
        dated = [k for k in keys if edge_groups[k]["valid_from"]]
        undated = [k for k in keys if not edge_groups[k]["valid_from"]]
        if len(dated) != 1 or not undated:
            continue
        tgt = edge_groups[dated[0]]
        seen = {evidence_key(e) for e in tgt["evidence"]}
        for k in undated:
            g = edge_groups.pop(k)
            for field in ("candidate_ids", "verdicts", "confidences"):
                tgt[field].extend(g[field])
            for ev, ch in zip(g["evidence"], g["evidence_checks"]):
                if evidence_key(ev) not in seen:
                    tgt["evidence"].append(ev)
                    tgt["evidence_checks"].append(ch)
                    seen.add(evidence_key(ev))
            for field in ("docs", "modalities", "extractors", "reasons", "resolution"):
                tgt[field].update(g[field])
    # finalise edge proposals --------------------------------------------------------------
    proposals: list[dict[str, Any]] = []
    for g in edge_groups.values():
        grounded = [v for v in g["verdicts"] if v != "rejected"]
        best = max((STATUS_RANK[v] for v in grounded), default=0)
        g["best_verdict"] = next(k for k, v in STATUS_RANK.items() if v == best) if grounded else "rejected"
        base = max(g["confidences"]) if g["confidences"] else 0.0
        g["confidence"] = round(min(1.0, base + 0.05 * max(0, len(g["docs"]) - 1)), 3)
        g["docs"] = sorted(g["docs"])
        g["modalities"] = sorted(g["modalities"])
        g["extractors"] = sorted(g["extractors"])
        g["reasons"] = sorted(g["reasons"])
        g["resolution"] = sorted(g["resolution"])
        proposals.append(g)
    # contradictions -----------------------------------------------------------------------
    assertions = []
    for g in proposals:
        if g["best_verdict"] == "rejected":
            continue
        assertions.append({"ref": g["proposal_id"], "scope": "candidates", "source_id": g["source"]["id"],
                           "source_name": g["source"]["name"], "source_label": g["source"]["label"],
                           "predicate": g["predicate"], "target_id": g["target"]["id"], "target_name": g["target"]["name"],
                           "valid_from": g["valid_from"], "valid_to": g["valid_to"], "docs": g["docs"],
                           "modalities": g["modalities"]})
    names = {n.id: n.canonical_name for n in graph.nodes}
    labels = {n.id: n.label for n in graph.nodes}
    for e in graph.edges:
        if e.status in ("approved", "superseded"):
            assertions.append({"ref": e.id, "scope": "graph", "source_id": e.source_id, "source_name": names.get(e.source_id),
                               "source_label": labels.get(e.source_id), "predicate": e.type, "target_id": e.target_id,
                               "target_name": names.get(e.target_id), "valid_from": e.valid_from, "valid_to": e.valid_to,
                               "docs": sorted({x.doc_id for x in e.evidence}), "modalities": sorted({x.kind for x in e.evidence})})
    contradictions = [c for c in functional_conflicts(assertions, ont) if c["scope"] != "graph"]
    graph_edge_ids = {e.id for e in graph.edges}
    contradictions = [c for c in contradictions if any(v["ref"] not in graph_edge_ids for v in c["values"])]
    # property contradictions (structured values vs attribute claims)
    prop_values: dict[tuple[str, str], list[dict]] = defaultdict(list)
    node_label = {ng["node_id"]: ng["label"] for ng in node_groups.values()}
    node_label.update({a["node_id"]: a["label"] for a in attr_claims})
    for ng in node_groups.values():
        for k, srcs in ng["property_sources"].items():
            for s in srcs:
                prop_values[(ng["node_id"], k)].append({**s, "name": ng["name"]})
    for a in attr_claims:
        prop_values[(a["node_id"], a["property"])].append({"value": a["value"], "candidate_id": a["candidate_id"],
                                                           "docs": a["docs"], "modalities": a["modalities"], "name": a["name"]})
    for (nid, prop), vals in sorted(prop_values.items()):
        cls = ont.get_class(node_label.get(nid) or "")
        schema = cls.properties.get(prop) if cls else None
        if schema is not None and not schema.functional:
            continue
        distinct = {normalize_text(str(v["value"])) for v in vals}
        if len(distinct) > 1:
            contradictions.append({
                "contradiction_id": "x-" + sha256_text(f"{nid}|{prop}|{sorted(distinct)}")[:12], "kind": "functional_property",
                "source_id": nid, "source_name": vals[0]["name"], "predicate": prop, "scope": "candidates",
                "values": [{"ref": v["candidate_id"], "value": v["value"], "docs": v["docs"], "modalities": v["modalities"]}
                           for v in vals],
                "modalities": sorted({m for v in vals for m in v["modalities"]})})
    in_conflict = {v["ref"] for c in contradictions for v in c["values"]}
    # buckets --------------------------------------------------------------------------------
    for g in proposals:
        why = list(g["reasons"])
        neg = (normalize_name(g["source"]["name"]), g["predicate"], normalize_name(g["target"]["name"]))
        onto_status = g["ontology"]["status"]
        if g["best_verdict"] == "rejected":
            bucket = "quarantine"
        elif neg in negatives:
            bucket = "quarantine"
            why.append("calibration: rejected previously at Gate 2")
        elif g["proposal_id"] in in_conflict:
            bucket = "conflict"
            why.append("contradicts another value of a functional predicate")
        else:
            bucket = "auto"
            if onto_status != "ok":
                bucket = "review"
                why.append(g["ontology"].get("detail", onto_status))
            if g["best_verdict"] == "soft_verified":
                bucket = "review"
                why.append("only fuzzy (soft) grounding")
            if g["best_verdict"] == "needs_visual" or "image" in g["modalities"]:
                bucket = "review"
                why.append("image evidence needs visual confirmation")
            if g["confidence"] < threshold:
                bucket = "review"
                why.append(f"confidence {g['confidence']:.2f} < {threshold:.2f}")
            if g["resolution"]:
                bucket = "review"
                why.append("entity resolution: " + ", ".join(g["resolution"]))
            if g["source"]["id"] and g["source"]["id"] == g["target"]["id"]:
                bucket = "review"
                why.append("self-loop: subject and object resolve to the same entity")
        g["bucket"] = bucket
        g["status"] = "quarantined" if bucket == "quarantine" else "pending"
        g["reasons"] = sorted(set(why))
        order = sorted(range(len(g["evidence"])), key=lambda i: -STATUS_RANK[g["evidence_checks"][i]["status"]])
        g["evidence"] = [g["evidence"][i].model_dump(mode="json") for i in order]
        g["evidence_checks"] = [g["evidence_checks"][i] for i in order]
    proposals.sort(key=lambda g: (_bucket_order(g["bucket"]), -g["confidence"], g["proposal_id"]))
    node_props = []
    for g in node_groups.values():
        grounded = [v for v in g["verdicts"] if v != "rejected"]
        g["bucket"] = "quarantine" if not grounded else ("auto" if g["ontology"]["status"] == "ok" else "review")
        g["aliases"] = sorted(g["aliases"])
        g["docs"] = sorted(g["docs"])
        g["extractors"] = sorted(g["extractors"])
        g["evidence"] = [e.model_dump(mode="json") for e in g["evidence"]]
        g["property_sources"] = {k: v for k, v in g["property_sources"].items()}
        node_props.append(g)
    node_props.sort(key=lambda g: g["node_id"])
    for a in attr_claims:
        a["evidence"] = [e.model_dump(mode="json") for e in a["evidence"]]
    buckets = Counter(g["bucket"] for g in proposals)
    verdicts = Counter(r["verdict"] for r in results.values())
    report = {
        "generated_at": utcnow_iso(), "ontology_fingerprint": ont.fingerprint(), "closed_world": ont.closed_world,
        "threshold": threshold,
        "summary": {
            "candidates": len(cands), "verified": verdicts.get("verified", 0), "soft_verified": verdicts.get("soft_verified", 0),
            "needs_visual": verdicts.get("needs_visual", 0), "rejected": verdicts.get("rejected", 0),
            "evidence_checks": dict(counts), "edge_proposals": len(proposals),
            "buckets": {b: buckets.get(b, 0) for b in ("conflict", "review", "auto", "quarantine")},
            "node_proposals": len(node_props), "attribute_claims": len(attr_claims),
            "schema_extensions": len(ext_index), "contradictions": len(contradictions),
            "ambiguous_entities": len(resolver.ambiguous),
        },
        "candidates": results,
        "proposals": proposals,
        "node_proposals": node_props,
        "attribute_claims": attr_claims,
        "proposed_schema_extensions": sorted(ext_index.values(), key=lambda e: -e["count"]),
        "entity_resolution": {"merges": resolver.merges, "ambiguous_pairs": resolver.ambiguous},
        "contradictions": contradictions,
    }
    write_json(ws.verify_path, report)
    m = ws.load_manifest()
    m.set_phase("verified")
    ws.save_manifest(m)
    return report


def compact_verify(report: dict[str, Any]) -> dict[str, Any]:
    return {"summary": report["summary"],
            "contradictions": [{"source": c["source_name"], "predicate": c["predicate"],
                                "values": [v.get("target_name", v.get("value")) for v in c["values"]],
                                "modalities": c["modalities"]} for c in report["contradictions"]],
            "schema_extensions": [{k: e.get(k) for k in ("source_class", "predicate", "target_class", "class", "property", "count")}
                                  for e in report["proposed_schema_extensions"][:10]],
            "ambiguous": report["entity_resolution"]["ambiguous_pairs"][:10]}


# --------------------------------------------------------------------------------------------
# Doctor: Graph Health Scorecard
# --------------------------------------------------------------------------------------------


def source_integrity(ws: Workspace) -> dict[str, Any]:
    """Re-derive chunks from the sources and compare hashes with ``chunks.jsonl``."""
    report = read_json(ws.audit_path, {}) or {}
    details = {e["doc_id"]: e.get("details", {}) for e in report.get("files", []) if e.get("doc_id")}
    stored = ws.load_chunks()
    by_doc: dict[str, list] = defaultdict(list)
    for c in stored.values():
        by_doc[c.doc_id].append(c)
    m = ws.load_manifest()
    checked = mismatched = 0
    problems = []
    for doc_id, chunks in sorted(by_doc.items()):
        rec = m.source_by_id(doc_id)
        if rec is None or not Path(rec.path).exists():
            problems.append({"doc_id": doc_id, "issue": "source missing"})
            mismatched += len(chunks)
            continue
        pages = extract_pages(ws, rec, details.get(doc_id, {}))
        texts = {(p.page_no, p.span_label): p.text for p in pages}
        for c in chunks:
            checked += 1
            page_text = next((t for (pn, lbl), t in texts.items()
                              if pn == c.page_no and (pn is not None or c.page_or_span.startswith(lbl + ":"))), None)
            if page_text is None or sha256_text(c.text) != c.sha256 or \
                    sha256_text(page_text[c.char_start:c.char_end]) != c.sha256:
                mismatched += 1
                if len(problems) < 20:
                    problems.append({"chunk_id": c.chunk_id, "issue": "chunk text no longer matches source"})
    return {"chunks_checked": checked, "chunks_matching": checked - mismatched,
            "pct": round(100.0 * (checked - mismatched) / checked, 2) if checked else 100.0, "problems": problems}


def doctor(ws: Workspace, deep: bool = True) -> dict[str, Any]:
    ws.require()
    graph = ws.load_graph()
    ont = ws.load_ontology(True) or Ontology()
    ctx = VerifyContext(ws)
    node_ids = {n.id for n in graph.nodes}
    labels = {n.id: n.label for n in graph.nodes}
    names = {n.id: n.canonical_name for n in graph.nodes}
    committed = [e for e in graph.edges if e.status in ("approved", "superseded")]
    ev_counts: Counter = Counter()
    ungrounded = []
    grounded_edges = 0
    for e in committed:
        checks = [verify_evidence(x, ctx) for x in e.evidence]
        for c in checks:
            ev_counts[c["status"]] += 1
        ok = bool(checks) and all(c["status"] in GROUNDED for c in checks)
        if ok:
            grounded_edges += 1
        else:
            ungrounded.append({"edge_id": e.id, "type": e.type, "source": names.get(e.source_id), "target": names.get(e.target_id),
                               "issues": [c["detail"] for c in checks if c["status"] not in GROUNDED] or ["no evidence"]})
    nodes_with_ev = [n for n in graph.nodes if n.evidence]
    nodes_grounded = sum(1 for n in nodes_with_ev if all(verify_evidence(x, ctx)["status"] in GROUNDED for x in n.evidence))
    classes = ont.class_names()
    schema_bad = [{"node_id": n.id, "issue": f"label {n.label} not in ontology"} for n in graph.nodes if n.label not in classes]
    for e in committed:
        if e.source_id in labels and e.target_id in labels and not (
                ont.allows(labels[e.source_id], e.type, labels[e.target_id]) or e.properties.get("schema_extension")):
            schema_bad.append({"edge_id": e.id, "issue": f"{labels[e.source_id]}-[{e.type}]->{labels[e.target_id]} not in grammar"})
    total_items = len(graph.nodes) + len(committed)
    dangling = [{"edge_id": e.id, "missing": [x for x in (e.source_id, e.target_id) if x not in node_ids]}
                for e in graph.edges if e.source_id not in node_ids or e.target_id not in node_ids]
    degree: Counter = Counter()
    for e in committed:
        degree[e.source_id] += 1
        degree[e.target_id] += 1
    orphans = sorted(n.id for n in graph.nodes if degree[n.id] == 0)
    assertions = [{"ref": e.id, "scope": "graph", "source_id": e.source_id, "source_name": names.get(e.source_id),
                   "source_label": labels.get(e.source_id), "predicate": e.type, "target_id": e.target_id,
                   "target_name": names.get(e.target_id), "valid_from": e.valid_from, "valid_to": e.valid_to,
                   "docs": sorted({x.doc_id for x in e.evidence}), "modalities": sorted({x.kind for x in e.evidence})}
                  for e in committed]
    graph_contradictions = functional_conflicts(assertions, ont)
    vrep = read_json(ws.verify_path, {}) or {}
    decisions = ws.load_decisions()
    edge_map = graph.edge_map()
    pending_contradictions = [c for c in vrep.get("contradictions", []) if not contradiction_resolved(c, decisions, edge_map)]
    contradictions = graph_contradictions + [dict(c, scope=c.get("scope", "candidates")) for c in pending_contradictions]
    # coverage per source
    m = ws.load_manifest()
    chunks_by_doc: Counter = Counter(c.doc_id for c in ctx.chunks.values())
    cited_chunks: dict[str, set] = defaultdict(set)
    edges_by_doc: Counter = Counter()
    nodes_by_doc: Counter = Counter()
    for e in committed:
        for d in {x.doc_id for x in e.evidence}:
            edges_by_doc[d] += 1
        for x in e.evidence:
            if x.kind == "text":
                cited_chunks[x.doc_id].add(x.chunk_id)
    for n in graph.nodes:
        for d in {x.doc_id for x in n.evidence}:
            nodes_by_doc[d] += 1
        for x in n.evidence:
            if x.kind == "text":
                cited_chunks[x.doc_id].add(x.chunk_id)
    coverage = {}
    for s in m.sources:
        if s.status not in ("usable", "needs_ocr_or_vlm"):
            continue
        total = chunks_by_doc.get(s.doc_id, 0)
        cov = len(cited_chunks.get(s.doc_id, set()))
        coverage[s.doc_id] = {"name": s.name, "modality": s.modality, "chunks": total, "chunks_cited": cov,
                              "chunk_coverage_pct": round(100.0 * cov / total, 1) if total else None,
                              "nodes": nodes_by_doc.get(s.doc_id, 0), "edges": edges_by_doc.get(s.doc_id, 0)}
    store = GraphStore(ws.sqlite_path)
    comms = store.load_communities(graph.content_hash()) if ws.sqlite_path.exists() else None
    grounding_pct = round(100.0 * grounded_edges / len(committed), 2) if committed else 100.0
    schema_pct = round(100.0 * (total_items - len(schema_bad)) / total_items, 2) if total_items else 100.0
    orphan_ratio = len(orphans) / len(graph.nodes) if graph.nodes else 0.0
    score = (0.4 * grounding_pct + 0.25 * schema_pct + 15.0 * (1.0 if not graph_contradictions else max(0.0, 1 - len(graph_contradictions) / max(1, len(committed)) * 10))
             + 10.0 * (0.0 if dangling else 1.0) + 10.0 * (1 - orphan_ratio))
    buckets = (vrep.get("summary") or {}).get("buckets", {})
    undecided = [p for p in vrep.get("proposals", []) if p["proposal_id"] not in decisions and p["bucket"] != "quarantine"]
    card = {
        "generated_at": utcnow_iso(),
        "graph_hash": graph.content_hash(),
        "nodes": len(graph.nodes),
        "edges": {"total": len(graph.edges), "approved": sum(1 for e in graph.edges if e.status == "approved"),
                  "superseded": sum(1 for e in graph.edges if e.status == "superseded"),
                  "active": sum(1 for e in graph.edges if e.active_at(None))},
        "labels": dict(Counter(n.label for n in graph.nodes)),
        "predicates": dict(Counter(e.type for e in committed)),
        "grounding": {"edges_checked": len(committed), "edges_grounded": grounded_edges, "pct": grounding_pct,
                      "nodes_with_evidence": len(nodes_with_ev), "nodes_grounded": nodes_grounded,
                      "evidence": dict(ev_counts), "ungrounded_edges": ungrounded[:25]},
        "schema_compliance": {"pct": schema_pct, "violations": schema_bad[:25], "closed_world": ont.closed_world},
        "contradiction_count": len(contradictions),
        "contradictions": contradictions,
        "orphan_nodes": orphans,
        "dangling_edges": dangling,
        "coverage_per_source": coverage,
        "communities": len(comms) if comms else 0,
        "review_queue": {"undecided_proposals": len(undecided), "buckets": buckets},
        "score": round(max(0.0, min(100.0, score)), 1),
    }
    if deep:
        card["source_integrity"] = source_integrity(ws)
    write_json(ws.p("doctor-report.json"), card)
    return card


def compact_doctor(card: dict[str, Any]) -> dict[str, Any]:
    return {k: card[k] for k in ("nodes", "edges", "score", "contradiction_count", "communities")} | {
        "grounding_pct": card["grounding"]["pct"], "schema_compliance_pct": card["schema_compliance"]["pct"],
        "orphans": len(card["orphan_nodes"]), "dangling": len(card["dangling_edges"]),
        "source_integrity_pct": card.get("source_integrity", {}).get("pct"),
        "contradictions": [{"source": c["source_name"], "predicate": c["predicate"], "scope": c["scope"],
                            "values": [v.get("target_name", v.get("value")) for v in c["values"]]} for c in card["contradictions"]],
        "review_queue": card["review_queue"],
    }

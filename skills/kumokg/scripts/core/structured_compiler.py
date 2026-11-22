"""Zero-token structured compiler.

Compiles tables (CSV/TSV/XLSX/Parquet/SQLite), semi-structured trees (JSON/JSONL/YAML/XML), Jupyter
notebooks and Obsidian vaults into graph candidates deterministically, attaching exact
``TableEvidence`` / ``JsonEvidence`` / ``NotebookEvidence`` / ``TextEvidence`` pointers. It also proposes
``structured_mappings`` + classes + relation grammar for ``ontology.draft.json`` so users can confirm or
rename them at Gate 1.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ir import (
    Candidate, EntityClass, EntityRef, FieldRelation, GroundedExample, JsonEvidence, NotebookEvidence, Ontology,
    ParentRelation, PropertySchema, StructuredMapping, TableEvidence, TextEvidence, Workspace, fold_text,
    normalize_name, read_json, to_class_name, to_predicate, upsert_candidates, write_json,
)
from .modality_router import (
    cell_source, discover_terms, escape_pointer_token, load_notebook, load_semistructured, parse_markdown_note,
    read_table,
)

IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w\.]*)\s+import\b|import\s+([A-Za-z_][\w\.]*(?:\s*,\s*[A-Za-z_][\w\.]*)*))")
RESERVED_FM_KEYS = {"type", "class", "aliases", "alias", "tags", "title", "cssclass", "cssclasses"}


def singularize(word: str) -> str:
    w = word.strip()
    lw = w.lower()
    if lw.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if lw.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if lw.endswith("s") and not lw.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _coerce(value: Any, ptype: str) -> Any:
    if value is None or value == "":
        return None
    try:
        if ptype == "int":
            return int(float(value))
        if ptype == "float":
            return float(value)
        if ptype == "bool":
            return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")
        if ptype == "date":
            return str(value)[:10]
    except (TypeError, ValueError):
        return str(value)
    return str(value) if not isinstance(value, str) else value


# --------------------------------------------------------------------------------------------
# Mapping proposal
# --------------------------------------------------------------------------------------------


def _relation_name_for(field: str, target_class: str) -> str:
    stem = re.sub(r"(_ids?|_refs?|_key|Ids?)$", "", field, flags=re.I)
    return to_predicate(f"has_{stem}") if stem else to_predicate(f"has_{target_class}")


def _iter_pattern(doc: Any, pattern: str) -> list[tuple[str, dict]]:
    """Expand an array pattern like ``/projects/*/milestones/*`` into concrete (pointer, object) pairs."""
    tokens = [t for t in pattern.split("/")[1:]] if pattern else []
    out: list[tuple[str, dict]] = []

    def rec(node: Any, i: int, ptr: str) -> None:
        if i == len(tokens):
            if isinstance(node, dict):
                out.append((ptr, node))
            return
        tok = tokens[i].replace("~1", "/").replace("~0", "~")
        if tok == "*":
            if isinstance(node, list):
                for j, item in enumerate(node):
                    rec(item, i + 1, f"{ptr}/{j}")
        elif isinstance(node, dict) and tok in node:
            rec(node[tok], i + 1, f"{ptr}/{escape_pointer_token(tok)}")

    rec(doc, 0, "")
    return out


def propose_mappings(ws: Workspace) -> tuple[list[StructuredMapping], dict[str, Any]]:
    """Derive mapping proposals from ``audit-report.json``. Returns (mappings, vault_info)."""
    report = read_json(ws.audit_path, {}) or {}
    files = [f for f in report.get("files", []) if f.get("doc_id") and f["status"] == "usable"]
    mappings: list[StructuredMapping] = []
    table_class: dict[tuple[str, str], str] = {}
    # tables -------------------------------------------------------------------------------
    for f in files:
        if f["modality"] != "structured":
            continue
        for t in f["details"].get("tables", []):
            name_col = t.get("name_column") or t.get("primary_key")
            if not name_col:
                continue
            cls = to_class_name(singularize(t["table"]))
            table_class[(f["doc_id"], t["table"])] = cls
    for f in files:
        if f["modality"] != "structured":
            continue
        for t in f["details"].get("tables", []):
            key = (f["doc_id"], t["table"])
            if key not in table_class:
                continue
            fk_cols = {fk["column"]: fk for fk in t.get("foreign_keys", [])}
            relations = []
            for col, fk in fk_cols.items():
                tcls = table_class.get((fk.get("ref_doc_id", f["doc_id"]), fk["ref_table"]))
                if not tcls:
                    continue
                relations.append(FieldRelation(
                    field=col, predicate=_relation_name_for(col, tcls), target_class=tcls, target_match="id",
                    target_mapping=f"{fk.get('ref_doc_id', f['doc_id'])}:{fk['ref_table']}",
                    separator=";" if fk.get("multi") else None))
            name_col = t.get("name_column") or t.get("primary_key")
            props = {c["name"]: c["type"] for c in t["columns"]
                     if c["name"] not in fk_cols and c["name"] != name_col and c["name"] != t.get("primary_key")
                     and not c["is_prose"]}
            mappings.append(StructuredMapping(
                mapping_id=f"{f['doc_id']}:{t['table']}", kind="table", source=f["doc_id"], table=t["table"],
                entity_class=table_class[key], id_field=t.get("primary_key"), name_field=name_col,
                property_fields=props, prose_fields=list(t.get("prose_columns", [])), relations=relations,
                row_count=t["row_count"]))
    # semi-structured ----------------------------------------------------------------------
    id_index: dict[str, tuple[str, set[str]]] = {}
    for m in mappings:
        if m.id_field:
            _, rows = read_table(Path(ws.load_manifest().source_by_id(m.source).path), m.table)
            id_index[m.mapping_id] = (m.entity_class, {str(r.get(m.id_field)) for r in rows})
    json_props: list[tuple[dict, dict, str]] = []
    for f in files:
        if f["modality"] != "semi_structured":
            continue
        doc = load_semistructured(Path(f["path"]))
        for arr in f["details"].get("object_arrays", []):
            name_key = arr.get("name_key") or next((k for k in arr["id_keys"] if k.lower() in ("id", "key", "uid", "slug")), None)
            if not name_key:
                continue
            segs = [s for s in arr["array_path"].split("/") if s and s != "*"]
            cls = to_class_name(singularize(segs[-1] if segs else Path(f["path"]).stem))
            id_field = next((k for k in arr["id_keys"] if k.lower() in ("id", "key", "uid", "slug")), None)
            mid = f"{f['doc_id']}:{arr['array_path']}"
            objs = _iter_pattern(doc, arr["array_path"])
            if id_field:
                id_index[mid] = (cls, {str(o.get(id_field)) for _, o in objs if o.get(id_field) is not None})
            json_props.append((f, arr, mid))
            mappings.append(StructuredMapping(
                mapping_id=mid, kind="json", source=f["doc_id"], table=arr["array_path"], entity_class=cls,
                id_field=id_field, name_field=name_key, row_count=arr["count"],
                prose_fields=list(arr.get("prose_fields", []))))
    by_mid = {m.mapping_id: m for m in mappings}
    for f, arr, mid in json_props:
        m = by_mid[mid]
        doc = load_semistructured(Path(f["path"]))
        objs = _iter_pattern(doc, arr["array_path"])
        for key in arr["keys"]:
            if key in (m.name_field, m.id_field) or key in m.prose_fields:
                continue
            values = [o.get(key) for _, o in objs if key in o]
            scalars = [v for v in values if isinstance(v, (str, int, float, bool))]
            lists = [v for v in values if isinstance(v, list) and all(isinstance(x, (str, int)) for x in v)]
            flat = {str(v) for v in scalars} | {str(x) for v in lists for x in v}
            target = None
            if key in arr["id_keys"] and flat:
                for other_mid, (ocls, ids) in id_index.items():
                    if other_mid != mid and flat <= ids:
                        target = (other_mid, ocls)
                        break
            if target:
                m.relations.append(FieldRelation(field=key, predicate=_relation_name_for(key, target[1]),
                                                 target_class=target[1], target_match="id", target_mapping=target[0]))
            elif scalars and len(scalars) == len(values):
                m.property_fields[key] = _infer_json_type(scalars)
        # nested arrays -> parent relation
        parent_pattern = arr["array_path"].rsplit("/", 2)[0] if arr["array_path"].count("*") >= 2 else None
        if parent_pattern:
            parent_mid = f"{f['doc_id']}:{parent_pattern}"
            if parent_mid in by_mid:
                child_seg = arr["array_path"].rstrip("/*").split("/")[-1]
                m.parent_relation = ParentRelation(predicate=to_predicate(f"has_{singularize(child_seg)}"),
                                                   parent_class=by_mid[parent_mid].entity_class)
    # notebooks ----------------------------------------------------------------------------
    for f in files:
        if f["ext"] == ".ipynb":
            mappings.append(StructuredMapping(
                mapping_id=f"{f['doc_id']}:notebook", kind="notebook", source=f["doc_id"], table="cells",
                entity_class="Notebook", name_field="title",
                relations=[FieldRelation(field="imports", predicate="IMPORTS", target_class="Library")],
                row_count=f["details"].get("cells", 0)))
    # vault --------------------------------------------------------------------------------
    vault_notes = [f for f in files if f["ext"] in (".md", ".markdown") and f["details"].get("is_vault_note")]
    vault_info: dict[str, Any] = {"notes": {}, "relations": set()}
    if vault_notes:
        notes = {}
        for f in vault_notes:
            note = parse_markdown_note(Path(f["path"]))
            fm = note["frontmatter"]
            title = str(fm.get("title") or Path(f["path"]).stem)
            cls = to_class_name(str(fm.get("type") or fm.get("class") or "Note"))
            notes[normalize_name(title)] = {"doc_id": f["doc_id"], "title": title, "class": cls, "note": note}
            for a in _as_list(fm.get("aliases") or fm.get("alias")):
                notes.setdefault(normalize_name(str(a)), notes[normalize_name(title)])
        props: dict[str, str] = {}
        for info in {id(v): v for v in notes.values()}.values():
            for k, v in info["note"]["frontmatter"].items():
                if k not in RESERVED_FM_KEYS and isinstance(v, (str, int, float, bool)):
                    props[k] = _infer_json_type([v])
        relations: dict[tuple[str, str, str], None] = {}
        for info in {id(v): v for v in notes.values()}.values():
            fields_spans = [(fl["char_start"], fl["char_end"], fl["key"]) for fl in info["note"]["inline_fields"]]
            for link in info["note"]["wikilinks"]:
                pred = "LINKS_TO"
                for s, e, key in fields_spans:
                    if s <= link["char_start"] < e:
                        pred = to_predicate(key)
                tgt = notes.get(normalize_name(link["target"]))
                relations[(info["class"], pred, tgt["class"] if tgt else "Note")] = None
        vault_info = {"notes": notes, "relations": set(relations)}
        mappings.append(StructuredMapping(
            mapping_id="vault:notes", kind="vault", source=",".join(sorted(f["doc_id"] for f in vault_notes)),
            table="notes", entity_class="Note", name_field="title", property_fields=props,
            relations=[FieldRelation(field=f"{p}", predicate=p, target_class=t) for (_, p, t) in sorted(relations)],
            row_count=len(vault_notes)))
    return mappings, vault_info


def _infer_json_type(values: list[Any]) -> str:
    if all(isinstance(v, bool) for v in values):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "int"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "float"
    if all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v) for v in values):
        return "date"
    return "string"


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------------------------
# Ontology draft
# --------------------------------------------------------------------------------------------


def _is_front_matter(text: str) -> bool:
    """Title pages and tables of contents make poor grounded examples."""
    if re.search(r"\.{6,}", text):
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    short = sum(1 for ln in lines if len(ln) < 30)
    return len(text) < 700 and short / len(lines) > 0.6


def find_examples(ws: Workspace, terms: list[str], limit: int = 3, max_chars: int = 240,
                  aliases: dict[str, list[str]] | None = None) -> list[GroundedExample]:
    """Verbatim, sentence-bounded grounded quotes for seed terms (or their aliases), skipping front matter."""
    from .worker_pool import fold_preserving, sentence_spans

    chunks = [c for c in ws.load_chunks().values() if not _is_front_matter(c.text)]
    out: list[GroundedExample] = []
    for term in terms:
        if len(out) >= limit:
            break
        surfaces = [term] + [a for a in (aliases or {}).get(term, []) if a != term]
        ws_rx = r"\s+"
        pats = []
        for surf in surfaces:
            body = ws_rx.join(re.escape(w) for w in fold_preserving(surf).split())
            pats.append(re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])", 0 if any(c.isupper() for c in surf) else re.I))
        found = None
        fallback = None
        for ch in chunks:
            folded = fold_preserving(ch.text)
            spans = sentence_spans(ch.text)
            for pat in pats:
                for m in pat.finditer(folded):
                    s, e = next(((s, e) for s, e in spans if s <= m.start() < e), (m.start(), m.end()))
                    hit = (ch, m.start(), m.end(), ch.text[m.start():m.end()], s, e)
                    if len(ch.text[s:e].split()) >= 8:  # a real sentence, not a chapter heading
                        found = hit
                        break
                    fallback = fallback or hit
                if found:
                    break
            if found:
                break
        found = found or fallback
        if not found:
            continue
        ch, a, b, surface, s, e = found
        if e - s > max_chars:
            s, e = max(s, a - max_chars // 2), min(e, b + max_chars // 2)
            while s > 0 and not ch.text[s - 1].isspace():
                s += 1
            while e < len(ch.text) and not ch.text[e].isspace():
                e += 1
        out.append(GroundedExample(name=term, quote=ch.text[s:e].strip(), doc_id=ch.doc_id, chunk_id=ch.chunk_id,
                                   page_no=ch.page_no, surface=" ".join(surface.split())))
    return out


def propose_ontology(ws: Workspace, agent_draft: dict[str, Any] | None = None) -> Ontology:
    """Write ``ontology.draft.json`` from structured mappings + an optional agent-authored draft."""
    ws.require()
    base = Ontology.model_validate(agent_draft) if agent_draft else (ws.load_ontology(False) or Ontology())
    mappings, vault_info = propose_mappings(ws)
    existing = {m.mapping_id: m for m in base.structured_mappings}
    merged = []
    for m in mappings:
        merged.append(existing.pop(m.mapping_id, m))
    merged.extend(existing.values())
    base.structured_mappings = merged
    report = read_json(ws.audit_path, {}) or {}
    files = {f["doc_id"]: f for f in report.get("files", []) if f.get("doc_id")}
    for m in base.structured_mappings:
        if not m.enabled:
            continue
        if m.kind == "vault":
            for info in {id(v): v for v in vault_info.get("notes", {}).values()}.values():
                cls = base.ensure_class(info["class"], "Obsidian note type (frontmatter `type`)")
                for k, t in m.property_fields.items():
                    if k in info["note"]["frontmatter"]:
                        cls.properties.setdefault(k, PropertySchema(type=t))
            base.ensure_class("Note", "Obsidian note without an explicit type")
            for (s, p, t) in sorted(vault_info.get("relations", set())):
                base.ensure_relation(s, p, t, description="Obsidian wikilink / inline field")
            continue
        cls = base.ensure_class(m.entity_class, f"Compiled from {m.kind} source {m.table}")
        for k, t in m.property_fields.items():
            cls.properties.setdefault(k, PropertySchema(type=t))
        for r in m.relations:
            base.ensure_class(r.target_class, f"Referenced by {m.entity_class}.{r.field}")
            base.ensure_relation(m.entity_class, r.predicate, r.target_class, description=f"{m.table}.{r.field}")
        if m.parent_relation:
            base.ensure_relation(m.parent_relation.parent_class, m.parent_relation.predicate, m.entity_class,
                                 description=f"nesting {m.table}")
        if not cls.examples and m.kind in ("table", "json"):
            cls.examples = _structured_examples(ws, m, files)
    chunks = ws.load_chunks()
    base.discovered_terms = discover_terms(chunks.values()) if chunks else []
    for cls in base.entity_classes:
        if len(cls.examples) < 3 and cls.seed_terms:
            have = {e.name for e in cls.examples}
            alias_map = {a.canonical: a.aliases for a in base.alias_rules if a.entity_class in (None, cls.name)}
            cls.examples.extend(find_examples(ws, [t for t in cls.seed_terms if t not in have], 3 - len(cls.examples),
                                              aliases=alias_map))
    write_json(ws.ontology_draft_path, base)
    manifest = ws.load_manifest()
    manifest.set_phase("ontology_draft")
    ws.save_manifest(manifest)
    return base


def _structured_examples(ws: Workspace, m: StructuredMapping, files: dict[str, Any]) -> list[GroundedExample]:
    rec = ws.load_manifest().source_by_id(m.source)
    if not rec:
        return []
    out = []
    if m.kind == "table":
        _, rows = read_table(Path(rec.path), m.table)
        for i, row in enumerate(rows[:3]):
            out.append(GroundedExample(name=str(row.get(m.name_field)), doc_id=m.source,
                                       quote=f"{m.table}[{i}].{m.name_field} = {row.get(m.name_field)}"))
    else:
        doc = load_semistructured(Path(rec.path))
        for ptr, obj in _iter_pattern(doc, m.table)[:3]:
            out.append(GroundedExample(name=str(obj.get(m.name_field)), doc_id=m.source,
                                       quote=f"{ptr}/{m.name_field} = {obj.get(m.name_field)}"))
    return out


def rename_class(ont: Ontology, old: str, new: str) -> Ontology:
    """Rename (or merge) an entity class everywhere it is referenced."""
    new = to_class_name(new)
    if old == new:
        return ont
    src = ont.get_class(old)
    dst = ont.get_class(new)
    if src and dst:
        for k, v in src.properties.items():
            dst.properties.setdefault(k, v)
        dst.examples.extend(e for e in src.examples if e not in dst.examples)
        dst.seed_terms.extend(t for t in src.seed_terms if t not in dst.seed_terms)
        ont.entity_classes = [c for c in ont.entity_classes if c.name != old]
    elif src:
        src.name = new
    for r in ont.relations:
        if r.source_class == old:
            r.source_class = new
        if r.target_class == old:
            r.target_class = new
    for m in ont.structured_mappings:
        if m.entity_class == old:
            m.entity_class = new
        for fr in m.relations:
            if fr.target_class == old:
                fr.target_class = new
        if m.parent_relation and m.parent_relation.parent_class == old:
            m.parent_relation.parent_class = new
    for a in ont.alias_rules:
        if a.entity_class == old:
            a.entity_class = new
    _dedupe_relations(ont)
    return ont


def rename_predicate(ont: Ontology, old: str, new: str) -> Ontology:
    old, new = to_predicate(old), to_predicate(new)
    for r in ont.relations:
        if r.predicate == old:
            r.predicate = new
    for m in ont.structured_mappings:
        for fr in m.relations:
            if fr.predicate == old:
                fr.predicate = new
        if m.parent_relation and m.parent_relation.predicate == old:
            m.parent_relation.predicate = new
    _dedupe_relations(ont)
    return ont


def _dedupe_relations(ont: Ontology) -> None:
    seen: dict[tuple[str, str, str], Any] = {}
    for r in ont.relations:
        key = (r.source_class, r.predicate, r.target_class)
        if key in seen:
            prev = seen[key]
            prev.functional = prev.functional or r.functional
            prev.cue_phrases.extend(c for c in r.cue_phrases if c not in prev.cue_phrases)
        else:
            seen[key] = r
    ont.relations = list(seen.values())


def sync_mapping_grammar(ont: Ontology) -> Ontology:
    """Ensure every enabled structured mapping relation is part of the relation grammar."""
    for m in ont.structured_mappings:
        if not m.enabled or m.kind == "vault":
            continue
        ont.ensure_class(m.entity_class)
        for fr in m.relations:
            ont.ensure_class(fr.target_class)
            ont.ensure_relation(m.entity_class, fr.predicate, fr.target_class)
        if m.parent_relation:
            ont.ensure_class(m.parent_relation.parent_class)
            ont.ensure_relation(m.parent_relation.parent_class, m.parent_relation.predicate, m.entity_class)
    return ont


def approve_ontology(ws: Workspace, data: dict[str, Any] | None = None) -> Ontology:
    ont = Ontology.model_validate(data) if data else ws.load_ontology(False)
    if ont is None:
        raise FileNotFoundError("no ontology draft to approve (run `kumokg propose-ontology` first)")
    sync_mapping_grammar(ont)
    _dedupe_relations(ont)
    known = ont.class_names()
    errors = []
    for r in ont.relations:
        for c in (r.source_class, r.target_class):
            if c not in known:
                errors.append(f"relation {r.source_class}-{r.predicate}->{r.target_class} references unknown class {c}")
    if errors:
        raise ValueError("; ".join(errors))
    write_json(ws.ontology_approved_path, ont)
    manifest = ws.load_manifest()
    manifest.gates["gate1"] = "approved"
    manifest.set_phase("ontology_approved")
    ws.save_manifest(manifest)
    return ont


# --------------------------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------------------------


def _node_name_map(ws: Workspace, m: StructuredMapping) -> dict[str, str]:
    """id -> display name for a mapping (used to resolve FK targets)."""
    rec = ws.load_manifest().source_by_id(m.source)
    if not rec or not m.id_field:
        return {}
    if m.kind == "table":
        _, rows = read_table(Path(rec.path), m.table)
        return {str(r.get(m.id_field)): str(r.get(m.name_field) or r.get(m.id_field)) for r in rows}
    if m.kind == "json":
        doc = load_semistructured(Path(rec.path))
        return {str(o.get(m.id_field)): str(o.get(m.name_field) or o.get(m.id_field))
                for _, o in _iter_pattern(doc, m.table) if o.get(m.id_field) is not None}
    return {}


def compile_structured(ws: Workspace, ontology: Ontology | None = None) -> dict[str, Any]:
    ws.require()
    ont = ontology or ws.load_ontology(True) or ws.load_ontology(False)
    if ont is None:
        raise FileNotFoundError("no ontology found: run `kumokg propose-ontology` first")
    manifest = ws.load_manifest()
    mappings = {m.mapping_id: m for m in ont.structured_mappings if m.enabled}
    name_maps = {mid: _node_name_map(ws, m) for mid, m in mappings.items()}
    candidates: list[Candidate] = []
    stats: dict[str, int] = {}
    for mid, m in mappings.items():
        before = len(candidates)
        if m.kind == "table":
            candidates.extend(_compile_table(ws, m, name_maps))
        elif m.kind == "json":
            candidates.extend(_compile_json(ws, m, name_maps, mappings))
        elif m.kind == "notebook":
            candidates.extend(_compile_notebook(ws, m))
        elif m.kind == "vault":
            candidates.extend(_compile_vault(ws, m))
        stats[mid] = len(candidates) - before
    res = upsert_candidates(ws, candidates, replace_extractors={"structured"})
    manifest.set_phase("structured_compiled")
    ws.save_manifest(manifest)
    return {"candidates": len(candidates), "per_mapping": stats, **res}


def _compile_table(ws: Workspace, m: StructuredMapping, name_maps: dict) -> list[Candidate]:
    rec = ws.load_manifest().source_by_id(m.source)
    if rec is None:
        return []
    _, rows = read_table(Path(rec.path), m.table)
    out: list[Candidate] = []
    names = [str(r.get(m.name_field) or "") for r in rows]
    dup = {n for n in names if names.count(n) > 1}
    for i, row in enumerate(rows):
        raw_name = row.get(m.name_field)
        if raw_name in (None, ""):
            continue
        name = str(raw_name)
        disp = f"{name} ({row.get(m.id_field)})" if name in dup and m.id_field else name
        ev = [TableEvidence(doc_id=m.source, sheet_or_table=m.table, row_index=i, column_name=m.name_field,
                            cell_value=raw_name, file_sha256=rec.sha256)]
        props = {}
        for col, ptype in m.property_fields.items():
            val = row.get(col)
            if val in (None, ""):
                continue
            props[col] = _coerce(val, ptype)
            ev.append(TableEvidence(doc_id=m.source, sheet_or_table=m.table, row_index=i, column_name=col,
                                    cell_value=val, file_sha256=rec.sha256))
        if m.id_field:
            props.setdefault("source_key", str(row.get(m.id_field)))
        subj = EntityRef(name=disp, label=m.entity_class)
        out.append(Candidate(kind="node", subject=subj, properties=props, confidence=1.0, evidence=ev,
                             extractor="structured").finalize())
        for rel in m.relations:
            raw = row.get(rel.field)
            if raw in (None, ""):
                continue
            parts = [p.strip() for p in re.split(r"[;|]", str(raw))] if (rel.separator or re.search(r"[;|]", str(raw))) else [str(raw).strip()]
            for part in parts:
                if not part:
                    continue
                tname = part
                if rel.target_match == "id":
                    tmap = name_maps.get(rel.target_mapping or "", {})
                    if part not in tmap:
                        continue
                    tname = tmap[part]
                ev_list = [TableEvidence(doc_id=m.source, sheet_or_table=m.table, row_index=i, column_name=rel.field,
                                         cell_value=raw, file_sha256=rec.sha256)]
                vf = _coerce(row.get(rel.valid_from_field), "date") if rel.valid_from_field else None
                vt = _coerce(row.get(rel.valid_to_field), "date") if rel.valid_to_field else None
                for col in (rel.valid_from_field, rel.valid_to_field):
                    if col and row.get(col) not in (None, ""):
                        ev_list.append(TableEvidence(doc_id=m.source, sheet_or_table=m.table, row_index=i,
                                                     column_name=col, cell_value=row.get(col), file_sha256=rec.sha256))
                out.append(Candidate(
                    kind="edge", subject=subj, predicate=rel.predicate, object=EntityRef(name=tname, label=rel.target_class),
                    confidence=1.0, extractor="structured", valid_from=vf, valid_to=vt, evidence=ev_list).finalize())
    return out


def _compile_json(ws: Workspace, m: StructuredMapping, name_maps: dict, mappings: dict) -> list[Candidate]:
    rec = ws.load_manifest().source_by_id(m.source)
    if rec is None:
        return []
    doc = load_semistructured(Path(rec.path))
    out: list[Candidate] = []
    objs = _iter_pattern(doc, m.table)
    parent_objs: dict[str, dict] = {}
    if m.parent_relation:
        parent_pattern = m.table.rsplit("/", 2)[0]
        parent_objs = dict(_iter_pattern(doc, parent_pattern))
    parent_name_field = None
    if m.parent_relation:
        for other in mappings.values():
            if other.kind == "json" and other.source == m.source and other.table == m.table.rsplit("/", 2)[0]:
                parent_name_field = other.name_field
    for ptr, obj in objs:
        raw_name = obj.get(m.name_field)
        if raw_name in (None, ""):
            continue
        name = str(raw_name)
        subj = EntityRef(name=name, label=m.entity_class)
        ev = [JsonEvidence(doc_id=m.source, json_pointer=f"{ptr}/{escape_pointer_token(m.name_field)}",
                           raw_value=raw_name, file_sha256=rec.sha256)]
        props = {}
        for key, ptype in m.property_fields.items():
            if key in obj and obj[key] not in (None, ""):
                props[key] = _coerce(obj[key], ptype)
                ev.append(JsonEvidence(doc_id=m.source, json_pointer=f"{ptr}/{escape_pointer_token(key)}",
                                       raw_value=obj[key], file_sha256=rec.sha256))
        if m.id_field and obj.get(m.id_field) is not None:
            props.setdefault("source_key", str(obj.get(m.id_field)))
        out.append(Candidate(kind="node", subject=subj, properties=props, confidence=1.0, evidence=ev,
                             extractor="structured").finalize())
        for rel in m.relations:
            if rel.field not in obj:
                continue
            raw = obj[rel.field]
            items = list(enumerate(raw)) if isinstance(raw, list) else [(None, raw)]
            for idx, val in items:
                if val in (None, ""):
                    continue
                tname = str(val)
                if rel.target_match == "id":
                    tmap = name_maps.get(rel.target_mapping or "", {})
                    if tname not in tmap:
                        continue
                    tname = tmap[tname]
                p = f"{ptr}/{escape_pointer_token(rel.field)}" + (f"/{idx}" if idx is not None else "")
                out.append(Candidate(
                    kind="edge", subject=subj, predicate=rel.predicate, object=EntityRef(name=tname, label=rel.target_class),
                    confidence=1.0, extractor="structured",
                    evidence=[JsonEvidence(doc_id=m.source, json_pointer=p, raw_value=val, file_sha256=rec.sha256)]).finalize())
        if m.parent_relation and parent_name_field:
            parent_ptr = ptr.rsplit("/", 2)[0]
            pobj = parent_objs.get(parent_ptr)
            if pobj and pobj.get(parent_name_field):
                out.append(Candidate(
                    kind="edge", subject=EntityRef(name=str(pobj[parent_name_field]), label=m.parent_relation.parent_class),
                    predicate=m.parent_relation.predicate, object=subj, confidence=1.0, extractor="structured",
                    evidence=[JsonEvidence(doc_id=m.source, json_pointer=f"{ptr}/{escape_pointer_token(m.name_field)}",
                                           raw_value=raw_name, file_sha256=rec.sha256)]).finalize())
    return out


def notebook_title(nb: dict[str, Any], fallback: str) -> tuple[str, int | None, int | None, str | None]:
    for ci, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "markdown":
            continue
        for li, line in enumerate(cell_source(cell).splitlines()):
            mm = re.match(r"^#\s+(.+)$", line.strip())
            if mm:
                return mm.group(1).strip(), ci, li + 1, line.strip()
    return fallback, None, None, None


def _compile_notebook(ws: Workspace, m: StructuredMapping) -> list[Candidate]:
    rec = ws.load_manifest().source_by_id(m.source)
    if rec is None:
        return []
    nb = load_notebook(Path(rec.path))
    title, ci, li, line = notebook_title(nb, Path(rec.path).stem)
    subj = EntityRef(name=title, label=m.entity_class)
    out = []
    ev = []
    if ci is not None:
        ev.append(NotebookEvidence(doc_id=m.source, cell_index=ci, cell_type="markdown", line_span=(li, li),
                                   verbatim_quote=line, file_sha256=rec.sha256))
    out.append(Candidate(kind="node", subject=subj, properties={"cells": len(nb.get("cells", []))}, confidence=1.0,
                         evidence=ev, extractor="structured").finalize())
    rel = next((r for r in m.relations if r.field == "imports"), None)
    if rel is None:
        return out
    for idx, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        for ln, line_text in enumerate(cell_source(cell).splitlines(), start=1):
            mm = IMPORT_RE.match(line_text)
            if not mm:
                continue
            modules = [mm.group(1)] if mm.group(1) else [x.strip() for x in mm.group(2).split(",")]
            for mod in modules:
                lib = mod.split(".")[0]
                nev = NotebookEvidence(doc_id=m.source, cell_index=idx, cell_type="code", line_span=(ln, ln),
                                       verbatim_quote=line_text.strip(), file_sha256=rec.sha256)
                out.append(Candidate(kind="node", subject=EntityRef(name=lib, label=rel.target_class), confidence=1.0,
                                     evidence=[nev], extractor="structured").finalize())
                out.append(Candidate(kind="edge", subject=subj, predicate=rel.predicate,
                                     object=EntityRef(name=lib, label=rel.target_class), confidence=1.0,
                                     evidence=[nev], extractor="structured").finalize())
    return out


def _chunk_for_offset(ws: Workspace, doc_id: str, pos: int):
    best = None
    for ch in ws.load_chunks().values():
        if ch.doc_id == doc_id and ch.char_start <= pos < ch.char_end:
            if best is None or ch.char_start > best.char_start:
                best = ch
    return best


def _line_span(body: str, pos: int) -> tuple[int, int]:
    s = body.rfind("\n", 0, pos) + 1
    e = body.find("\n", pos)
    return s, (len(body) if e == -1 else e)


def _compile_vault(ws: Workspace, m: StructuredMapping) -> list[Candidate]:
    manifest = ws.load_manifest()
    doc_ids = [d for d in m.source.split(",") if d]
    notes: dict[str, dict] = {}
    for d in doc_ids:
        rec = manifest.source_by_id(d)
        if rec is None:
            continue
        note = parse_markdown_note(Path(rec.path))
        fm = note["frontmatter"]
        title = str(fm.get("title") or Path(rec.path).stem)
        info = {"doc_id": d, "rec": rec, "title": title, "class": to_class_name(str(fm.get("type") or fm.get("class") or "Note")),
                "note": note, "aliases": [str(a) for a in _as_list(fm.get("aliases") or fm.get("alias"))]}
        notes[normalize_name(title)] = info
        for a in info["aliases"]:
            notes.setdefault(normalize_name(a), info)
    out: list[Candidate] = []
    for info in {id(v): v for v in notes.values()}.values():
        rec, note, fm = info["rec"], info["note"], info["note"]["frontmatter"]
        subj = EntityRef(name=info["title"], label=info["class"])
        ev = []
        type_key = "type" if "type" in fm else ("class" if "class" in fm else None)
        if type_key:
            ev.append(JsonEvidence(doc_id=info["doc_id"], json_pointer=f"/{type_key}", raw_value=fm[type_key],
                                   file_sha256=rec.sha256))
        props = {}
        for k, t in m.property_fields.items():
            if k in fm and fm[k] not in (None, ""):
                props[k] = _coerce(fm[k], t)
                ev.append(JsonEvidence(doc_id=info["doc_id"], json_pointer=f"/{escape_pointer_token(k)}",
                                       raw_value=fm[k], file_sha256=rec.sha256))
        if not ev:
            ch = _chunk_for_offset(ws, info["doc_id"], 0)
            if ch is not None:
                first_line = ch.text.splitlines()[0].strip() if ch.text.strip() else ""
                if first_line:
                    ev.append(TextEvidence(doc_id=info["doc_id"], chunk_id=ch.chunk_id, page_no=None,
                                           char_start=ch.char_start, char_end=ch.char_start + len(ch.text.splitlines()[0]),
                                           verbatim_quote=first_line, chunk_sha256=ch.sha256))
        out.append(Candidate(kind="node", subject=subj, aliases=info["aliases"], properties=props, confidence=1.0,
                             evidence=ev, extractor="structured").finalize())
        body = note["body"]
        field_spans = [(fl["char_start"], fl["char_end"], fl["key"]) for fl in note["inline_fields"]]
        for link in note["wikilinks"]:
            pred = "LINKS_TO"
            for s, e, key in field_spans:
                if s <= link["char_start"] < e:
                    pred = to_predicate(key)
            tgt = notes.get(normalize_name(link["target"]))
            tname = tgt["title"] if tgt else link["target"]
            tcls = tgt["class"] if tgt else "Note"
            ch = _chunk_for_offset(ws, info["doc_id"], link["char_start"])
            if ch is None:
                continue
            ls, le = _line_span(body, link["char_start"])
            ls, le = max(ls, ch.char_start), min(le, ch.char_end)
            quote = body[ls:le].strip()
            out.append(Candidate(
                kind="edge", subject=subj, predicate=pred, object=EntityRef(name=tname, label=tcls), confidence=1.0,
                extractor="structured",
                evidence=[TextEvidence(doc_id=info["doc_id"], chunk_id=ch.chunk_id, page_no=None, char_start=ls,
                                       char_end=le, verbatim_quote=quote, chunk_sha256=ch.sha256)]).finalize())
    return out

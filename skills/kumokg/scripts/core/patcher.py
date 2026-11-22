"""Atomic graph mutation: Gate 2 decisions + commit, and bi-temporal chat patches.

* ``record_decisions`` stores Gate 2 decisions and turns them into active-learning calibration rules
  (negative examples, predicate corrections, direction corrections, alias merges).
* ``commit`` upserts approved proposals into ``graph.json`` + ``graph.sqlite`` (never deletes, never commits
  quarantined/ungrounded proposals) and extends a closed-world ontology only for user-approved extensions.
* ``apply_patch`` executes ``add-node``, ``merge-nodes``, ``add-edge``, ``supersede-edge``, ``retract-edge`` and
  ``add-alias`` on a copy of the graph, verifies every evidence item, then writes everything atomically,
  appends ``changelog.jsonl`` and notifies a running Studio over SSE.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .ir import (
    Edge, EntityRef, Graph, Node, Ontology, RelationRule, Workspace, append_jsonl, commit_graph, evidence_key,
    make_edge_id, make_node_id, normalize_name, parse_evidence, read_json, sha256_text, to_class_name, to_predicate,
    utcnow, utcnow_iso, write_json,
)
from .verifier import (  # noqa: F401  (contradiction_resolved is re-exported for the Studio)
    GROUNDED, EntityResolver, VerifyContext, check_ontology, contradiction_resolved, display_name, verify_evidence,
)

DECISIONS = ("approve", "reject", "resolved")

NODE_EVIDENCE_CAP = 12


# --------------------------------------------------------------------------------------------
# Studio notification + changelog
# --------------------------------------------------------------------------------------------


def notify_studio(ws: Workspace, event: dict[str, Any]) -> bool:
    info = read_json(ws.studio_path, None)
    if not info or not info.get("port"):
        return False
    import httpx

    try:
        r = httpx.post(f"http://127.0.0.1:{info['port']}/api/notify", json=event, timeout=1.0,
                       headers={"X-KumoKG-Token": info.get("token", "")})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def log_change(ws: Workspace, entries: list[dict[str, Any]]) -> list[str]:
    ids = []
    rows = []
    for e in entries:
        cid = "chg-" + secrets.token_hex(6)
        ids.append(cid)
        rows.append({"change_id": cid, "ts": utcnow_iso(), **e})
    append_jsonl(ws.changelog_path, rows)
    return ids


# --------------------------------------------------------------------------------------------
# Gate 2 decisions
# --------------------------------------------------------------------------------------------


def _proposal_index(ws: Workspace) -> dict[str, dict[str, Any]]:
    rep = read_json(ws.verify_path, {}) or {}
    idx = {p["proposal_id"]: p for p in rep.get("proposals", [])}
    idx.update({p["proposal_id"]: p for p in rep.get("node_proposals", [])})
    return idx


def record_decisions(ws: Workspace, decisions: list[dict[str, Any]], by: str = "gate2") -> dict[str, Any]:
    ws.require()
    props = _proposal_index(ws)
    stored = ws.load_decisions()
    cal = ws.load_calibration()
    applied, unknown = 0, []
    rules = {"negative_examples": 0, "predicate_corrections": 0, "direction_corrections": 0, "alias_merges": 0}
    for d in decisions:
        pid = d.get("proposal_id")
        decision = d.get("decision")
        if pid not in props or decision not in DECISIONS:
            unknown.append(pid)
            continue
        p = props[pid]
        edit = d.get("edit") or {}
        stored[pid] = {"decision": decision, "edit": edit, "reason": d.get("reason", ""), "decided_at": utcnow_iso(), "by": by}
        applied += 1
        if p["kind"] != "edge" or decision == "resolved":
            continue
        quote = next((e.get("verbatim_quote") or e.get("element_description") or "" for e in p.get("evidence", [])), "")
        if decision == "reject":
            cal["negative_examples"].append({"proposal_id": pid, "subject": p["source"]["name"], "predicate": p["predicate"],
                                             "object": p["target"]["name"], "quote": quote[:240],
                                             "reason": d.get("reason") or "rejected at Gate 2"})
            rules["negative_examples"] += 1
            continue
        new_pred = edit.get("predicate")
        if new_pred and to_predicate(new_pred) != p["predicate"]:
            cal["predicate_corrections"].append({"from": p["predicate"], "to": to_predicate(new_pred),
                                                 "source_class": p["source"]["label"], "target_class": p["target"]["label"]})
            rules["predicate_corrections"] += 1
        if edit.get("flip"):
            cal["direction_corrections"].append({"predicate": to_predicate(new_pred or p["predicate"]),
                                                 "source_class": p["source"]["label"], "target_class": p["target"]["label"]})
            rules["direction_corrections"] += 1
        for side in ("source", "target"):
            new_name = edit.get(f"{side}_name")
            if new_name and normalize_name(new_name) != normalize_name(p[side]["name"]):
                cal["alias_merges"].append({"alias": p[side]["name"], "canonical": new_name, "label": p[side]["label"]})
                rules["alias_merges"] += 1
    for key in ("negative_examples", "predicate_corrections", "direction_corrections"):
        seen, uniq = set(), []
        for r in cal[key]:
            k = json.dumps({kk: vv for kk, vv in r.items() if kk not in ("proposal_id", "quote", "reason")}, sort_keys=True)
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        cal[key] = uniq
    write_json(ws.decisions_path, stored)
    write_json(ws.calibration_path, cal)
    m = ws.load_manifest()
    m.set_phase("decided")
    ws.save_manifest(m)
    return {"applied": applied, "unknown": [u for u in unknown if u], "calibration_rules_added": rules,
            "decisions_total": len(stored)}


def auto_decide(ws: Workspace, buckets: tuple[str, ...] = ("auto",), by: str = "auto") -> dict[str, Any]:
    rep = read_json(ws.verify_path, {}) or {}
    existing = ws.load_decisions()
    todo = [{"proposal_id": p["proposal_id"], "decision": "approve"} for p in rep.get("proposals", [])
            if p["bucket"] in buckets and p["proposal_id"] not in existing]
    return record_decisions(ws, todo, by=by)


# --------------------------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------------------------


def _merge_evidence(target: list, extra: list, cap: int | None = None) -> int:
    seen = {evidence_key(e) for e in target}
    added = 0
    for e in extra:
        if cap is not None and len(target) >= cap:
            break
        k = evidence_key(e)
        if k not in seen:
            target.append(e)
            seen.add(k)
            added += 1
    return added


def commit(ws: Workspace, by: str = "gate2") -> dict[str, Any]:
    ws.require()
    ont = ws.require_ontology()
    rep = read_json(ws.verify_path, {}) or {}
    decisions = ws.load_decisions()
    graph = ws.load_graph()
    before_hash = graph.content_hash()
    nodes = graph.node_map()
    edges = graph.edge_map()
    node_props = {g["node_id"]: g for g in rep.get("node_proposals", [])}
    stats = {"nodes_added": 0, "nodes_updated": 0, "edges_added": 0, "edges_merged": 0, "ontology_extensions": 0,
             "skipped_quarantined": 0}
    ontology_changed = False

    def ensure_node(nid: str, label: str, name: str) -> Node:
        np = node_props.get(nid)
        ev = [parse_evidence(e) for e in (np["evidence"] if np else [])]
        if nid in nodes:
            n = nodes[nid]
            if np:
                n.aliases = sorted(set(n.aliases) | set(np["aliases"]))
                for k, v in np["properties"].items():
                    n.properties.setdefault(k, v)
                if _merge_evidence(n.evidence, ev, NODE_EVIDENCE_CAP):
                    stats["nodes_updated"] += 1
            return n
        props = dict(np["properties"]) if np else {}
        if np and len(np["evidence"]) > NODE_EVIDENCE_CAP:
            props["mention_count"] = len(np["evidence"])
        n = Node(id=nid, labels=[label], canonical_name=name, aliases=np["aliases"] if np else [], properties=props,
                 confidence=1.0 if (np and np["structured"]) else 0.9, evidence=ev[:NODE_EVIDENCE_CAP])
        nodes[nid] = n
        stats["nodes_added"] += 1
        return n

    for np in node_props.values():
        d = decisions.get(np["proposal_id"], {})
        if np["bucket"] == "quarantine" or d.get("decision") == "reject":
            continue
        if (np["structured"] and np["bucket"] == "auto") or d.get("decision") == "approve":
            ensure_node(np["node_id"], np["label"], np["name"])

    for p in rep.get("proposals", []):
        d = decisions.get(p["proposal_id"])
        if not d or d["decision"] != "approve":
            continue
        if p["bucket"] == "quarantine":
            stats["skipped_quarantined"] += 1
            continue
        edit = d.get("edit") or {}
        src, tgt = dict(p["source"]), dict(p["target"])
        pred = to_predicate(edit.get("predicate") or p["predicate"])
        for side, ref in (("source", src), ("target", tgt)):
            new_name = edit.get(f"{side}_name")
            if new_name:
                ref["name"] = display_name(new_name)
                ref["id"] = make_node_id(ref["label"], ref["name"])
        if edit.get("flip"):
            src, tgt = tgt, src
        if not ont.allows(src["label"], pred, tgt["label"]):
            for cls in (src["label"], tgt["label"]):
                if ont.get_class(cls) is None:
                    ont.ensure_class(cls, "added by approved schema extension at Gate 2")
            ont.relations.append(RelationRule(source_class=src["label"], predicate=pred, target_class=tgt["label"],
                                              description=f"approved schema extension ({p['proposal_id']})"))
            ontology_changed = True
            stats["ontology_extensions"] += 1
        ensure_node(src["id"], src["label"], src["name"])
        ensure_node(tgt["id"], tgt["label"], tgt["name"])
        eid = make_edge_id(src["id"], pred, tgt["id"], p.get("valid_from"))
        checks = p.get("evidence_checks") or [{"status": "verified"}] * len(p["evidence"])
        exact = [e for e, c in zip(p["evidence"], checks) if c["status"] in ("verified", "needs_visual")]
        ev = [parse_evidence(e) for e in (exact or p["evidence"])]
        if eid in edges:
            if _merge_evidence(edges[eid].evidence, ev):
                stats["edges_merged"] += 1
            edges[eid].confidence = max(edges[eid].confidence, p["confidence"])
        else:
            edges[eid] = Edge(id=eid, source_id=src["id"], target_id=tgt["id"], type=pred, confidence=p["confidence"],
                              valid_from=p.get("valid_from"), valid_to=p.get("valid_to"), status="approved", evidence=ev,
                              properties={"origin": ",".join(p.get("extractors", [])), "proposal_id": p["proposal_id"],
                                          "sources": len(p.get("docs", []))})
            stats["edges_added"] += 1
    graph.nodes = list(nodes.values())
    graph.edges = list(edges.values())
    if ontology_changed:
        write_json(ws.ontology_approved_path, ont)
    counts = commit_graph(ws, graph)
    after_hash = graph.content_hash()
    log_change(ws, [{"author": by, "op": "commit", "message": "Gate 2 commit", "stats": stats,
                     "graph_hash_before": before_hash, "graph_hash_after": after_hash}])
    m = ws.load_manifest()
    m.gates["gate2"] = "committed"
    m.set_phase("committed")
    ws.save_manifest(m)
    notify_studio(ws, {"type": "graph_changed", "reason": "commit", "graph_hash": after_hash})
    return {**stats, **{f"graph_{k}": v for k, v in counts.items()}, "graph_hash": after_hash}


# --------------------------------------------------------------------------------------------
# Chat patches
# --------------------------------------------------------------------------------------------


class PatchOp(BaseModel):
    model_config = ConfigDict(extra="allow")
    op: Literal["add-node", "merge-nodes", "add-edge", "supersede-edge", "retract-edge", "add-alias"]


class PatchDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ops: list[PatchOp]
    author: str = "chat"
    message: str = ""
    allow_extension: bool = False
    allow_unverified: bool = False


class PatchError(Exception):
    pass


class _Patcher:
    def __init__(self, ws: Workspace, graph: Graph, ont: Ontology, allow_extension: bool, allow_unverified: bool):
        from .worker_pool import SourceIndex

        self.ws = ws
        self.graph = graph
        self.ont = ont
        self.nodes = graph.node_map()
        self.edges = graph.edge_map()
        self.allow_extension = allow_extension
        self.allow_unverified = allow_unverified
        self.ctx = VerifyContext(ws)
        self.sidx = SourceIndex(ws)
        self.resolver = EntityResolver(ont, ws.load_calibration(), graph)
        self.ontology_changed = False
        self.log: list[dict[str, Any]] = []

    # evidence --------------------------------------------------------------------------
    def evidence(self, op: dict[str, Any]) -> list[Any]:
        from .worker_pool import make_text_evidence

        raw = op.get("evidence") or []
        out = [parse_evidence(e) for e in raw]
        if op.get("quote"):
            cid = op.get("chunk_id") or "auto"
            chunk = self.sidx.chunks.get(cid) if cid != "auto" else self.sidx.locate_chunk(op["quote"], op.get("doc_id"))
            if chunk is None:
                raise PatchError(f"quote not found in any chunk: {op['quote'][:80]!r}")
            out.append(make_text_evidence(self.sidx, chunk, op["quote"]))
        checks = [verify_evidence(e, self.ctx) for e in out]
        bad = [c["detail"] for c in checks if c["status"] not in GROUNDED]
        if bad:
            raise PatchError("evidence failed verification: " + "; ".join(bad))
        if not out and not self.allow_unverified:
            raise PatchError("patch needs evidence (quote + chunk_id, or typed evidence); "
                             "pass allow_unverified=true to record an unverified user assertion")
        return out

    # node helpers ----------------------------------------------------------------------
    def node_ref(self, ref: Any, create: bool = False, evidence: list | None = None) -> Node:
        if isinstance(ref, str):
            if ref in self.nodes:
                return self.nodes[ref]
            raise PatchError(f"unknown node id {ref!r}")
        er = EntityRef(name=ref["name"], label=to_class_name(ref["label"]) if ref.get("label") else None)
        r = self.resolver.resolve(er)
        if r["id"] in self.nodes:
            return self.nodes[r["id"]]
        if not create:
            raise PatchError(f"node {ref} does not exist (use add-node first or set create=true)")
        return self._new_node(r["label"], r["name"], [], {}, evidence or [])

    def _new_node(self, label: str, name: str, aliases: list[str], props: dict, evidence: list) -> Node:
        label = to_class_name(label)
        self._check_class(label)
        nid = make_node_id(label, name)
        if nid in self.nodes:
            raise PatchError(f"node {nid} already exists")
        n = Node(id=nid, labels=[label], canonical_name=display_name(name), aliases=aliases, properties=props,
                 confidence=1.0, evidence=evidence[:NODE_EVIDENCE_CAP])
        self.nodes[nid] = n
        self.resolver.register(label, n.canonical_name, aliases)
        self.log.append({"op": "add-node", "after": {"id": nid, "label": label, "name": n.canonical_name}})
        return n

    def _check_class(self, label: str) -> None:
        if self.ont.get_class(label) is None:
            if self.ont.closed_world and not self.allow_extension:
                raise PatchError(f"class {label} is not in the closed-world ontology (set allow_extension=true)")
            self.ont.ensure_class(label, "added by chat patch")
            self.ontology_changed = True

    def _check_triple(self, s: Node, pred: str, t: Node) -> None:
        res = check_ontology("edge", self.ont, s.label, pred, t.label)
        if res["status"] == "ok":
            return
        if res["status"] == "flip_suggested":
            raise PatchError(f"{res['detail']} (swap source and target)")
        if self.ont.closed_world and not self.allow_extension:
            raise PatchError(f"{res.get('detail')} (closed-world ontology; set allow_extension=true)")
        self.ont.ensure_relation(s.label, pred, t.label, description="added by chat patch")
        self.ontology_changed = True

    def _edge(self, op: dict[str, Any], src: Node, tgt: Node, pred: str, valid_from: str | None,
              valid_to: str | None, evidence: list) -> Edge:
        self._check_triple(src, pred, tgt)
        eid = make_edge_id(src.id, pred, tgt.id, valid_from)
        if eid in self.edges and self.edges[eid].status == "approved":
            raise PatchError(f"edge {eid} already exists")
        props = dict(op.get("properties") or {})
        props["origin"] = "patch"
        if not evidence:
            props["unverified_user_assertion"] = True
        e = Edge(id=eid, source_id=src.id, target_id=tgt.id, type=pred, confidence=float(op.get("confidence", 1.0)),
                 valid_from=valid_from, valid_to=valid_to, status="approved", evidence=evidence, properties=props)
        self.edges[eid] = e
        return e

    # ops ---------------------------------------------------------------------------------
    def apply(self, op: dict[str, Any]) -> dict[str, Any]:
        kind = op["op"]
        if kind == "add-node":
            ev = self.evidence(op)
            n = self._new_node(op["label"], op["name"], list(op.get("aliases") or []), dict(op.get("properties") or {}), ev)
            return {"op": kind, "node_id": n.id}
        if kind == "add-alias":
            n = self.node_ref(op["node_id"])
            alias = op["alias"].strip()
            if alias and alias not in n.aliases and normalize_name(alias) != normalize_name(n.canonical_name):
                n.aliases.append(alias)
                self.resolver.register(n.label, n.canonical_name, [alias])
            self.log.append({"op": kind, "after": {"id": n.id, "alias": alias}})
            return {"op": kind, "node_id": n.id, "aliases": n.aliases}
        if kind == "merge-nodes":
            keep = self.node_ref(op["keep"])
            merged = []
            for mid in op["merge"]:
                if mid == keep.id:
                    continue
                other = self.node_ref(mid)
                keep.aliases = sorted(set(keep.aliases) | set(other.aliases) | {other.canonical_name})
                for k, v in other.properties.items():
                    keep.properties.setdefault(k, v)
                _merge_evidence(keep.evidence, other.evidence, NODE_EVIDENCE_CAP)
                for eid, e in list(self.edges.items()):
                    if other.id not in (e.source_id, e.target_id):
                        continue
                    s = keep.id if e.source_id == other.id else e.source_id
                    t = keep.id if e.target_id == other.id else e.target_id
                    del self.edges[eid]
                    nid = make_edge_id(s, e.type, t, e.valid_from)
                    if nid in self.edges:
                        _merge_evidence(self.edges[nid].evidence, e.evidence)
                    else:
                        self.edges[nid] = e.model_copy(update={"id": nid, "source_id": s, "target_id": t})
                    for x in self.edges.values():
                        if x.superseded_by == eid:
                            x.superseded_by = nid
                del self.nodes[other.id]
                merged.append(other.id)
            self.log.append({"op": kind, "after": {"keep": keep.id, "merged": merged}})
            return {"op": kind, "keep": keep.id, "merged": merged}
        if kind == "add-edge":
            ev = self.evidence(op)
            src = self.node_ref(op["source"], create=bool(op.get("create")), evidence=ev)
            tgt = self.node_ref(op["target"], create=bool(op.get("create")), evidence=ev)
            pred = to_predicate(op["type"])
            e = self._edge(op, src, tgt, pred, op.get("valid_from"), op.get("valid_to"), ev)
            self.log.append({"op": kind, "after": e.model_dump(mode="json", exclude={"evidence"})})
            return {"op": kind, "edge_id": e.id}
        if kind == "supersede-edge":
            old = self.edges.get(op["edge_id"])
            if old is None:
                raise PatchError(f"unknown edge {op['edge_id']}")
            if old.status != "approved" or old.valid_to is not None:
                raise PatchError(f"edge {old.id} is not currently active")
            vf = op.get("valid_from")
            if not vf:
                raise PatchError("supersede-edge needs valid_from (the instant the new fact starts)")
            if old.valid_from and vf <= old.valid_from:
                raise PatchError(f"valid_from {vf} must be after the old edge's valid_from {old.valid_from}")
            new = op.get("new") or {}
            ev = self.evidence(op)
            src = self.node_ref(new["source"], create=bool(new.get("create")), evidence=ev) if new.get("source") else self.nodes[old.source_id]
            tgt = self.node_ref(new["target"], create=bool(new.get("create")), evidence=ev) if new.get("target") else self.nodes[old.target_id]
            pred = to_predicate(new.get("type") or old.type)
            before = old.model_dump(mode="json", exclude={"evidence"})
            e = self._edge({**op, "properties": new.get("properties")}, src, tgt, pred, vf, new.get("valid_to"), ev)
            old.valid_to = vf
            old.superseded_by = e.id
            old.status = "superseded"
            self.log.append({"op": kind, "before": before, "after": {"superseded": old.id, "new_edge": e.id, "valid_from": vf}})
            return {"op": kind, "superseded": old.id, "new_edge_id": e.id}
        if kind == "retract-edge":
            e = self.edges.get(op["edge_id"])
            if e is None:
                raise PatchError(f"unknown edge {op['edge_id']}")
            if e.status != "approved":
                raise PatchError(f"edge {e.id} is not active")
            before = e.model_dump(mode="json", exclude={"evidence"})
            e.valid_to = op.get("valid_to") or utcnow().date().isoformat()
            e.status = "superseded"
            e.properties["retracted"] = True
            e.properties["retract_reason"] = op.get("reason", "")
            self.log.append({"op": kind, "before": before, "after": {"id": e.id, "valid_to": e.valid_to}})
            return {"op": kind, "edge_id": e.id, "valid_to": e.valid_to}
        raise PatchError(f"unsupported op {kind}")


def apply_patch(ws: Workspace, patch: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    ws.require()
    doc = PatchDoc.model_validate(patch)
    ont = ws.require_ontology()
    graph = Graph.model_validate(json.loads(ws.load_graph().model_dump_json()))
    before_hash = graph.content_hash()
    p = _Patcher(ws, graph, ont, doc.allow_extension, doc.allow_unverified)
    results = []
    for i, op in enumerate(doc.ops):
        try:
            results.append(p.apply(op.model_dump()))
        except (PatchError, KeyError, ValueError) as exc:
            msg = str(exc) if not isinstance(exc, KeyError) else f"missing field {exc}"
            return {"ok": False, "error": f"op {i} ({op.op}): {msg}", "applied": [], "dry_run": dry_run}
    graph.nodes = list(p.nodes.values())
    graph.edges = list(p.edges.values())
    dangling = [e.id for e in graph.edges if e.source_id not in p.nodes or e.target_id not in p.nodes]
    if dangling:
        return {"ok": False, "error": f"patch would leave dangling edges: {dangling[:5]}", "applied": [], "dry_run": dry_run}
    after_hash = graph.content_hash()
    if dry_run:
        return {"ok": True, "dry_run": True, "applied": results, "graph_hash_after": after_hash}
    if p.ontology_changed:
        write_json(ws.ontology_approved_path, ont)
    counts = commit_graph(ws, graph)
    ids = log_change(ws, [{"author": doc.author, "message": doc.message, "graph_hash_before": before_hash,
                           "graph_hash_after": after_hash, **entry} for entry in p.log])
    notified = notify_studio(ws, {"type": "graph_changed", "reason": "patch", "graph_hash": after_hash,
                                  "ops": [r["op"] for r in results], "change_ids": ids})
    return {"ok": True, "dry_run": False, "applied": results, "change_ids": ids, "graph_hash": after_hash,
            "graph": counts, "studio_notified": notified, "ontology_extended": p.ontology_changed}


# --------------------------------------------------------------------------------------------
# Contradiction resolution (Gate 2)
# --------------------------------------------------------------------------------------------


def _edge_for_ref(graph: Graph, ref: str, props: dict[str, Any], source_id: str, predicate: str) -> Edge | None:
    edges = graph.edge_map()
    if ref in edges:
        return edges[ref]
    by_prop = next((e for e in graph.edges if (e.properties or {}).get("proposal_id") == ref), None)
    if by_prop is not None:
        return by_prop
    p = props.get(ref)
    if p is None:
        return None
    return next((e for e in graph.edges if e.source_id == source_id and e.type == predicate
                 and e.target_id == p["target"]["id"] and e.status == "approved"), None)


def resolve_contradiction(ws: Workspace, contradiction_id: str, action: str, keep: str | None = None,
                          order: list[str] | None = None, switch_at: str | None = None, by: str = "gate2") -> dict[str, Any]:
    """Resolve a functional contradiction.

    ``keep``: approve ``keep``, reject every other candidate value and retract other committed edges.
    ``sequence``: keep both values as a bi-temporal sequence — the earlier value is committed (if needed) and superseded
    by the later value at ``switch_at`` using the later proposal's verified evidence (or, when that sequence already
    exists in the graph, the later proposal's evidence is merged into it as corroboration).
    """
    ws.require()
    rep = read_json(ws.verify_path, {}) or {}
    c = next((x for x in rep.get("contradictions", []) if x["contradiction_id"] == contradiction_id), None)
    if c is None:
        raise KeyError(f"unknown contradiction {contradiction_id!r}")
    props = {p["proposal_id"]: p for p in rep.get("proposals", [])}
    refs = [v["ref"] for v in c["values"]]
    steps: list[dict[str, Any]] = []
    if action == "keep":
        if keep not in refs:
            raise ValueError("'keep' must be one of the contradiction's values")
        graph = ws.load_graph()
        decisions, retract = [], []
        for r in refs:
            if r in props:
                decisions.append({"proposal_id": r, "decision": "approve" if r == keep else "reject",
                                  "reason": f"contradiction {contradiction_id}: kept {keep}"})
            elif r != keep:
                e = graph.edge_map().get(r)
                if e is not None and e.status == "approved":
                    retract.append(r)
        if decisions:
            steps.append({"decisions": record_decisions(ws, decisions, by=by)})
        if retract:
            res = apply_patch(ws, {"author": by, "message": f"contradiction {contradiction_id}: retract conflicting value",
                                   "ops": [{"op": "retract-edge", "edge_id": r, "reason": "contradiction resolved at Gate 2"} for r in retract]})
            if not res["ok"]:
                raise ValueError(res["error"])
            steps.append({"retracted": retract})
        if keep in props:
            steps.append({"commit": commit(ws, by=by)})
    elif action == "sequence":
        if c["kind"] != "functional_relation":
            raise ValueError("temporal sequences apply to functional relations, not properties")
        if not order or len(order) != 2 or set(order) - set(refs) or order[0] == order[1]:
            raise ValueError("'order' must list two distinct values of the contradiction: [earlier, later]")
        if not switch_at:
            raise ValueError("'switch_at' (ISO date or datetime when the later value starts) is required")
        earlier, later = order
        graph = ws.load_graph()
        e_edge = _edge_for_ref(graph, earlier, props, c["source_id"], c["predicate"])
        if e_edge is None and earlier in props:
            record_decisions(ws, [{"proposal_id": earlier, "decision": "approve", "reason": "earlier value of a temporal sequence"}], by=by)
            steps.append({"commit": commit(ws, by=by)})
            graph = ws.load_graph()
            e_edge = _edge_for_ref(graph, earlier, props, c["source_id"], c["predicate"])
        if e_edge is None:
            raise ValueError("the earlier value could not be committed")
        lp = props.get(later)
        later_target = lp["target"] if lp else {"id": graph.edge_map()[later].target_id}
        evidence = [ev for ev, chk in zip(lp["evidence"], lp.get("evidence_checks") or [{"status": "verified"}] * len(lp["evidence"]))
                    if chk["status"] in GROUNDED] if lp else []
        existing = next((e for e in graph.edges if e.source_id == e_edge.source_id and e.type == e_edge.type
                         and e.target_id == later_target["id"] and e.status == "approved"), None)
        if existing is not None and e_edge.superseded_by == existing.id:
            before = graph.content_hash()
            added = _merge_evidence(existing.evidence, [parse_evidence(x) for x in evidence])
            commit_graph(ws, graph)
            log_change(ws, [{"author": by, "op": "merge-evidence", "message": f"contradiction {contradiction_id}: corroborates "
                             f"the existing sequence {e_edge.id} -> {existing.id}", "after": {"edge_id": existing.id, "evidence_added": added},
                             "graph_hash_before": before, "graph_hash_after": graph.content_hash()}])
            steps.append({"already_sequenced": existing.id, "evidence_added": added})
        elif existing is not None and e_edge.status == "approved" and e_edge.valid_to is None:
            if e_edge.valid_from and switch_at <= e_edge.valid_from:
                raise ValueError(f"switch_at {switch_at} must be after the earlier value's valid_from {e_edge.valid_from}")
            before_hash = graph.content_hash()
            before = e_edge.model_dump(mode="json", exclude={"evidence"})
            e_edge.valid_to, e_edge.superseded_by, e_edge.status = switch_at, existing.id, "superseded"
            existing.valid_from = existing.valid_from or switch_at
            _merge_evidence(existing.evidence, [parse_evidence(x) for x in evidence])
            commit_graph(ws, graph)
            log_change(ws, [{"author": by, "op": "supersede-edge", "message": f"contradiction {contradiction_id}: sequence two "
                             f"committed values at {switch_at}", "before": before,
                             "after": {"superseded": e_edge.id, "new_edge": existing.id, "valid_from": switch_at},
                             "graph_hash_before": before_hash, "graph_hash_after": graph.content_hash()}])
            steps.append({"superseded": e_edge.id, "new_edge_id": existing.id, "valid_from": switch_at})
        elif e_edge.status == "approved" and e_edge.valid_to is None:
            nodes = graph.node_map()
            target = later_target["id"] if later_target["id"] in nodes else {"name": later_target["name"], "label": later_target["label"]}
            res = apply_patch(ws, {"author": by, "message": f"contradiction {contradiction_id}: temporal sequence at {switch_at}",
                                   "ops": [{"op": "supersede-edge", "edge_id": e_edge.id, "valid_from": switch_at,
                                            "new": {"target": target, "create": True}, "evidence": evidence}]})
            if not res["ok"]:
                raise ValueError(res["error"])
            steps.append({"superseded": e_edge.id, "new_edge_id": res["applied"][0]["new_edge_id"], "valid_from": switch_at})
        else:
            raise ValueError(f"the earlier value ({e_edge.id}) is no longer active and is not followed by the later value")
        marks = []
        if lp:
            marks.append({"proposal_id": later, "decision": "resolved",
                          "edit": {"resolution": "temporal_sequence", "switch_at": switch_at, "after": earlier}})
        if earlier in props and earlier not in ws.load_decisions():
            marks.append({"proposal_id": earlier, "decision": "approve"})
        if marks:
            record_decisions(ws, marks, by=by)
    else:
        raise ValueError("action must be 'keep' or 'sequence'")
    graph = ws.load_graph()
    resolved = contradiction_resolved(c, ws.load_decisions(), graph.edge_map())
    notify_studio(ws, {"type": "graph_changed", "reason": "contradiction_resolved", "graph_hash": graph.content_hash()})
    return {"ok": True, "contradiction_id": contradiction_id, "action": action, "resolved": resolved, "steps": steps,
            "graph_hash": graph.content_hash()}


def history(ws: Workspace, edge_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from .ir import iter_jsonl

    rows = list(iter_jsonl(ws.changelog_path))
    if edge_id:
        rows = [r for r in rows if edge_id in json.dumps(r)]
    return rows[-limit:]

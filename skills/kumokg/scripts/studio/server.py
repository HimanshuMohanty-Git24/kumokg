"""KumoKG Studio: zero-build local HTTP + Server-Sent-Events server for Gate 1, Gate 2, the Explorer and exports.

Binds to 127.0.0.1 only, rejects foreign Host/Origin headers (DNS-rebinding / CSRF hardening), serves the static
pages from ``assets/studio`` and media thumbnails from ``.kumokg/media`` with path-traversal protection.
``GET /api/events`` streams ``graph_changed`` / gate events; mutations from ``kumokg patch`` or ``commit`` arrive via
``POST /api/notify`` (token-authenticated) and a ``graph.json`` mtime watcher as a fallback.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from core.ir import Workspace, read_json, utcnow_iso, write_json

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets" / "studio"
PAGES = {"/gate1": "ontology-review.html", "/gate2": "edge-review.html", "/explorer": "explorer.html"}
MAX_BODY = 8 * 1024 * 1024


class StudioState:
    def __init__(self, ws: Workspace, gate: str | None = None, blocking: bool = False):
        self.ws = ws
        self.gate = gate
        self.blocking = blocking
        self.token = secrets.token_hex(16)
        self.done = threading.Event()
        self.result: dict[str, Any] = {}
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.port = 0
        self.last_hash: str | None = None
        self._doctor_cache: tuple[str, dict] | None = None
        self.events_sent = 0
        self.client_log: list[dict[str, Any]] = []

    def broadcast(self, event: dict[str, Any]) -> int:
        event = {"ts": utcnow_iso(), **event}
        if event.get("type") == "graph_changed":
            if event.get("graph_hash") and event["graph_hash"] == self.last_hash and event.get("reason") == "watch":
                return 0
            self.last_hash = event.get("graph_hash") or self.last_hash
            self._doctor_cache = None
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            q.put(event)
        self.events_sent += 1
        return len(clients)

    def finish(self, result: dict[str, Any]) -> None:
        self.result = result
        if self.blocking:
            self.done.set()


# --------------------------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------------------------


def mapping_preview(ws: Workspace, ont) -> list[dict[str, Any]]:
    from core.modality_router import load_semistructured, read_table
    from core.structured_compiler import _iter_pattern

    m = ws.load_manifest()
    out = []
    for mp in ont.structured_mappings:
        rec = None
        if mp.kind in ("table", "json", "notebook"):
            rec = m.source_by_id(mp.source)
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        try:
            if mp.kind == "table" and rec:
                columns, all_rows = read_table(Path(rec.path), mp.table)
                rows = all_rows[:3]
            elif mp.kind == "json" and rec:
                objs = _iter_pattern(load_semistructured(Path(rec.path)), mp.table)[:3]
                rows = [{k: (v if not isinstance(v, (dict, list)) else json.dumps(v)[:60]) for k, v in o.items()} for _, o in objs]
                columns = sorted({k for r in rows for k in r})
        except Exception as exc:  # preview must never break Gate 1
            rows = [{"error": str(exc)}]
            columns = ["error"]
        out.append({"mapping_id": mp.mapping_id, "kind": mp.kind, "source_name": rec.name if rec else mp.source,
                    "columns": columns, "rows": [{c: (str(r.get(c))[:80] if r.get(c) is not None else "") for c in columns} for r in rows]})
    return out


def _context(text: str, a: int, b: int, n: int) -> dict[str, str]:
    """Context window around a match, trimmed to whole words."""
    s, e = max(0, a - n), min(len(text), b + n)
    before, after = text[s:a], text[b:e]
    if s > 0 and " " in before:
        before = before.split(" ", 1)[1]
    if e < len(text) and " " in after:
        after = after.rsplit(" ", 1)[0]
    return {"before": before, "match": text[a:b], "after": after}


def enrich_evidence(ws: Workspace, ev: dict[str, Any], ctx, docs: dict[str, str], context_chars: int = 220) -> dict[str, Any]:
    from core.modality_router import cell_source, load_semistructured, resolve_pointer
    from core.verifier import verify_evidence
    from core.ir import parse_evidence
    from core.worker_pool import find_span

    d = dict(ev)
    d["source_name"] = docs.get(ev["doc_id"], ev["doc_id"])
    try:
        d["check"] = verify_evidence(parse_evidence(ev), ctx)
    except Exception as exc:
        d["check"] = {"status": "rejected", "detail": str(exc)}
    kind = ev["kind"]
    try:
        if kind == "text":
            ch = ctx.chunks.get(ev["chunk_id"])
            if ch:
                span = find_span(ch.text, ev["verbatim_quote"])
                if span:
                    d["context"] = _context(ch.text, span[0], span[1], context_chars)
                d["page_or_span"] = ch.page_or_span
        elif kind == "web":
            text = ctx.web_text(ev["doc_id"])
            span = find_span(text, ev["verbatim_quote"])
            if span:
                d["context"] = _context(text, span[0], span[1], context_chars)
        elif kind == "table":
            rows = ctx.table_rows(ev["doc_id"], ev["sheet_or_table"])
            row = rows[ev["row_index"]] if 0 <= ev["row_index"] < len(rows) else {}
            d["row"] = {"columns": list(row.keys()), "values": [str(v) if v is not None else "" for v in row.values()]}
        elif kind == "json":
            doc = ctx.semi_doc(ev["doc_id"])
            parent_ptr = ev["json_pointer"].rsplit("/", 1)[0]
            parent = resolve_pointer(doc, parent_ptr) if parent_ptr else doc
            d["parent"] = json.dumps(parent, ensure_ascii=False, default=str, indent=1)[:900]
            d["path"] = [p for p in ev["json_pointer"].split("/")[1:]]
        elif kind == "notebook":
            cells = ctx.notebook(ev["doc_id"]).get("cells", [])
            if 0 <= ev["cell_index"] < len(cells):
                d["cell_source"] = cell_source(cells[ev["cell_index"]])[:2000]
        elif kind == "image":
            d["media_url"] = "/media/" + Path(ev["image_path"]).name
    except Exception as exc:
        d["enrich_error"] = str(exc)
    return d


def workspace_label(root: Path) -> str:
    """Short human label for a workspace folder: ``.kumokg_alice_demo`` -> ``alice_demo``."""
    name = root.name.lstrip(".")
    for prefix in ("kumokg_", "kumokg-"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
    return name or root.name


def active_sources(ws: Workspace, audit: dict[str, Any]) -> list[dict[str, Any]]:
    """Usable sources grouped by file kind, with page and chunk counts (excluded files never count as active)."""
    from collections import Counter

    chunks = Counter(c.doc_id for c in ws.load_chunks().values())
    groups: dict[str, dict[str, Any]] = {}
    for f in audit.get("files", []):
        if not f.get("doc_id") or f.get("status") not in ("usable", "needs_ocr_or_vlm"):
            continue
        kind = "WEB" if f.get("modality") == "web" else (f.get("ext") or "").lstrip(".").upper() or "FILE"
        g = groups.setdefault(kind, {"kind": kind, "modality": f.get("modality"), "files": 0, "pages": 0, "chunks": 0,
                                     "needs_ocr_or_vlm": 0})
        g["files"] += 1
        g["pages"] += int((f.get("details") or {}).get("page_count") or 0)
        g["chunks"] += chunks.get(f["doc_id"], 0)
        g["needs_ocr_or_vlm"] += f.get("status") == "needs_ocr_or_vlm"
    return sorted(groups.values(), key=lambda g: (-g["files"], g["kind"]))


def entity_surfaces(ont, nodes: dict[str, Any], node_id: str | None, name: str, extra: Any = ()) -> list[str]:
    """Every surface form an entity may take in a quote (canonical, graph aliases, alias rules, raw claim names)."""
    from core.ir import normalize_name

    forms = {name, *extra}
    node = nodes.get(node_id) if node_id else None
    if node is not None:
        forms.update(node.aliases)
    key = normalize_name(name)
    for rule in (ont.alias_rules if ont else []):
        if normalize_name(rule.canonical) == key:
            forms.update(rule.aliases)
    return sorted({f.strip() for f in forms if f and len(f.strip()) >= 2}, key=lambda s: (-len(s), s))


# --------------------------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "KumoKGStudio/0.1"
    state: StudioState

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the CLI quiet
        if os.environ.get("KUMOKG_STUDIO_LOG"):
            super().log_message(fmt, *args)

    # utils -----------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str, hint: str | None = None) -> None:
        payload = {"ok": False, "error": msg}
        if hint:
            payload["hint"] = hint
        self._json(payload, code)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin and self.command == "POST":
            o = urlparse(origin)
            return o.hostname in ("127.0.0.1", "localhost") and (o.port or 80) == self.state.port
        return True

    def _read_body(self) -> bytes:
        """Consume the declared request body before any reply, including rejections. Closing a socket that still
        holds unread request bytes makes the OS send a TCP reset, which can destroy the response in flight."""
        try:
            n = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            n = 0
        if n > MAX_BODY:
            left = min(n, 4 * MAX_BODY)  # drain a bounded amount, then refuse and close
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
            raise ValueError("request body too large")
        return self.rfile.read(n) if n else b""

    # routing ---------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(403, "forbidden host")
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if path == "/":
                return self._redirect(self._default_page())
            if path in PAGES:
                return self._static(PAGES[path])
            if path.startswith("/assets/"):
                return self._static(unquote(path[len("/assets/"):]))
            if path.startswith("/media/"):
                return self._media(unquote(path[len("/media/"):]))
            if path == "/api/events":
                return self._sse()
            api = {
                "/api/status": self.api_status, "/api/ontology": self.api_ontology, "/api/candidates": self.api_candidates,
                "/api/graph": self.api_graph, "/api/doctor": self.api_doctor, "/api/search": self.api_search,
                "/api/adapters": self.api_adapters, "/api/communities": self.api_communities, "/api/history": self.api_history,
                "/api/client-log": self.api_client_log_get,
            }
            if path in api:
                return api[path](qs)
            if path.startswith("/api/evidence/"):
                return self.api_evidence(unquote(path[len("/api/evidence/"):]))
            if path.startswith("/api/proposal/"):
                return self.api_proposal(unquote(path[len("/api/proposal/"):]))
            return self._error(404, f"no route {path}")
        except FileNotFoundError as exc:
            return self._error(409, str(exc))
        except (KeyError, ValueError) as exc:
            return self._error(400, f"bad request: {exc}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception as exc:  # never drop the connection without a response
            return self._error(500, f"internal error ({type(exc).__name__}): {exc}")

    def do_POST(self) -> None:
        try:
            raw = self._read_body()
        except ValueError as exc:
            return self._error(413, str(exc))
        if not self._host_ok():
            return self._error(403, "forbidden host or origin")
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._error(400, f"invalid JSON body: {exc}")
        routes = {"/api/ontology/approve": self.api_approve, "/api/edges/decide": self.api_decide,
                  "/api/export": self.api_export, "/api/notify": self.api_notify,
                  "/api/contradictions/resolve": self.api_resolve_contradiction, "/api/client-log": self.api_client_log_post}
        if path not in routes:
            return self._error(404, f"no route {path}")
        try:
            return routes[path](body)
        except FileNotFoundError as exc:
            return self._error(409, str(exc))
        except (KeyError, ValueError) as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"internal error ({type(exc).__name__}): {exc}")

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _default_page(self) -> str:
        st = self.state
        if st.gate == "gate1":
            return "/gate1"
        if st.gate == "gate2":
            return "/gate2"
        ws = st.ws
        if ws.graph_path.exists():
            return "/explorer"
        if ws.verify_path.exists():
            return "/gate2"
        if ws.ontology_draft_path.exists() and not ws.ontology_approved_path.exists():
            return "/gate1"
        return "/explorer"

    def _static(self, name: str) -> None:
        target = (ASSETS_DIR / name).resolve()
        if not target.is_relative_to(ASSETS_DIR.resolve()) or not target.is_file():
            return self._error(404, "asset not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"})

    def _media(self, name: str) -> None:
        media = self.state.ws.media_dir.resolve()
        target = (media / name).resolve()
        if not target.is_relative_to(media) or not target.is_file():
            return self._error(404, "media not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if not ctype.startswith("image/"):
            return self._error(403, "only images are served")
        self._send(200, target.read_bytes(), ctype)

    def _sse(self) -> None:
        q: queue.Queue = queue.Queue()
        with self.state.lock:
            self.state.clients.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            hello = {"type": "hello", "graph_hash": self.state.last_hash, "gate": self.state.gate, "ts": utcnow_iso()}
            self.wfile.write(f"retry: 2000\nevent: hello\ndata: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            while not self.state.done.is_set():
                try:
                    ev = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with self.state.lock:
                if q in self.state.clients:
                    self.state.clients.remove(q)

    # GET APIs --------------------------------------------------------------------------
    def api_status(self, qs: dict) -> None:
        ws = self.state.ws
        m = ws.load_manifest()
        g = read_json(ws.graph_path, {}) or {}
        ont = ws.load_ontology(True) or ws.load_ontology(False)
        self._json({"ok": True, "workspace": str(ws.root), "workspace_name": workspace_label(ws.root), "phase": m.phase,
                    "mode": m.mode, "tier": m.extraction_tier, "gates": m.gates, "gate": self.state.gate,
                    "blocking": self.state.blocking, "classes": sorted(ont.class_names()) if ont else [],
                    "graph": {"nodes": len(g.get("nodes", [])), "edges": len(g.get("edges", [])),
                              "hash": (g.get("meta") or {}).get("content_hash")},
                    "sse_clients": len(self.state.clients)})

    def api_ontology(self, qs: dict) -> None:
        ws = self.state.ws
        draft = ws.load_ontology(False)
        approved = ws.load_ontology(True)
        use_approved = approved is not None and (
            draft is None or ws.ontology_approved_path.stat().st_mtime_ns >= ws.ontology_draft_path.stat().st_mtime_ns)
        ont = approved if use_approved else draft
        if ont is None:
            raise FileNotFoundError("no ontology draft yet: run `kumokg propose-ontology`")
        audit = read_json(ws.audit_path, {}) or {}
        m = ws.load_manifest()
        excluded = [{"name": Path(x["path"]).name if not str(x["path"]).startswith("http") else x["path"],
                     "doc_id": x.get("doc_id"), "reason": x["reason"], "detail": x.get("detail", "")}
                    for x in audit.get("excluded_sources", [])]
        self._json({"ok": True, "ontology": ont.model_dump(mode="json"), "approved": approved is not None,
                    "source": "approved" if use_approved else "draft", "draft_newer": approved is not None and not use_approved,
                    "approved_fingerprint": approved.fingerprint() if approved else None,
                    "mapping_preview": mapping_preview(ws, ont), "docs": {s.doc_id: s.name for s in m.sources},
                    "audit": {"sources_scanned": audit.get("sources_scanned"), "modality_breakdown": audit.get("modality_breakdown"),
                              "excluded": len(excluded), "needs_ocr_or_vlm": len(audit.get("needs_ocr_or_vlm", [])),
                              "active": active_sources(ws, audit), "excluded_sources": excluded,
                              "duplicates": audit.get("duplicates", [])}})

    def _graph_edges_by_proposal(self, proposals: list[dict] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """proposal id -> committed edge (by proposal id, else by identical triple), plus the full edge map."""
        from core.verifier import committed_edge_for, index_edges

        g = self.state.ws.load_graph()
        by_ref, by_triple = index_edges(g.edges)
        by_prop = dict(by_ref)
        for p in proposals or []:
            if p["proposal_id"] not in by_prop:
                e = committed_edge_for(p["proposal_id"], p["source"].get("id"), p["predicate"], p["target"].get("id"),
                                       by_ref, by_triple)
                if e is not None:
                    by_prop[p["proposal_id"]] = e
        return by_prop, g.edge_map()

    def api_candidates(self, qs: dict) -> None:
        from core.patcher import contradiction_resolved

        ws = self.state.ws
        rep = read_json(ws.verify_path, None)
        if rep is None:
            raise FileNotFoundError("no verify-report.json yet: run `kumokg verify`")
        decisions = ws.load_decisions()
        m = ws.load_manifest()
        docs = {s.doc_id: s.name for s in m.sources}
        by_prop, edges = self._graph_edges_by_proposal(rep["proposals"])
        contradictions = []
        open_refs: set[str] = set()
        for c in rep["contradictions"]:
            resolved = contradiction_resolved(c, decisions, edges)
            if not resolved:
                open_refs.update(v["ref"] for v in c["values"])
            contradictions.append({**c, "resolved": resolved,
                                   "values": [{**v, "decision": (decisions.get(v["ref"]) or {}).get("decision")} for v in c["values"]]})
        proposals = []
        for p in rep["proposals"]:
            d = decisions.get(p["proposal_id"])
            ge = by_prop.get(p["proposal_id"])
            proposals.append({k: p[k] for k in ("proposal_id", "source", "target", "predicate", "valid_from", "valid_to",
                                                 "bucket", "status", "confidence", "docs", "modalities", "extractors",
                                                 "reasons", "best_verdict", "ontology")}
                             | {"evidence_count": len(p["evidence"]), "candidate_count": len(p["candidate_ids"]),
                                "quote": next((e.get("verbatim_quote") or e.get("element_description") or "" for e in p["evidence"]), ""),
                                "decision": d, "graph_edge_id": ge.id if ge else None, "graph_status": ge.status if ge else None,
                                "open_conflict": p["proposal_id"] in open_refs})
        decided = sum(1 for p in rep["proposals"] if p["proposal_id"] in decisions)
        self._json({"ok": True, "summary": rep["summary"], "threshold": rep["threshold"], "proposals": proposals,
                    "contradictions": contradictions, "schema_extensions": rep["proposed_schema_extensions"],
                    "ambiguous": rep["entity_resolution"]["ambiguous_pairs"], "docs": docs,
                    "decided": decided, "decisions_total": len(decisions), "graph_exists": ws.graph_path.exists()})

    def api_proposal(self, pid: str) -> None:
        from core.ir import normalize_name
        from core.patcher import contradiction_resolved
        from core.verifier import VerifyContext

        ws = self.state.ws
        rep = read_json(ws.verify_path, {}) or {}
        props = {x["proposal_id"]: x for x in rep.get("proposals", [])}
        p = props.get(pid)
        if p is None:
            raise KeyError(pid)
        ctx = VerifyContext(ws)
        docs = {s.doc_id: s.name for s in ws.load_manifest().sources}
        decisions = ws.load_decisions()
        graph = ws.load_graph()
        nodes = graph.node_map()
        by_prop, edges = self._graph_edges_by_proposal(rep.get("proposals", []))
        ont = ws.load_ontology(True)
        raw = {c.candidate_id: c for c in ws.load_candidates()} if p.get("candidate_ids") else {}
        src_extra, tgt_extra = set(), set()
        for cid in p.get("candidate_ids", []):
            c = raw.get(cid)
            if c is None or c.object is None:
                continue
            swapped = normalize_name(c.subject.name) == normalize_name(p["target"]["name"])
            (tgt_extra if swapped else src_extra).add(c.subject.name)
            (src_extra if swapped else tgt_extra).add(c.object.name)
        out = dict(p)
        out["evidence"] = [enrich_evidence(ws, e, ctx, docs) for e in p["evidence"]]
        out["decision"] = decisions.get(pid)
        out["surfaces"] = {"source": entity_surfaces(ont, nodes, p["source"]["id"], p["source"]["name"], src_extra),
                           "target": entity_surfaces(ont, nodes, p["target"]["id"], p["target"]["name"], tgt_extra)}
        ge = by_prop.get(pid)
        out["graph_edge"] = ({"id": ge.id, "status": ge.status, "valid_from": ge.valid_from, "valid_to": ge.valid_to,
                              "superseded_by": ge.superseded_by} if ge else None)
        contradictions = []
        for c in rep.get("contradictions", []):
            if not any(v["ref"] == pid for v in c["values"]):
                continue
            values = []
            for v in c["values"]:
                e = edges.get(v["ref"]) or by_prop.get(v["ref"])
                if e is None and v["ref"] in props:
                    t = props[v["ref"]]["target"]["id"]
                    e = next((x for x in graph.edges if x.source_id == c["source_id"] and x.type == c["predicate"]
                              and x.target_id == t and x.status == "approved"), None)
                values.append({**v, "decision": (decisions.get(v["ref"]) or {}).get("decision"), "is_proposal": v["ref"] in props,
                               "graph": ({"id": e.id, "status": e.status, "valid_from": e.valid_from, "valid_to": e.valid_to}
                                         if e else None)})

            def when(x):
                return (x["graph"] or {}).get("valid_from") or x.get("valid_from")

            dated = sorted((x for x in values if when(x)), key=when)
            undated = [x for x in values if not when(x)]
            active_later = [x for x in values if x["graph"] and x["graph"]["status"] == "approved" and x["graph"]["valid_from"]]
            contradictions.append({**c, "values": values, "resolved": contradiction_resolved(c, decisions, edges),
                                   "suggested_order": [x["ref"] for x in dated + undated],
                                   "suggested_switch_at": active_later[-1]["graph"]["valid_from"] if active_later else None})
        out["contradictions"] = contradictions
        self._json({"ok": True, "proposal": out})

    def api_graph(self, qs: dict) -> None:
        from core.retrieval import GraphIndex

        gi = GraphIndex(self.state.ws)
        view = gi.view(as_of=qs.get("as_of") or None, include_superseded=qs.get("include_superseded") in ("1", "true"))
        self.state.last_hash = self.state.last_hash or view["graph_hash"]
        self._json({"ok": True, **view})

    def api_doctor(self, qs: dict) -> None:
        from core.verifier import doctor

        ws = self.state.ws
        g = read_json(ws.graph_path, {}) or {}
        key = (g.get("meta") or {}).get("content_hash", "") + "".join(
            str(p.stat().st_mtime_ns) if p.exists() else "0" for p in (ws.verify_path, ws.decisions_path))
        if self.state._doctor_cache and self.state._doctor_cache[0] == key and qs.get("refresh") != "1":
            return self._json(self.state._doctor_cache[1])
        card = doctor(ws, deep=qs.get("deep") == "1")
        payload = {"ok": True, **card}
        self.state._doctor_cache = (key, payload)
        self._json(payload)

    def api_search(self, qs: dict) -> None:
        from core.retrieval import query

        q = qs.get("q", "").strip()
        if not q:
            raise ValueError("missing q")
        res = query(self.state.ws, q, mode=qs.get("mode", "local"), hops=int(qs.get("hops", 2)),
                    as_of=qs.get("as_of") or None, max_tokens=int(qs.get("max_tokens", 1500)), fmt="json")
        self._json({"ok": True, **res})

    def api_evidence(self, item_id: str) -> None:
        from core.retrieval import GraphIndex
        from core.verifier import VerifyContext

        ws = self.state.ws
        gi = GraphIndex(ws)
        info = gi.evidence_for(item_id)
        ctx = VerifyContext(ws)
        info["evidence"] = [enrich_evidence(ws, {k: v for k, v in e.items() if k not in ("source_name", "citation", "context", "media_url")}, ctx, gi.docs)
                            | {"citation": e.get("citation")} for e in info["evidence"]]
        ont = ws.load_ontology(True)
        if info["kind"] == "edge":
            info["surfaces"] = {"source": entity_surfaces(ont, gi.nodes, info["source"], info["source_name"]),
                                "target": entity_surfaces(ont, gi.nodes, info["target"], info["target_name"])}
        else:
            info["surfaces"] = {"source": entity_surfaces(ont, gi.nodes, info["id"], info["name"]), "target": []}
        from core.patcher import history

        info["history"] = history(ws, item_id, limit=20)
        self._json({"ok": True, **info})

    def api_communities(self, qs: dict) -> None:
        from core.retrieval import GraphIndex

        self._json({"ok": True, "communities": GraphIndex(self.state.ws).communities(qs.get("as_of") or None)})

    def api_history(self, qs: dict) -> None:
        from core.patcher import history

        self._json({"ok": True, "changes": history(self.state.ws, qs.get("id") or None, int(qs.get("limit", 50)))})

    def api_adapters(self, qs: dict) -> None:
        from adapters.base import load_registry

        self._json({"ok": True, "adapters": load_registry()})

    # POST APIs -------------------------------------------------------------------------
    def api_approve(self, body: dict) -> None:
        from core.structured_compiler import approve_ontology

        data = body.get("ontology")
        if not isinstance(data, dict):
            raise ValueError("body must contain an 'ontology' object")
        ont = approve_ontology(self.state.ws, data)
        result = {"ok": True, "gate": "gate1", "fingerprint": ont.fingerprint(), "classes": len(ont.entity_classes),
                  "relations": len(ont.relations)}
        self._json(result)
        self.state.broadcast({"type": "gate1_approved", "fingerprint": ont.fingerprint()})
        if self.state.gate in (None, "gate1"):
            self.state.finish(result)

    def api_decide(self, body: dict) -> None:
        from core.patcher import commit, record_decisions

        ws = self.state.ws
        decisions = body.get("decisions") or []
        if not isinstance(decisions, list):
            raise ValueError("'decisions' must be a list")
        res: dict[str, Any] = {"ok": True, "decisions": record_decisions(ws, decisions, by="gate2")}
        if body.get("commit"):
            res["commit"] = commit(ws, by="gate2")
            self.state.broadcast({"type": "graph_changed", "reason": "gate2_commit", "graph_hash": res["commit"]["graph_hash"]})
        self._json(res)
        if body.get("commit") and self.state.gate in (None, "gate2"):
            self.state.finish(res)

    def api_export(self, body: dict) -> None:
        from adapters.base import run_export

        target = body.get("target")
        if not target:
            raise ValueError("missing 'target'")
        res = run_export(self.state.ws, target, provenance_mode=body.get("provenance_mode", "props"),
                         options=body.get("options") or {}, verify=True)
        self._json({"ok": res["parity"]["ok"], **res})

    def api_resolve_contradiction(self, body: dict) -> None:
        from core.patcher import resolve_contradiction

        res = resolve_contradiction(self.state.ws, body.get("contradiction_id", ""), body.get("action", ""),
                                    keep=body.get("keep"), order=body.get("order"), switch_at=body.get("switch_at"), by="gate2")
        self.state.broadcast({"type": "graph_changed", "reason": "contradiction_resolved", "graph_hash": res["graph_hash"]})
        self._json(res)

    def api_client_log_post(self, body: dict) -> None:
        entry = {"ts": utcnow_iso(), **{k: body.get(k) for k in ("page", "event", "level", "message", "stack", "stats") if k in body}}
        with self.state.lock:
            self.state.client_log.append(entry)
            del self.state.client_log[:-500]
        self._json({"ok": True})

    def api_client_log_get(self, qs: dict) -> None:
        with self.state.lock:
            rows = list(self.state.client_log)
        self._json({"ok": True, "entries": rows, "errors": sum(1 for r in rows if r.get("level") == "error")})

    def api_notify(self, body: dict) -> None:
        if self.headers.get("X-KumoKG-Token") != self.state.token:
            return self._error(401, "bad token")
        n = self.state.broadcast(body if isinstance(body, dict) else {"type": "message"})
        self._json({"ok": True, "delivered": n})


# --------------------------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------------------------


class StudioServer:
    def __init__(self, ws: Workspace, port: int = 0, gate: str | None = None, blocking: bool = False, watch: bool = True):
        ws.require()
        self.state = StudioState(ws, gate=gate, blocking=blocking)
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.state.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.state.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._watch = watch
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> "StudioServer":
        g = read_json(self.state.ws.graph_path, {}) or {}
        self.state.last_hash = (g.get("meta") or {}).get("content_hash")
        write_json(self.state.ws.studio_path, {"port": self.state.port, "url": self.url, "pid": os.getpid(),
                                               "token": self.state.token, "gate": self.state.gate, "started_at": utcnow_iso()})
        self._thread.start()
        if self._watch:
            self._watcher = threading.Thread(target=self._watch_graph, daemon=True)
            self._watcher.start()
        return self

    def _watch_graph(self) -> None:
        path = self.state.ws.graph_path
        last = path.stat().st_mtime_ns if path.exists() else 0
        while not self._stop.wait(0.75):
            try:
                cur = path.stat().st_mtime_ns if path.exists() else 0
            except OSError:
                continue
            if cur != last:
                last = cur
                g = read_json(path, {}) or {}
                self.state.broadcast({"type": "graph_changed", "reason": "watch",
                                      "graph_hash": (g.get("meta") or {}).get("content_hash")})

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        """Block until the gate completes (blocking mode) or until interrupted."""
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while not self.state.done.wait(0.25):
                if deadline is not None and time.monotonic() > deadline:
                    return {"ok": False, "timeout": True}
        except KeyboardInterrupt:
            return {"ok": False, "interrupted": True}
        time.sleep(0.3)
        return self.state.result

    def stop(self) -> None:
        self._stop.set()
        self.state.done.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        info = read_json(self.state.ws.studio_path, None)
        if info and info.get("port") == self.state.port:
            try:
                self.state.ws.studio_path.unlink()
            except OSError:
                pass


def run_studio(ws: Workspace, port: int = 8765, gate: str | None = None, blocking: bool = False,
               open_browser: bool = False, timeout: float | None = None) -> dict[str, Any]:
    server = StudioServer(ws, port=port, gate=gate, blocking=blocking).start()
    page = {"gate1": "/gate1", "gate2": "/gate2"}.get(gate or "", "/explorer")
    print(json.dumps({"ok": True, "studio": server.url, "open": server.url + page, "gate": gate, "blocking": blocking,
                      "pages": {k: server.url + k for k in PAGES}}), flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(server.url + page)
    try:
        if blocking:
            result = server.wait(timeout)
        else:
            result = server.wait(timeout) if timeout else _forever(server)
    finally:
        server.stop()
    return result


def _forever(server: StudioServer) -> dict[str, Any]:
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return {"ok": True, "stopped": True}

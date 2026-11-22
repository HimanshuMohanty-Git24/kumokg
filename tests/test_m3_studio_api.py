"""Milestone 3: Studio REST + SSE endpoints, gate blocking, security hardening and zero-build assets."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

import httpx
import pytest

from conftest import SKILL_DIR
from core import patcher, verifier, worker_pool as wp
from core.ir import read_json
from studio.server import StudioServer

ASSETS = SKILL_DIR / "assets" / "studio"


@pytest.fixture()
def reviewed_ws(synthetic_ws):
    wp.extract_heuristic(synthetic_ws)
    verifier.verify(synthetic_ws)
    return synthetic_ws


@pytest.fixture()
def server(reviewed_ws):
    srv = StudioServer(reviewed_ws, port=0).start()
    yield srv
    srv.stop()


def client(srv):
    return httpx.Client(base_url=srv.url, timeout=10)


class SSEReader:
    """Background SSE consumer collecting events."""

    def __init__(self, url):
        self.events = []
        self.ready = threading.Event()
        self._stop = False
        self.t = threading.Thread(target=self._run, args=(url,), daemon=True)
        self.t.start()
        assert self.ready.wait(5), "SSE stream never sent hello"

    def _run(self, url):
        with httpx.stream("GET", url + "/api/events", timeout=httpx.Timeout(20, read=20)) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            event = None
            for line in r.iter_lines():
                if self._stop:
                    return
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    self.events.append((event, json.loads(line.split(":", 1)[1])))
                    if event == "hello":
                        self.ready.set()

    def wait_for(self, etype, timeout=8.0, pred=lambda d: True):
        end = time.time() + timeout
        while time.time() < end:
            for e, d in self.events:
                if e == etype and pred(d):
                    return d
            time.sleep(0.05)
        raise AssertionError(f"no {etype} event; got {[e for e, _ in self.events]}")

    def close(self):
        self._stop = True


def test_pages_and_assets_served(server):
    with client(server) as c:
        for path, marker in (("/gate1", "Ontology review"), ("/gate2", "Edge &amp; evidence review"), ("/explorer", "Time travel")):
            r = c.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and marker in r.text
            assert "default-src 'self'" in r.headers["content-security-policy"]
        assert c.get("/assets/styles.css").headers["content-type"].startswith("text/css")
        assert c.get("/assets/kumo.js").headers["content-type"].startswith("text/javascript")
        assert c.get("/assets/..%2F..%2Fscripts%2Fkumokg_cli.py").status_code == 404
        assert c.get("/assets/nope.css").status_code == 404
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/gate2"
        assert c.get("/api/nothing").status_code == 404


def test_localhost_hardening(server):
    with client(server) as c:
        assert c.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
        r = c.post("/api/edges/decide", json={"decisions": []}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        assert c.post("/api/notify", json={"type": "graph_changed"}).status_code == 401
        assert c.post("/api/edges/decide", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert server.httpd.server_address[0] == "127.0.0.1"


def _raw_post(port: int, body: bytes, origin: str) -> bytes:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"POST /api/edges/decide HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {origin}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


def test_rejected_posts_still_deliver_their_response(server):
    """A rejected POST must consume its body first: closing a socket with unread bytes sends a TCP reset that can
    destroy the 403 in flight (seen as an intermittent client ReadError)."""
    from studio.server import MAX_BODY

    port = server.httpd.server_address[1]
    body = json.dumps({"decisions": [], "pad": "x" * 512_000}).encode()
    for _ in range(25):  # the pre-fix handler lost ~1 in 6 of these responses
        resp = _raw_post(port, body, "http://evil.example")
        assert resp.startswith(b"HTTP/1.0 403") and b"forbidden host or origin" in resp
    resp = _raw_post(port, b" " * (MAX_BODY + 1), server.url)
    assert resp.startswith(b"HTTP/1.0 413") and b"too large" in resp


def test_status_ontology_and_mapping_preview(server):
    with client(server) as c:
        st = c.get("/api/status").json()
        assert st["ok"] and st["phase"] == "verified"
        data = c.get("/api/ontology").json()
        assert data["approved"] and data["ontology"]["entity_classes"]
        prev = {p["mapping_id"].split(":", 1)[1]: p for p in data["mapping_preview"]}
        assert prev["companies"]["rows"][0]["name"] == "Acme Robotics"
        assert prev["/projects/*"]["rows"][1]["name"] == "Project Borealis"
        assert data["audit"]["modality_breakdown"]["image"] == 2


def test_gate1_blocking_approve_flow(synthetic_ws):
    ws = synthetic_ws
    srv = StudioServer(ws, port=0, gate="gate1", blocking=True).start()
    out = {}
    waiter = threading.Thread(target=lambda: out.update(srv.wait(timeout=15)))
    waiter.start()
    try:
        with client(srv) as c:
            assert c.get("/", follow_redirects=False).headers["location"] == "/gate1"
            ont = c.get("/api/ontology").json()["ontology"]
            ont["entity_classes"].append({"name": "Investor", "description": "added at Gate 1", "properties": {}, "examples": [], "seed_terms": ["Northwind Capital"]})
            ont["relations"].append({"source_class": "Investor", "predicate": "FUNDS", "target_class": "Company"})
            bad = json.loads(json.dumps(ont))
            bad["relations"].append({"source_class": "Ghost", "predicate": "HAUNTS", "target_class": "Company"})
            r = c.post("/api/ontology/approve", json={"ontology": bad})
            assert r.status_code == 400 and "unknown class Ghost" in r.json()["error"]
            assert waiter.is_alive()
            r = c.post("/api/ontology/approve", json={"ontology": ont})
            assert r.status_code == 200 and r.json()["ok"]
        waiter.join(10)
        assert not waiter.is_alive() and out["gate"] == "gate1"
        approved = ws.load_ontology(True)
        assert approved.allows("Investor", "FUNDS", "Company")
        assert ws.load_manifest().gates["gate1"] == "approved"
    finally:
        srv.stop()
    assert not ws.studio_path.exists()


def test_gate2_candidates_inspector_decide_commit_and_sse(server, reviewed_ws):
    ws = reviewed_ws
    sse = SSEReader(server.url)
    try:
        with client(server) as c:
            data = c.get("/api/candidates").json()
            buckets = {p["bucket"] for p in data["proposals"]}
            assert {"auto", "conflict", "review"} <= buckets and data["contradictions"]
            conflict = next(p for p in data["proposals"] if p["bucket"] == "conflict" and p["target"]["name"] == "Berlin")
            detail = c.get(f"/api/proposal/{conflict['proposal_id']}").json()["proposal"]
            tev = next(e for e in detail["evidence"] if e["kind"] == "table")
            assert tev["row"]["columns"][:2] == ["company_id", "name"] and tev["check"]["status"] == "verified"
            assert detail["contradictions"][0]["predicate"] == "HEADQUARTERED_IN"
            text_p = next(p for p in data["proposals"] if p["predicate"] == "LEADS")
            tdetail = c.get(f"/api/proposal/{text_p['proposal_id']}").json()["proposal"]
            ctx = tdetail["evidence"][0]["context"]
            assert "leads" in ctx["match"].lower() and tdetail["evidence"][0]["page_or_span"].startswith("p")
            json_p = next(p for p in data["proposals"] if p["predicate"] == "HAS_MILESTONE")
            jdetail = c.get(f"/api/proposal/{json_p['proposal_id']}").json()["proposal"]
            jev = jdetail["evidence"][0]
            assert jev["path"][0] == "projects" and "milestones" in jev["path"] and '"date"' in jev["parent"]
            assert c.get("/api/proposal/p-missing").status_code == 400
            decisions = [{"proposal_id": p["proposal_id"], "decision": "approve"} for p in data["proposals"] if p["bucket"] == "auto"]
            decisions.append({"proposal_id": conflict["proposal_id"], "decision": "approve"})
            munich = next(p for p in data["proposals"] if p["bucket"] == "conflict" and p["target"]["name"] == "Munich")
            decisions.append({"proposal_id": munich["proposal_id"], "decision": "reject", "reason": "report is outdated"})
            r = c.post("/api/edges/decide", json={"decisions": decisions, "commit": True}, headers={"Origin": server.url})
            res = r.json()
            assert r.status_code == 200 and res["commit"]["edges_added"] == len(decisions) - 1
            assert res["decisions"]["calibration_rules_added"]["negative_examples"] == 1
            ev = sse.wait_for("graph_changed", pred=lambda d: d.get("reason") == "gate2_commit")
            assert ev["graph_hash"] == ws.load_graph().content_hash()
            again = c.get("/api/candidates").json()
            assert again["decided"] == len(decisions) and again["graph_exists"]
            assert c.get("/", follow_redirects=False).headers["location"] == "/explorer"
    finally:
        sse.close()
    assert read_json(ws.calibration_path)["negative_examples"][0]["object"] == "Munich"


def test_graph_doctor_search_evidence_media_endpoints(server, reviewed_ws):
    ws = reviewed_ws
    patcher.auto_decide(ws)
    patcher.commit(ws)
    from core import media_prep

    media_prep.prepare_media(ws)
    with client(server) as c:
        g = c.get("/api/graph").json()
        assert g["counts"]["nodes"] == len(ws.load_graph().nodes) and g["labels"] and g["edges"]
        assert all("community" in n for n in g["nodes"])
        doc = c.get("/api/doctor").json()
        assert doc["grounding"]["pct"] == 100.0 and doc["nodes"] == g["counts"]["nodes"]
        assert c.get("/api/doctor").json() == doc  # cached by graph hash
        local = c.get("/api/search", params={"q": "Who leads Project Atlas?", "mode": "local"}).json()
        assert any(e["type"] == "LEADS" and e["target_name"] == "Project Atlas" for e in local["edges"])
        ppr = c.get("/api/search", params={"q": "Elena Voss", "mode": "ppr"}).json()
        assert ppr["mode"] == "ppr" and ppr["seeds"][0]["name"] == "Elena Voss"
        assert c.get("/api/search").status_code == 400
        assert c.get("/api/search", params={"q": "x", "mode": "bogus"}).status_code == 400
        node = c.get("/api/evidence/company:acme-robotics").json()
        assert node["kind"] == "node" and node["evidence"] and node["evidence"][0]["check"]["status"] == "verified"
        edge_id = next(e["id"] for e in g["edges"] if e["type"] == "LEADS")
        edge = c.get(f"/api/evidence/{edge_id}").json()
        assert edge["kind"] == "edge" and edge["evidence"][0]["context"]["match"]
        assert c.get("/api/evidence/nope").status_code == 400
        comms = c.get("/api/communities").json()["communities"]
        assert comms and sum(cm["size"] for cm in comms) <= g["counts"]["nodes"]
        png = next(ws.media_dir.glob("*.png")).name
        r = c.get(f"/media/{png}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
        assert c.get("/media/..%2Fmanifest.json").status_code == 404
        (ws.media_dir / "notes.txt").write_text("x")
        assert c.get("/media/notes.txt").status_code == 403


def test_patch_notifies_studio_over_sse_and_time_travel(server, reviewed_ws):
    ws = reviewed_ws
    patcher.auto_decide(ws)
    patcher.commit(ws)
    g = ws.load_graph()
    old = next(e for e in g.edges if e.type == "WORKS_AT" and e.source_id == "person:elena-voss")
    sse = SSEReader(server.url)
    try:
        chunk = next(c for c in ws.load_chunks().values() if "joined Globex Dynamics" in c.text)
        res = patcher.apply_patch(ws, {"author": "chat", "message": "Elena moved to Globex", "ops": [{
            "op": "supersede-edge", "edge_id": old.id, "valid_from": "2023-06-01",
            "new": {"target": "company:globex-dynamics"}, "quote": "Elena Voss joined Globex Dynamics", "chunk_id": chunk.chunk_id}]})
        assert res["ok"] and res["studio_notified"] is True
        ev = sse.wait_for("graph_changed", pred=lambda d: d.get("reason") == "patch")
        assert ev["ops"] == ["supersede-edge"]
        with client(server) as c:
            past = c.get("/api/graph", params={"as_of": "2021-01-01"}).json()
            now = c.get("/api/graph").json()
            hist = c.get("/api/graph", params={"include_superseded": "1"}).json()
            def works(v):
                return {e["target"] for e in v["edges"] if e["type"] == "WORKS_AT" and e["source"] == "person:elena-voss"}
            assert works(past) == {"company:acme-robotics"} and works(now) == {"company:globex-dynamics"}
            assert works(hist) == {"company:acme-robotics", "company:globex-dynamics"}
            assert "2023-06-01" in now["temporal_breakpoints"]
            ev_hist = c.get(f"/api/evidence/{old.id}").json()["history"]
            assert ev_hist and ev_hist[-1]["op"] == "supersede-edge"
    finally:
        sse.close()


def test_file_watcher_broadcasts_external_changes(server, reviewed_ws):
    sse = SSEReader(server.url)
    try:
        patcher.auto_decide(reviewed_ws)
        reviewed_ws.studio_path.unlink()  # simulate a CLI that cannot reach the server: watcher must still notice
        patcher.commit(reviewed_ws)
        ev = sse.wait_for("graph_changed", pred=lambda d: d.get("reason") == "watch")
        assert ev["graph_hash"] == reviewed_ws.load_graph().content_hash()
    finally:
        sse.close()


def test_assets_zero_build_offline_and_syntax():
    files = sorted(ASSETS.iterdir())
    names = {f.name for f in files}
    assert {"styles.css", "ontology-review.html", "edge-review.html", "explorer.html"} <= names
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"""(src|href)\s*=\s*["']https?://""", text), f"external reference in {f.name}"
        assert "@import" not in text and "cdn." not in text.lower()
        assert "TODO" not in text and "FIXME" not in text
    css = (ASSETS / "styles.css").read_text(encoding="utf-8")
    assert "#d8dde6" in css and "JetBrains Mono" in css and "gradient(" not in css.replace("radial-gradient(color-mix", "")
    assert "backdrop-filter" not in css
    for html in ("ontology-review.html", "edge-review.html", "explorer.html"):
        text = (ASSETS / html).read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="/assets/([^"]+)"', text):
            assert (ASSETS / ref).exists(), ref
    for js in ASSETS.glob("*.js"):
        src = js.read_text(encoding="utf-8")
        for ref in re.findall(r'from "/assets/([^"]+)"', src):
            assert (ASSETS / ref).exists(), ref
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, "--input-type=module", "--check"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=60)
            assert r.returncode == 0, f"{js.name}: {r.stderr}"
        else:
            assert src.count("{") == src.count("}") and src.count("(") == src.count(")")
    gate2 = (ASSETS / "gate2.js").read_text(encoding="utf-8")
    for key in ('k === "j"', 'k === "k"', 'k === "a"', 'k === "r"', 'k === "e"', 'e.key === "Enter"'):
        assert key in gate2


# ---------------------------------------------------------------------------------------------
# Studio v2: workbench APIs, contradiction resolver, telemetry, canvas geometry
# ---------------------------------------------------------------------------------------------


def test_status_chip_ontology_source_and_active_audit(server, reviewed_ws):
    with client(server) as c:
        st = c.get("/api/status").json()
        assert st["workspace_name"] == reviewed_ws.root.name and "Company" in st["classes"]
        ont = c.get("/api/ontology").json()
        assert ont["source"] == "approved" and ont["draft_newer"] is False and ont["docs"]
        audit = ont["audit"]
        kinds = {a["kind"]: a for a in audit["active"]}
        assert kinds["PDF"]["files"] == 2 and kinds["PDF"]["pages"] == 5 and kinds["PDF"]["needs_ocr_or_vlm"] == 1
        assert {"CSV", "XLSX", "JSON", "YAML", "IPYNB", "MD", "PNG"} <= set(kinds)
        names = {x["name"] for x in audit["excluded_sources"]}
        assert {"empty.pdf", "corrupt.pdf", "diagram_copy.png"} <= names
        assert all(x["reason"] for x in audit["excluded_sources"])


def test_workspace_label():
    from pathlib import Path
    from studio.server import workspace_label

    assert workspace_label(Path(".kumokg_alice_demo")) == "alice_demo"
    assert workspace_label(Path("my-kg")) == "my-kg"


def _hq_conflict(c):
    data = c.get("/api/candidates").json()
    cx = next(x for x in data["contradictions"] if x["predicate"] == "HEADQUARTERED_IN")
    props = {p["proposal_id"]: p for p in data["proposals"]}
    berlin = next(v["ref"] for v in cx["values"] if v["target_name"] == "Berlin")
    munich = next(v["ref"] for v in cx["values"] if v["target_name"] == "Munich")
    return data, cx, props, berlin, munich


def test_resolve_contradiction_keep_only(server, reviewed_ws):
    with client(server) as c:
        data, cx, props, berlin, munich = _hq_conflict(c)
        assert not cx["resolved"] and props[berlin]["open_conflict"] and props[munich]["open_conflict"]
        detail = c.get(f"/api/proposal/{berlin}").json()["proposal"]
        assert detail["surfaces"]["source"][0] == "Acme Robotics" and "Berlin" in detail["surfaces"]["target"]
        rc = detail["contradictions"][0]
        assert {v["ref"] for v in rc["values"]} == {berlin, munich} and rc["values"][0]["is_proposal"]
        bad = c.post("/api/contradictions/resolve", json={"contradiction_id": cx["contradiction_id"], "action": "keep", "keep": "nope"})
        assert bad.status_code == 400
        r = c.post("/api/contradictions/resolve", json={"contradiction_id": cx["contradiction_id"], "action": "keep", "keep": berlin},
                   headers={"Origin": server.url})
        assert r.status_code == 200 and r.json()["resolved"] is True
        data, cx, props, berlin, munich = _hq_conflict(c)
        assert cx["resolved"] and not props[berlin]["open_conflict"] and props[berlin]["graph_status"] == "approved"
        assert props[munich]["decision"]["decision"] == "reject"
        assert c.post("/api/contradictions/resolve", json={"contradiction_id": "x-missing", "action": "keep"}).status_code == 400
    card = verifier.doctor(reviewed_ws, deep=False)
    assert not any(x["predicate"] == "HEADQUARTERED_IN" for x in card["contradictions"])
    assert any(e.type == "HEADQUARTERED_IN" and e.target_id == "city:berlin" for e in reviewed_ws.load_graph().edges)


def test_resolve_contradiction_as_temporal_sequence(server, reviewed_ws):
    from core import retrieval
    from core.ir import iter_jsonl

    with client(server) as c:
        _, cx, _, berlin, munich = _hq_conflict(c)
        missing = c.post("/api/contradictions/resolve", json={"contradiction_id": cx["contradiction_id"], "action": "sequence",
                                                              "order": [berlin, munich]})
        assert missing.status_code == 400 and "switch_at" in missing.json()["error"]
        r = c.post("/api/contradictions/resolve", json={"contradiction_id": cx["contradiction_id"], "action": "sequence",
                                                        "order": [berlin, munich], "switch_at": "2020-01-01"})
        res = r.json()
        assert r.status_code == 200 and res["resolved"], res
        data, cx, props, berlin, munich = _hq_conflict(c)
        assert cx["resolved"] and props[berlin]["graph_status"] == "superseded" and props[munich]["decision"]["decision"] == "resolved"
    g = reviewed_ws.load_graph()
    hq = sorted((e for e in g.edges if e.type == "HEADQUARTERED_IN" and e.source_id == "company:acme-robotics"), key=lambda e: e.status)
    assert [(e.target_id, e.status, e.valid_from, e.valid_to) for e in hq] == [
        ("city:munich", "approved", "2020-01-01", None), ("city:berlin", "superseded", None, "2020-01-01")]
    assert hq[0].evidence[0].kind == "text" and hq[1].evidence[0].kind == "table"

    def city(as_of):
        out = retrieval.query(reviewed_ws, "Where is Acme Robotics headquartered?", mode="local", as_of=as_of, fmt="json")
        return {e["target_name"] for e in out["edges"] if e["type"] == "HEADQUARTERED_IN" and e["source_name"] == "Acme Robotics"}

    assert city("2019-06-01") == {"Berlin"} and city("2021-06-01") == {"Munich"}
    assert not any(x["predicate"] == "HEADQUARTERED_IN" for x in verifier.doctor(reviewed_ws, deep=False)["contradictions"])
    ops = [row["op"] for row in iter_jsonl(reviewed_ws.changelog_path)]
    assert ops[-1] == "supersede-edge" and "commit" in ops


def test_graph_view_marks_active_edges(server, reviewed_ws):
    patcher.auto_decide(reviewed_ws)
    patcher.commit(reviewed_ws)
    old = next(e for e in reviewed_ws.load_graph().edges if e.type == "WORKS_AT" and e.source_id == "person:elena-voss")
    res = patcher.apply_patch(reviewed_ws, {"ops": [{"op": "supersede-edge", "edge_id": old.id, "valid_from": "2023-06-01",
                                                     "new": {"target": "company:globex-dynamics"},
                                                     "quote": "Elena Voss joined Globex Dynamics"}]})
    assert res["ok"]
    with client(server) as c:
        hist = c.get("/api/graph", params={"include_superseded": "1", "as_of": "2021-01-01"}).json()
        flags = {e["target"]: e["active"] for e in hist["edges"] if e["type"] == "WORKS_AT" and e["source"] == "person:elena-voss"}
        assert flags == {"company:acme-robotics": True, "company:globex-dynamics": False}
        now = c.get("/api/graph").json()
        assert all(e["active"] for e in now["edges"])


def test_client_log_endpoint(server):
    with client(server) as c:
        assert c.get("/api/client-log").json() == {"ok": True, "entries": [], "errors": 0}
        c.post("/api/client-log", json={"page": "explorer", "event": "render", "stats": {"overlaps": 0}}, headers={"Origin": server.url})
        c.post("/api/client-log", json={"page": "gate2", "event": "error", "level": "error", "message": "boom"})
        log = c.get("/api/client-log").json()
        assert log["errors"] == 1 and log["entries"][0]["stats"] == {"overlaps": 0}
        assert c.post("/api/client-log", json={"page": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403


CANVAS_GEOMETRY_JS = r"""
import { placeLabels, countOverlaps, radialLayout, clusterLayout } from "__URL__";
let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const items = [];
for (let i = 0; i < 400; i++) { const x = rnd() * 700, y = rnd() * 420, w = 40 + rnd() * 90;
  items.push({ id: "l" + i, w, h: 18, priority: rnd() * 100, anchors: [{ x, y }, { x: x + 25, y: y + 22 }, { x, y: y - 24 }] }); }
const placed = placeLabels(items, [], 2);
const top = items.reduce((a, b) => (a.priority > b.priority ? a : b));
const p2 = placeLabels([{ id: "a", w: 50, h: 18, priority: 1, anchors: [{ x: 10, y: 10 }, { x: 200, y: 10 }] }],
  [{ id: "o", x: 0, y: 0, w: 100, h: 100 }], 2);
const nodes = [...Array(31)].map((_, i) => ({ id: "n" + i, degree: i === 0 ? 30 : 1, community: i % 3 }));
const edges = nodes.slice(1).map(n => ({ source: "n0", target: n.id }));
const rad = radialLayout(nodes, edges, "n0");
const ring = nodes.slice(1).map(n => rad.get(n.id));
let minPair = Infinity;
for (let i = 0; i < ring.length; i++) for (let j = i + 1; j < ring.length; j++)
  minPair = Math.min(minPair, Math.hypot(ring[i].x - ring[j].x, ring[i].y - ring[j].y));
const cl = clusterLayout(nodes);
let minCross = Infinity;
for (const a of nodes) for (const b of nodes) if (a.community !== b.community)
  minCross = Math.min(minCross, Math.hypot(cl.get(a.id).x - cl.get(b.id).x, cl.get(a.id).y - cl.get(b.id).y));
console.log(JSON.stringify({ placed: placed.length, overlaps: countOverlaps(placed, 1), topPlaced: placed.some(p => p.id === top.id),
  avoided: p2[0].x === 200, center: rad.get("n0"), minRing: Math.min(...ring.map(p => Math.hypot(p.x, p.y))), minPair, minCross,
  clustered: cl.size }));
"""


def test_graph_canvas_geometry_under_node():
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required for the canvas geometry test")
    script = CANVAS_GEOMETRY_JS.replace("__URL__", (ASSETS / "graph-canvas.js").resolve().as_uri())
    r = subprocess.run([node, "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["overlaps"] == 0 and 50 < out["placed"] < 400 and out["topPlaced"] and out["avoided"]
    assert out["center"] == {"x": 0, "y": 0} and out["minRing"] >= 150 and out["minPair"] >= 110
    assert out["minCross"] >= 60 and out["clustered"] == 31


def test_resolve_sequence_when_already_sequenced_merges_evidence(server, reviewed_ws):
    with client(server) as c:
        _, cx, props, berlin, munich = _hq_conflict(c)
    patcher.record_decisions(reviewed_ws, [{"proposal_id": berlin, "decision": "approve"}])
    patcher.commit(reviewed_ws)
    edge = next(e for e in reviewed_ws.load_graph().edges if e.type == "HEADQUARTERED_IN" and e.target_id == "city:berlin")
    res = patcher.apply_patch(reviewed_ws, {"ops": [{"op": "supersede-edge", "edge_id": edge.id, "valid_from": "2020-01-01",
                                                     "new": {"target": {"name": "Munich", "label": "City"}, "create": True},
                                                     "quote": "headquartered in Munich"}]})
    assert res["ok"]
    assert not any(x["predicate"] == "HEADQUARTERED_IN" for x in verifier.doctor(reviewed_ws, deep=False)["contradictions"])
    with client(server) as c:
        _, cx, props, berlin, munich = _hq_conflict(c)
        # the patch already made the graph a temporal sequence: Berlin superseded, Munich committed (matched by triple)
        assert cx["resolved"] and not props[berlin]["open_conflict"] and not props[munich]["open_conflict"]
        assert props[berlin]["graph_status"] == "superseded" and props[munich]["graph_status"] == "approved"
        detail = c.get(f"/api/proposal/{munich}").json()["proposal"]
        assert detail["contradictions"][0]["resolved"]
        assert detail["contradictions"][0]["suggested_switch_at"] == "2020-01-01"
        r = c.post("/api/contradictions/resolve", json={"contradiction_id": cx["contradiction_id"], "action": "sequence",
                                                        "order": [berlin, munich], "switch_at": "2020-01-01"})
        out = r.json()
        assert r.status_code == 200 and out["resolved"] and "already_sequenced" in out["steps"][-1]
        assert out["steps"][-1]["evidence_added"] >= 1
    munich_edge = next(e for e in reviewed_ws.load_graph().edges if e.type == "HEADQUARTERED_IN" and e.target_id == "city:munich")
    assert munich_edge.status == "approved" and len(munich_edge.evidence) >= 2

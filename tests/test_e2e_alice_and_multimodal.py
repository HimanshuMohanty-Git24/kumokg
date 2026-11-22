"""Milestone 7: end-to-end runs.

Part A — synthetic multi-modal torture suite: a freshly generated corpus goes through the whole pipeline via the public
CLI and the Studio HTTP API (Gate 1 approve, Gate 2 decide + commit), including a scanned non-OCR PDF, corrupt/empty
PDFs, CSV/XLSX with foreign keys, nested JSON/YAML, a notebook with an embedded chart, an Obsidian vault, duplicate
images, simulated harness + VLM answers, a hallucinated edge, a cross-source contradiction and a bi-temporal update.

Part B — real corpus: ``./data/`` (Alice's Adventures in Wonderland) is processed into ``.kumokg_alice_demo/`` (kept for
the Studio visual check) and every committed edge is re-verified against a verbatim quote.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import generate_fixtures as gf
from build_alice_demo import NARRATIVE_T0, NARRATIVE_T1, build_alice_demo
from conftest import DATA_DIR, REPO_ROOT, gate1_edits
from core.ir import Ontology, Workspace, iter_jsonl, read_json, sha256_file
from core.verifier import VerifyContext, verify_evidence


def kg(*args, expect=0):
    from kumokg_cli import app

    r = CliRunner().invoke(app, list(args), catch_exceptions=False)
    assert r.exit_code == expect, f"{args}: {r.output[-1500:]}"
    return json.loads(r.output)


# =============================================================================================
# Part A: synthetic multi-modal torture suite
# =============================================================================================


@pytest.fixture(scope="module")
def torture(tmp_path_factory):
    from studio.server import StudioServer

    corpus = tmp_path_factory.mktemp("torture_corpus")
    gf.generate(corpus)
    root = tmp_path_factory.mktemp("torture") / "ws"
    w = ["--workspace", str(root)]
    run: dict = {"corpus": corpus, "root": root, "w": w}
    run["audit"] = kg("audit", str(corpus), *w, "--mode", "hitl")
    run["chunk"] = kg("chunk", *w)
    run["sample"] = kg("sample", *w, "--n", "10")
    draft = root / "agent_draft.json"
    draft.write_text(json.dumps(gf.AGENT_DRAFT), encoding="utf-8")
    run["propose"] = kg("propose-ontology", *w, "--from", str(draft))
    ws = Workspace(root)

    # Gate 1 through the real Studio API (what the browser's Approve & Continue button sends).
    srv = StudioServer(ws, port=0, gate="gate1", blocking=True).start()
    result = {}
    waiter = threading.Thread(target=lambda: result.update(srv.wait(timeout=30)))
    waiter.start()
    try:
        with httpx.Client(base_url=srv.url, timeout=30) as c:
            ont = Ontology.model_validate(c.get("/api/ontology").json()["ontology"])
            gate1_edits(ont)
            r = c.post("/api/ontology/approve", json={"ontology": ont.model_dump(mode="json")}, headers={"Origin": srv.url})
            assert r.status_code == 200, r.text
        waiter.join(15)
    finally:
        srv.stop()
    run["gate1"] = result
    run["compile"] = kg("compile-structured", *w)
    run["media"] = kg("media-prep", *w)
    run["batches"] = kg("export-batches", *w)

    # Tier-1 harness answers: genuine text claims, one hallucination, and VLM claims for the scanned memo + diagram.
    chunks = ws.load_chunks()
    report = next(c for c in chunks.values() if c.doc_id.startswith("report-") and c.page_no == 1)
    vlm = {it["source_kind"] + (f":{it['page_no']}" if it.get("page_no") else ""): it for it in iter_jsonl(ws.vlm_batches_path)}
    answers = [
        {"chunk_id": report.chunk_id, "kind": "edge", "subject": "Elena Voss", "predicate": "LEADS", "object": "Project Atlas",
         "quote": "Elena Voss leads Project Atlas, the flagship autonomy program", "confidence": 0.95},
        {"chunk_id": report.chunk_id, "kind": "edge", "subject": "Elena Voss", "predicate": "WORKS_AT", "object": "Initech Labs",
         "quote": "Elena Voss secretly works at Initech Labs on weekends", "confidence": 0.95},
        {"image_item_id": vlm["scanned_pdf_page:1"]["item_id"], "kind": "edge", "subject": {"name": "Initech Labs", "label": "Company"},
         "predicate": "PARTNERS_WITH", "object": {"name": "Acme Robotics", "label": "Company"},
         "element_description": "typed memo line: 'Initech Labs signed a supply agreement with Acme Robotics'",
         "bbox_norm": [0.08, 0.12, 0.92, 0.22], "confidence": 0.85},
        {"image_item_id": vlm["image"]["item_id"], "kind": "edge", "subject": {"name": "Computer Vision", "label": "Technology"},
         "predicate": "USED_BY", "object": {"name": "Project Atlas", "label": "Project"},
         "element_description": "diagram box 'Vision Sensors' wired to 'Atlas Control Plane'", "bbox_norm": [0.72, 0.1, 0.96, 0.31],
         "confidence": 0.8},
    ]
    first = json.loads(Path(run["batches"]["index"]).read_text(encoding="utf-8"))["batches"][0]["output_file"]
    Path(first).write_text("".join(json.dumps(a) + "\n" for a in answers), encoding="utf-8")
    run["import"] = kg("import-candidates", first, *w)
    run["heuristic"] = kg("extract", *w, "--extractor", "heuristic")
    run["verify"] = kg("verify", *w)
    run["report"] = read_json(ws.verify_path)

    # Gate 2 through the Studio API: approve auto-verified + visually confirmed image proposals, commit.
    srv = StudioServer(ws, port=0).start()
    try:
        with httpx.Client(base_url=srv.url, timeout=60) as c:
            props = c.get("/api/candidates").json()["proposals"]
            decisions = [{"proposal_id": p["proposal_id"], "decision": "approve"} for p in props
                         if p["bucket"] == "auto" or (p["bucket"] == "review" and "image" in p["modalities"])]
            run["gate2"] = c.post("/api/edges/decide", json={"decisions": decisions, "commit": True}, headers={"Origin": srv.url}).json()
    finally:
        srv.stop()
    run["ws"] = ws
    return run


def test_torture_audit_flags_every_modality_and_failure(torture):
    a = torture["audit"]
    assert all(a["modality_breakdown"][m] > 0 for m in ("structured", "semi_structured", "notebook_vault", "unstructured", "image"))
    scanned = next(n for n in a["needs_ocr_or_vlm"] if n["doc_id"].startswith("scanned-memo"))
    assert scanned["pages"] == [1, 2] and scanned["options"] == ["harness_vlm", "byok_vlm", "local_ocr", "skip"]
    reasons = {x["path"]: x["reason"] for x in a["excluded_sources"]}
    assert reasons["empty.pdf"] == "empty file (0 bytes)" and reasons["corrupt.pdf"].startswith("corrupt or unreadable PDF")
    assert reasons["diagram_copy.png"].startswith("near-identical image")
    assert torture["media"]["rendered_pages"] == 2 and torture["media"]["items"] == 4
    assert torture["gate1"]["gate"] == "gate1" and torture["ws"].load_manifest().gates["gate1"] == "approved"


def test_torture_structured_evidence_is_verified(torture):
    rep = torture["report"]
    assert torture["compile"]["candidates"] > 30
    kinds = {}
    for p in rep["proposals"]:
        for m in p["modalities"]:
            kinds.setdefault(m, []).append(p["bucket"])
    for m in ("table", "json", "notebook", "text", "image"):
        assert m in kinds, m
    structured = [r for r in rep["candidates"].values() if r["extractor"] == "structured"]
    assert structured and all(r["verdict"] == "verified" for r in structured)
    g = torture["ws"].load_graph()
    edges = {(e.source_id, e.type, e.target_id) for e in g.edges}
    assert ("project:project-atlas", "HAS_MILESTONE", "milestone:atlas-beta") in edges          # nested JSON
    assert ("technology:computer-vision", "USED_BY", "project:project-borealis") in edges      # YAML ids
    assert ("notebook:atlas-fleet-analysis", "IMPORTS", "library:sklearn") in edges            # notebook code cell
    assert ("person:elena-voss", "WORKS_ON", "project:project-atlas") in edges                 # Obsidian inline field
    kinds_in_graph = {x.kind for e in g.edges for x in e.evidence}
    assert {"table", "json", "notebook", "text", "image"} <= kinds_in_graph


def test_torture_hallucination_quarantined_with_full_precision(torture):
    rep = torture["report"]
    harness = [r for r in rep["candidates"].values() if r["extractor"] == "harness"]
    rejected = [r for r in harness if r["verdict"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["object"]["name"] == "Initech Labs"
    assert all(r["verdict"] in ("verified", "needs_visual") for r in harness if r not in rejected)
    q = [p for p in rep["proposals"] if p["bucket"] == "quarantine"]
    assert [(p["source"]["name"], p["predicate"], p["target"]["name"]) for p in q] == [("Elena Voss", "WORKS_AT", "Initech Labs")]
    g = torture["ws"].load_graph()
    assert not any(e.source_id == "person:elena-voss" and e.target_id == "company:initech-labs" for e in g.edges)


def test_torture_image_evidence_pipeline(torture):
    rep = torture["report"]
    img = [p for p in rep["proposals"] if "image" in p["modalities"]]
    assert len(img) == 2 and all(p["bucket"] == "review" for p in img)
    assert all(any("visual confirmation" in r for r in p["reasons"]) for p in img)
    g = torture["ws"].load_graph()
    ev = [x for e in g.edges for x in e.evidence if x.kind == "image"]
    assert len(ev) == 2 and all((torture["ws"].media_dir / x.image_path).exists() for x in ev)
    assert {x.page_no for x in ev} == {1, None}


def test_torture_contradiction_flagged_by_doctor(torture):
    doc = kg("doctor", *torture["w"])
    hq = [c for c in doc["contradictions"] if c["predicate"] == "HEADQUARTERED_IN" and c["source"] == "Acme Robotics"]
    assert hq and set(hq[0]["values"]) == {"Berlin", "Munich"}
    assert doc["grounding_pct"] == 100.0 and doc["dangling"] == 0 and doc["source_integrity_pct"] == 100.0


def test_torture_bitemporal_update_preserves_history(torture):
    w = torture["w"]
    ws = torture["ws"]
    old = next(e for e in ws.load_graph().edges if e.type == "WORKS_AT" and e.source_id == "person:elena-voss")
    res = kg("patch", "supersede-edge", *w, "--edge-id", old.id, "--target", "company:globex-dynamics", "--valid-from", "2023-06-01",
             "--quote", "In June 2023 Elena Voss joined Globex Dynamics as Chief Scientist")
    assert res["ok"]

    def employer(as_of=None):
        args = ["query", "Where does Elena Voss work?", *w, "--format", "json"] + (["--as-of", as_of] if as_of else [])
        return {e["target_name"] for e in kg(*args)["edges"] if e["type"] == "WORKS_AT" and e["source_name"] == "Elena Voss"}

    assert employer("2020-05-01") == {"Acme Robotics"} and employer("2024-05-01") == {"Globex Dynamics"} == employer()
    hist = kg("history", *w, "--id", old.id)["changes"]
    assert hist[-1]["op"] == "supersede-edge" and hist[-1]["before"]["valid_to"] is None
    g = ws.load_graph().edge_map()
    assert g[old.id].status == "superseded" and g[old.id].valid_to == "2023-06-01" and g[old.id].superseded_by in g


def test_torture_eval_and_all_exports(torture):
    w = torture["w"]
    ev = kg("eval", *w, "--samples", "10")
    assert ev["pass"] and ev["summary"]["ppr"]["citation_grounding_pct"] == 100.0
    ex = kg("export", *w, "--target", "all")
    assert ex["ok"] and len(ex["results"]) == 10
    ve = kg("verify-export", *w, "--target", "all")
    assert ve["passed"] == 10


# =============================================================================================
# Part B: real-world corpus ./data/ (Alice's Adventures in Wonderland)
# =============================================================================================

ALICE_WS = REPO_ROOT / ".kumokg_alice_demo"


@pytest.fixture(scope="module")
def alice():
    before = {p.name: sha256_file(p) for p in DATA_DIR.iterdir() if p.is_file()}
    out = build_alice_demo(ALICE_WS, DATA_DIR, log=lambda *_: None)
    after = {p.name: sha256_file(p) for p in DATA_DIR.iterdir() if p.is_file()}
    out["data_unchanged"] = before == after
    out["ws"] = Workspace(ALICE_WS)
    return out


def test_alice_audit_and_sampling(alice):
    assert alice["data_unchanged"], "./data/ must never be modified"
    files = {f["name"]: f for f in alice["audit"]["files"]}
    pdf = files["Alice's Adventures In Wonderland.pdf"]
    assert pdf["status"] == "usable" and pdf["pages"] == 80 and pdf["median_chars_per_page"] > 1500
    md = files["AliceInWonderLand.md"]
    assert md["status"] == "duplicate" and md["similarity"] >= 0.98
    assert any("duplicate" in x["reason"] for x in alice["audit"]["excluded_sources"]) and alice["audit"]["needs_ocr_or_vlm"] == []
    assert alice["chunk"]["chunks"] > 100
    whys = {s["why"] for s in alice["sample"]["sample"]}
    assert {"beginning", "middle", "end"} <= whys and len(alice["sample"]["sample"]) == 12
    terms = {t["term"] for t in alice["sample"]["discovered_terms"]}
    assert {"Alice", "Queen", "Hatter", "Mock Turtle", "White Rabbit"} <= terms


def test_alice_ontology_and_graph_content(alice):
    ws = alice["ws"]
    ont = ws.load_ontology(True)
    assert {"Character", "Location", "Object", "Event", "Size"} <= ont.class_names()
    g = ws.load_graph()
    names = {n.canonical_name for n in g.nodes}
    for n in ("Alice", "White Rabbit", "Queen of Hearts", "Cheshire Cat", "Mad Hatter", "Caterpillar", "Wonderland", "Rabbit-Hole",
              "Croquet-Ground", "Courtroom", "Drink Me Bottle", "Eat Me Cake", "Pocket Watch"):
        assert n in names, n
    preds = {e.type for e in g.edges}
    assert {"ENCOUNTERS", "LOCATED_IN", "RULES", "TRANSFORMS_VIA", "ARGUES_WITH", "ATTENDS"} <= preds
    assert len(g.nodes) >= 40 and len(g.edges) >= 60
    triples = {(e.source_id, e.type, e.target_id) for e in g.edges}
    for t in [("character:alice", "ENCOUNTERS", "character:white-rabbit"), ("character:queen-of-hearts", "RULES", "location:croquet-ground"),
              ("character:alice", "TRANSFORMS_VIA", "object:drink-me-bottle"), ("character:white-rabbit", "CARRIES", "object:pocket-watch"),
              ("character:alice", "ARGUES_WITH", "character:mad-hatter"), ("character:mad-hatter", "ATTENDS", "event:mad-tea-party"),
              ("character:king-of-hearts", "PRESIDES_OVER", "event:trial-of-the-knave"), ("character:alice", "LOCATED_IN", "location:wonderland")]:
        assert t in triples, t


def test_alice_every_committed_edge_has_a_verified_verbatim_quote(alice):
    ws = alice["ws"]
    g = ws.load_graph()
    ctx = VerifyContext(ws)
    chunks = ws.load_chunks()
    for e in g.edges:
        assert e.evidence, e.id
        for ev in e.evidence:
            assert ev.kind == "text"
            chk = verify_evidence(ev, ctx)
            assert chk["status"] == "verified", (e.type, ev.verbatim_quote, chk)
            assert " ".join(ev.verbatim_quote.split()).lower().replace("‐", "-") in \
                " ".join(chunks[ev.chunk_id].text.split()).lower().replace("‐", "-")
            assert ev.page_no and 1 <= ev.page_no <= 80
    doc = alice["doctor"]
    assert doc["grounding_pct"] == 100.0 and doc["schema_compliance_pct"] == 100.0 and doc["source_integrity_pct"] == 100.0
    assert doc["dangling"] == 0 and doc["score"] >= 95


def test_alice_gate2_has_realistic_pending_review_set(alice):
    s = alice["verify"]["summary"]
    assert s["buckets"]["review"] >= 8 and s["buckets"]["conflict"] >= 2 and s["buckets"]["quarantine"] == 2
    assert s["soft_verified"] >= 1 and s["schema_extensions"] >= 1 and s["contradictions"] >= 1
    rep = read_json(alice["ws"].verify_path)
    quarantined = {(p["source"]["name"], p["predicate"], p["target"]["name"]) for p in rep["proposals"] if p["bucket"] == "quarantine"}
    assert quarantined == {("Cheshire Cat", "RULES", "Wonderland"), ("Mad Hatter", "STEALS", "Tarts")}
    assert alice["doctor"]["review_queue"]["undecided_proposals"] >= 10
    assert any(c["predicate"] == "HAS_SIZE" for c in rep["contradictions"])
    # the chat patch turned Ten Inches -> Nine Feet into a bi-temporal sequence, so it is no longer a pending conflict
    assert not any(c["predicate"] == "HAS_SIZE" for c in alice["doctor"]["contradictions"])


def test_alice_queries_three_modes(alice):
    local = alice["query_local"]["context"]
    assert "Mad Tea-Party" in local and ("March Hare" in local or "Mad Hatter" in local) and "p." in local
    ppr = alice["query_ppr"]["context"]
    assert "Knave of Hearts" in ppr and "Paths:" in ppr
    glob = alice["query_global"]["context"]
    assert glob.startswith("[lazy-global]") and glob.count("Community") >= 2


def test_alice_bitemporal_patch_and_time_travel(alice):
    assert alice["patch"]["ok"] and alice["patch"]["applied"][0]["op"] == "supersede-edge"
    before = {(e["type"], e["target_name"]) for e in alice["size_before"]["edges"] if e["source_name"] == "Alice"}
    after = {(e["type"], e["target_name"]) for e in alice["size_after"]["edges"] if e["source_name"] == "Alice"}
    assert ("HAS_SIZE", "Ten Inches High") in before and ("HAS_SIZE", "Nine Feet High") not in before
    assert ("HAS_SIZE", "Nine Feet High") in after and ("HAS_SIZE", "Ten Inches High") not in after
    g = alice["ws"].load_graph()
    ten = next(e for e in g.edges if e.type == "HAS_SIZE" and e.target_id == "size:ten-inches-high")
    nine = g.edge_map()[ten.superseded_by]
    assert ten.status == "superseded" and (ten.valid_from, ten.valid_to) == (NARRATIVE_T0, NARRATIVE_T1)
    assert nine.valid_from == NARRATIVE_T1 and nine.evidence[0].page_no == 8  # Chapter II, "The Pool of Tears"
    assert alice["history"]["changes"][-1]["op"] == "supersede-edge"
    log = list(iter_jsonl(alice["ws"].changelog_path))
    assert [r["op"] for r in log] == ["commit", "add-node", "supersede-edge"]  # the patch created the Nine Feet High node
    assert log[1]["after"]["id"] == "size:nine-feet-high" and log[2]["before"]["id"] == ten.id


def test_alice_eval_and_ten_database_exports(alice):
    ev = alice["eval"]
    assert ev["pass"] and ev["questions"] == 15
    assert ev["summary"]["ppr"]["entity_recall@10"] >= 0.6 and ev["summary"]["ppr"]["citation_grounding_pct"] == 100.0
    assert ev["summary"]["local"]["citation_grounding_pct"] == 100.0
    assert alice["export"]["ok"] and len(alice["export"]["results"]) == 10
    ve = alice["verify_export"]
    assert ve["ok"] and ve["passed"] == 10
    for r in ve["results"]:
        assert r["dangling"] == 0 and r["syntax_ok"] and r["nodes_found"] == r["nodes_expected"] and r["edges_found"] == r["edges_expected"]
    st = alice["status"]
    assert st["gates"] == {"gate1": "approved", "gate2": "committed"} and st["graph"]["nodes"] == len(alice["ws"].load_graph().nodes)

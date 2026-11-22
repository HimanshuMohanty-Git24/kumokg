"""Milestone 4: 3-mode retrieval, bi-temporal patching, eval harness and the MCP server."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest
from typer.testing import CliRunner

from core import eval_harness, mcp_server, patcher, retrieval, verifier, worker_pool as wp
from core.ir import Edge, GraphStore, iter_jsonl, read_json


@pytest.fixture()
def committed_ws(synthetic_ws):
    ws = synthetic_ws
    wp.extract_heuristic(ws)
    verifier.verify(ws)
    patcher.auto_decide(ws)
    patcher.commit(ws)
    return ws


def _chunk(ws, needle):
    return next(c for c in ws.load_chunks().values() if needle in " ".join(c.text.split()))


def _works_edge(ws, person="person:elena-voss"):
    return next(e for e in ws.load_graph().edges if e.type == "WORKS_AT" and e.source_id == person and e.status == "approved")


# ---------------------------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------------------------


def test_local_bm25_neighbourhood_and_budget(committed_ws):
    ws = committed_ws
    res = retrieval.query(ws, "Who leads Project Atlas?", mode="local", fmt="json")
    assert any(e["type"] == "LEADS" and e["source_name"] == "Elena Voss" for e in res["edges"])
    assert res["seeds"][0]["name"] == "Project Atlas"
    compact = retrieval.query(ws, "Who leads Project Atlas?", mode="local")
    assert "Elena Voss -LEADS-> Project Atlas" in compact["context"] and "report.pdf p.1" in compact["context"]
    small = retrieval.query(ws, "Who leads Project Atlas?", mode="local", max_tokens=140, fmt="json")
    assert 1 <= len(small["edges"]) < len(res["edges"]) and small["tokens_estimate"] <= 200
    hops1 = retrieval.query(ws, "Initech Labs", mode="local", hops=1, fmt="json")
    hops2 = retrieval.query(ws, "Initech Labs", mode="local", hops=2, fmt="json")
    assert len(hops2["nodes"]) >= len(hops1["nodes"])


def test_ppr_multihop_paths_and_ontology_constraint(committed_ws):
    ws = committed_ws
    res = retrieval.query(ws, "How is Tomas Novak connected to Project Borealis?", mode="ppr", hops=3, fmt="json")
    assert {s["name"] for s in res["seeds"]} >= {"Tomas Novak", "Project Borealis"}
    ids = [n["id"] for n in res["nodes"]]
    assert "company:initech-labs" in ids
    assert res["paths"] and all(p[0] in {s["id"] for s in res["seeds"]} for p in res["paths"])
    assert set(res["path_edges"]) <= {e["id"] for e in res["edges"]} | set(res["path_edges"])
    gi = retrieval.GraphIndex(ws)
    g = gi.graph
    g.edges.append(Edge(id="e-offgrammar", source_id="city:munich", target_id="person:tomas-novak", type="ADMIRES"))
    G = gi.constrained_graph(None)
    assert not G.has_edge("city:munich", "person:tomas-novak")
    assert G.has_edge("person:tomas-novak", "company:initech-labs")
    pr = retrieval.personalized_pagerank(G, {"person:tomas-novak": 1.0})
    top3 = sorted(pr, key=pr.get, reverse=True)[:3]
    assert abs(sum(pr.values()) - 1.0) < 1e-6 and "person:tomas-novak" in top3 and "company:initech-labs" in top3
    assert pr["person:tomas-novak"] > pr["city:paris"]


def test_lazy_global_communities_cached_and_deterministic(committed_ws):
    ws = committed_ws
    gi = retrieval.GraphIndex(ws)
    c1 = gi.communities()
    store = GraphStore(ws.sqlite_path)
    assert store.load_communities(gi.graph.content_hash()) == c1
    c2 = retrieval.GraphIndex(ws).communities()
    assert [c["members"] for c in c1] == [c["members"] for c in c2] and len(c1) >= 2
    res = retrieval.query(ws, "warehouse robotics companies and their cities", mode="lazy-global", fmt="json")
    assert res["communities"] and res["communities"][0]["hubs"] and res["communities"][0]["claims"]
    assert res["community_count"] == len(c1)
    compact = retrieval.query(ws, "robotics", mode="lazy-global")
    assert compact["context"].startswith("[lazy-global]") and "Community" in compact["context"]


# ---------------------------------------------------------------------------------------------
# patching
# ---------------------------------------------------------------------------------------------


def test_supersede_edge_preserves_history_and_time_travel(committed_ws):
    ws = committed_ws
    old = _works_edge(ws)
    assert old.valid_from == "2019-03-01" and old.target_id == "company:acme-robotics"
    ch = _chunk(ws, "Elena Voss joined Globex Dynamics")
    res = patcher.apply_patch(ws, {"message": "Elena moved", "ops": [{"op": "supersede-edge", "edge_id": old.id,
        "valid_from": "2023-06-01", "new": {"target": "company:globex-dynamics"},
        "quote": "In June 2023 Elena Voss joined Globex Dynamics as Chief Scientist", "chunk_id": ch.chunk_id}]})
    assert res["ok"], res
    g = ws.load_graph()
    em = g.edge_map()
    o = em[old.id]
    new = em[res["applied"][0]["new_edge_id"]]
    assert o.status == "superseded" and o.valid_to == "2023-06-01" and o.superseded_by == new.id
    assert new.status == "approved" and new.valid_from == "2023-06-01" and new.evidence[0].page_no == 3
    def employer(as_of):
        r = retrieval.query(ws, "Where does Elena Voss work?", mode="local", as_of=as_of, fmt="json")
        return {e["target_name"] for e in r["edges"] if e["type"] == "WORKS_AT" and e["source_name"] == "Elena Voss"}
    assert employer("2021-01-01") == {"Acme Robotics"}
    assert employer("2024-01-01") == {"Globex Dynamics"} == employer(None)
    assert employer("2018-01-01") == set()
    log = list(iter_jsonl(ws.changelog_path))
    assert log[-1]["op"] == "supersede-edge" and log[-1]["before"]["id"] == old.id and log[-1]["message"] == "Elena moved"
    card = verifier.doctor(ws, deep=False)
    assert card["grounding"]["pct"] == 100.0 and card["edges"]["superseded"] == 1
    assert not any(c["predicate"] == "WORKS_AT" and c["scope"] == "graph" for c in card["contradictions"])
    tt = mcp_server.KGTools(ws).kg_time_travel("2020-06-01", entity="Elena Voss")
    assert any(f["type"] == "WORKS_AT" and f["target_name"] == "Acme Robotics" for f in tt["facts"])
    bad = patcher.apply_patch(ws, {"ops": [{"op": "supersede-edge", "edge_id": old.id, "valid_from": "2024-01-01",
                                             "new": {"target": "company:initech-labs"}, "quote": "Initech Labs is headquartered in Munich"}]})
    assert not bad["ok"] and "not currently active" in bad["error"]


def test_patch_ops_verification_and_atomicity(committed_ws):
    ws = committed_ws
    before = ws.load_graph().content_hash()
    fake = patcher.apply_patch(ws, {"ops": [{"op": "add-edge", "source": "person:marcus-lee", "type": "WORKS_AT",
                                              "target": "company:initech-labs", "quote": "Marcus Lee works at Initech Labs since 1990"}]})
    assert not fake["ok"] and "quote not found" in fake["error"]
    multi = patcher.apply_patch(ws, {"ops": [
        {"op": "add-alias", "node_id": "person:marcus-lee", "alias": "M. Lee"},
        {"op": "add-edge", "source": "person:marcus-lee", "type": "TELEPORTS_TO", "target": "city:paris",
         "quote": "Marcus Lee leads Project Borealis from the Paris office"}]})
    assert not multi["ok"] and "closed-world" in multi["error"]
    assert ws.load_graph().content_hash() == before, "failed multi-op patch must not apply anything"
    assert "M. Lee" not in ws.load_graph().node_map()["person:marcus-lee"].aliases
    dry = patcher.apply_patch(ws, {"ops": [{"op": "add-alias", "node_id": "person:marcus-lee", "alias": "M. Lee"}]}, dry_run=True)
    assert dry["ok"] and dry["dry_run"] and ws.load_graph().content_hash() == before
    ok = patcher.apply_patch(ws, {"allow_extension": True, "ops": [
        {"op": "add-node", "label": "Office", "name": "Paris Office", "quote": "Marcus Lee leads Project Borealis from the Paris office"},
        {"op": "add-edge", "source": "person:marcus-lee", "type": "BASED_AT", "target": "office:paris-office",
         "quote": "Marcus Lee leads Project Borealis from the Paris office"},
        {"op": "add-alias", "node_id": "person:marcus-lee", "alias": "M. Lee"}]})
    assert ok["ok"] and ok["ontology_extended"], ok
    ont = ws.load_ontology(True)
    assert ont.get_class("Office") and ont.allows("Person", "BASED_AT", "Office")
    g = ws.load_graph()
    assert "M. Lee" in g.node_map()["person:marcus-lee"].aliases
    based = next(e for e in g.edges if e.type == "BASED_AT")
    assert based.properties["origin"] == "patch" and based.evidence[0].kind == "text"
    r = patcher.apply_patch(ws, {"ops": [{"op": "retract-edge", "edge_id": based.id, "valid_to": "2025-01-01", "reason": "office closed"}]})
    assert r["ok"]
    e = ws.load_graph().edge_map()[based.id]
    assert e.status == "superseded" and e.valid_to == "2025-01-01" and e.properties["retracted"]
    unverified = patcher.apply_patch(ws, {"ops": [{"op": "add-edge", "source": "person:priya-raman", "type": "LEADS",
                                                    "target": "project:project-borealis"}]})
    assert not unverified["ok"] and "needs evidence" in unverified["error"]
    asserted = patcher.apply_patch(ws, {"allow_unverified": True, "ops": [{"op": "add-edge", "source": "person:priya-raman",
                                                                           "type": "LEADS", "target": "project:project-borealis"}]})
    assert asserted["ok"]
    card = verifier.doctor(ws, deep=False)
    assert card["grounding"]["pct"] < 100.0  # the unverified user assertion is surfaced by doctor
    ops = [row["op"] for row in iter_jsonl(ws.changelog_path)]
    assert ops.count("add-alias") == 1 and "retract-edge" in ops and "add-node" in ops


def test_merge_nodes_redirects_edges(committed_ws):
    ws = committed_ws
    res = patcher.apply_patch(ws, {"allow_extension": True, "ops": [
        {"op": "add-node", "label": "Company", "name": "Acme Robotics GmbH", "quote": "Acme Robotics is headquartered in Munich"},
        {"op": "add-edge", "source": "company:acme-robotics-gmbh", "type": "HEADQUARTERED_IN", "target": "city:munich",
         "quote": "Acme Robotics is headquartered in Munich"}]})
    assert res["ok"], res
    merged = patcher.apply_patch(ws, {"ops": [{"op": "merge-nodes", "keep": "company:acme-robotics", "merge": ["company:acme-robotics-gmbh"]}]})
    assert merged["ok"]
    g = ws.load_graph()
    nodes = g.node_map()
    assert "company:acme-robotics-gmbh" not in nodes and "Acme Robotics GmbH" in nodes["company:acme-robotics"].aliases
    assert any(e.source_id == "company:acme-robotics" and e.target_id == "city:munich" for e in g.edges)
    assert verifier.doctor(ws, deep=False)["dangling_edges"] == []


def test_cli_query_patch_history(committed_ws):
    from kumokg_cli import app

    ws = committed_ws
    runner = CliRunner()
    w = ["--workspace", str(ws.root)]
    r = runner.invoke(app, ["query", "Who leads Project Borealis?", "--mode", "ppr"] + w)
    assert r.exit_code == 0 and "Marcus Lee" in json.loads(r.output)["context"]
    r = runner.invoke(app, ["query", "x", "--mode", "nope"] + w)
    assert r.exit_code == 1
    old = _works_edge(ws, "person:priya-raman")
    r = runner.invoke(app, ["patch", "supersede-edge", "--edge-id", old.id, "--target", "company:globex-dynamics",
                            "--valid-from", "2024-02-01", "--quote", "Globex Dynamics partners with Acme Robotics",
                            "--message", "Priya joined Globex"] + w)
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["history", "--id", old.id] + w)
    changes = json.loads(r.output)["changes"]
    assert changes[-1]["op"] == "supersede-edge" and changes[-1]["author"] == "cli"
    r = runner.invoke(app, ["patch", "add-edge", "--source", "Priya Raman", "--type", "LEADS", "--target", "Project Atlas",
                            "--quote", "Priya Raman leads nothing at all"] + w)
    assert r.exit_code == 1


# ---------------------------------------------------------------------------------------------
# eval harness
# ---------------------------------------------------------------------------------------------


def test_eval_suite_and_metrics(committed_ws):
    ws = committed_ws
    rep = eval_harness.run_eval(ws, samples=12, k=10)
    suite = read_json(ws.p("eval_suite.json"))["questions"]
    assert len(suite) == 12 and {q["hops"] for q in suite} == {2, 3}
    edges = ws.load_graph().edge_map()
    for q in suite:
        assert len(q["path_edge_ids"]) == q["hops"] and len(q["path_node_ids"]) == q["hops"] + 1
        assert all(eid in edges for eid in q["path_edge_ids"])
        assert q["question"].startswith("Starting from")
    again = eval_harness.build_suite(ws, samples=12)
    assert [q["path_edge_ids"] for q in again] == [q["path_edge_ids"] for q in suite]
    for mode in ("local", "ppr"):
        s = rep["summary"][mode]
        assert s["citation_grounding_pct"] == 100.0 and 0 <= s["entity_recall@10"] <= 1 and s["latency_ms_mean"] > 0
    assert rep["summary"]["ppr"]["entity_recall@10"] >= 0.5 and rep["pass"]
    assert read_json(ws.p("eval_report.json"))["questions"] == 12


# ---------------------------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------------------------


def test_mcp_server_tools_registered_and_callable(committed_ws):
    server = mcp_server.build_server(committed_ws)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == set(mcp_server.TOOL_NAMES)
    q = next(t for t in tools if t.name == "kg_query")
    schema = getattr(q, "inputSchema", None) or getattr(q, "input_schema", None)
    assert "question" in json.dumps(schema)
    result = asyncio.run(server.call_tool("kg_query", {"question": "Who leads Project Atlas?"}))
    assert "Elena Voss" in str(result)
    tools_impl = mcp_server.KGTools(committed_ws)
    doc = tools_impl.kg_doctor()
    assert doc["grounding_pct"] == 100.0
    ev = tools_impl.kg_get_evidence("company:acme-robotics")
    assert ev["kind"] == "node" and ev["evidence"]
    multi = tools_impl.kg_ppr_multihop("Tomas Novak and Project Borealis")
    assert multi["paths"] and multi["mode"] == "ppr"
    patched = tools_impl.kg_patch({"ops": [{"op": "add-alias", "node_id": "person:tomas-novak", "alias": "T. Novak"}]}, dry_run=True)
    assert patched["ok"] and patched["dry_run"]


def test_mcp_missing_extra_gives_install_hint(committed_ws, monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)
    with pytest.raises(mcp_server.McpUnavailable, match="--extra mcp"):
        mcp_server.build_server(committed_ws)
    from kumokg_cli import app

    r = CliRunner().invoke(app, ["serve-mcp", "--workspace", str(committed_ws.root)])
    assert r.exit_code == 2 and "--extra mcp" in r.output

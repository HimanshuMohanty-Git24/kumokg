"""Synthetic multi-hop GraphRAG benchmark.

``build_suite`` deterministically samples verified 2-hop and 3-hop paths from the committed graph and turns
them into golden questions tied to ground-truth node/edge ids (``.kumokg/eval_suite.json``). ``run_eval`` runs
every question through ``local`` and ``ppr`` retrieval and reports Entity Recall@K, Edge Recall@K, Answer
Hit@K, Citation Grounding % and latency (``.kumokg/eval_report.json``).
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from typing import Any

from .ir import Edge, Workspace, parse_evidence, utcnow_iso, write_json
from .retrieval import GraphIndex, active_edges
from .verifier import GROUNDED, VerifyContext, verify_evidence


def _phrase(pred: str) -> str:
    return pred.replace("_", " ").lower()


def _question(names: dict[str, str], labels: dict[str, str], steps: list[tuple[Edge, bool]], start: str, end: str) -> str:
    hops = []
    for e, forward in steps:
        hops.append(_phrase(e.type) if forward else f"is {_phrase(e.type)} by/of")
    chain = ", then ".join(hops)
    return f"Starting from {names[start]}, follow {chain}: which {labels[end]} do you reach?"


def build_suite(ws: Workspace, samples: int = 20, seed: int = 13, hops_mix: tuple[int, ...] = (2, 3)) -> list[dict[str, Any]]:
    graph = ws.load_graph()
    nodes = graph.node_map()
    names = {n.id: n.canonical_name for n in graph.nodes}
    labels = {n.id: n.label for n in graph.nodes}
    edges = [e for e in active_edges(graph) if e.evidence and e.source_id in nodes and e.target_id in nodes]
    adj: dict[str, list[tuple[Edge, bool, str]]] = defaultdict(list)
    for e in sorted(edges, key=lambda x: x.id):
        adj[e.source_id].append((e, True, e.target_id))
        adj[e.target_id].append((e, False, e.source_id))
    rng = random.Random(seed)
    starts = sorted(n for n in adj if len(adj[n]) > 0)
    suite: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    attempts = 0
    while len(suite) < samples and attempts < samples * 400 and starts:
        attempts += 1
        h = hops_mix[len(suite) % len(hops_mix)]
        start = rng.choice(starts)
        path_nodes = [start]
        steps: list[tuple[Edge, bool]] = []
        cur = start
        ok = True
        for _ in range(h):
            options = [(e, fwd, nxt) for e, fwd, nxt in adj[cur] if nxt not in path_nodes]
            if not options:
                ok = False
                break
            e, fwd, nxt = rng.choice(options)
            steps.append((e, fwd))
            path_nodes.append(nxt)
            cur = nxt
        if not ok:
            continue
        key = tuple(e.id for e, _ in steps)
        if key in seen:
            continue
        seen.add(key)
        end = path_nodes[-1]
        suite.append({
            "qid": f"q{len(suite) + 1:03d}", "hops": h, "question": _question(names, labels, steps, start, end),
            "start_id": start, "answer_id": end, "answer_name": names[end], "path_node_ids": path_nodes,
            "path_edge_ids": [e.id for e, _ in steps], "predicates": [e.type for e, _ in steps],
            "directions": ["forward" if f else "inverse" for _, f in steps],
        })
    payload = {"generated_at": utcnow_iso(), "seed": seed, "graph_hash": graph.content_hash(), "questions": suite}
    write_json(ws.p("eval_suite.json"), payload)
    return suite


def _ranked_nodes(res: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for group in (res.get("seeds", []), res.get("nodes", [])):
        for n in group:
            if n["id"] not in out:
                out.append(n["id"])
    for e in res.get("edges", []):
        for nid in (e["source"], e["target"]):
            if nid not in out:
                out.append(nid)
    return out


def run_eval(ws: Workspace, samples: int = 20, k: int = 10, seed: int = 13, modes: tuple[str, ...] = ("local", "ppr"),
             min_entity_recall: float = 0.5, max_tokens: int = 2000) -> dict[str, Any]:
    ws.require()
    suite = build_suite(ws, samples=samples, seed=seed)
    gi = GraphIndex(ws)
    emap = gi.graph.edge_map()
    ctx = VerifyContext(ws)
    ground_cache: dict[str, bool] = {}

    def grounded(edge_id: str) -> bool:
        if edge_id not in ground_cache:
            e = emap.get(edge_id)
            ground_cache[edge_id] = bool(e and e.evidence) and all(
                verify_evidence(parse_evidence(x.model_dump(mode="json")), ctx)["status"] in GROUNDED for x in e.evidence[:3])
        return ground_cache[edge_id]

    import time

    per_mode: dict[str, dict[str, list]] = {m: defaultdict(list) for m in modes}
    details = []
    for q in suite:
        row = {"qid": q["qid"], "hops": q["hops"], "question": q["question"], "answer": q["answer_name"], "modes": {}}
        for mode in modes:
            t0 = time.perf_counter()
            if mode == "local":
                res = gi.local(q["question"], hops=q["hops"], max_tokens=max_tokens)
            elif mode == "ppr":
                res = gi.ppr(q["question"], hops=q["hops"], max_tokens=max_tokens)
            else:
                res = gi.lazy_global(q["question"], max_tokens=max_tokens)
            ms = (time.perf_counter() - t0) * 1000
            top_nodes = _ranked_nodes(res)[:k]
            top_edges = [e["id"] for e in res.get("edges", [])][: 2 * k]
            ent = len(set(q["path_node_ids"]) & set(top_nodes)) / len(q["path_node_ids"])
            edg = len(set(q["path_edge_ids"]) & set(top_edges)) / len(q["path_edge_ids"])
            hit = 1.0 if q["answer_id"] in top_nodes else 0.0
            cites = [grounded(eid) for eid in top_edges]
            m = per_mode[mode]
            m["entity_recall"].append(ent)
            m["edge_recall"].append(edg)
            m["answer_hit"].append(hit)
            m["latency_ms"].append(ms)
            m["citations"].extend(cites)
            m[f"entity_recall_{q['hops']}hop"].append(ent)
            row["modes"][mode] = {"entity_recall": round(ent, 3), "edge_recall": round(edg, 3), "answer_hit": hit,
                                  "latency_ms": round(ms, 2), "edges_returned": len(top_edges)}
        details.append(row)

    def mean(xs):
        return round(statistics.mean(xs), 4) if xs else 0.0

    summary = {}
    for mode, m in per_mode.items():
        lat = sorted(m["latency_ms"])
        summary[mode] = {
            f"entity_recall@{k}": mean(m["entity_recall"]), f"edge_recall@{2 * k}": mean(m["edge_recall"]),
            f"answer_hit@{k}": mean(m["answer_hit"]),
            "entity_recall_2hop": mean(m["entity_recall_2hop"]), "entity_recall_3hop": mean(m["entity_recall_3hop"]),
            "citation_grounding_pct": round(100.0 * sum(m["citations"]) / len(m["citations"]), 2) if m["citations"] else 100.0,
            "latency_ms_mean": mean(lat), "latency_ms_p95": round(lat[int(0.95 * (len(lat) - 1))], 2) if lat else 0.0,
        }
    best = max((summary[m][f"entity_recall@{k}"] for m in modes), default=0.0)
    passed = bool(suite) and best >= min_entity_recall and all(summary[m]["citation_grounding_pct"] == 100.0 for m in modes)
    report = {"generated_at": utcnow_iso(), "questions": len(suite), "k": k, "seed": seed, "graph_hash": gi.graph.content_hash(),
              "summary": summary, "pass": passed,
              "criteria": {"min_entity_recall": min_entity_recall, "citation_grounding_pct": 100.0},
              "details": details}
    write_json(ws.p("eval_report.json"), report)
    return report

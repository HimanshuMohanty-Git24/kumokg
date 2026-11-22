"""3-mode local GraphRAG over ``graph.sqlite`` + ``graph.json``.

* ``local``: SQLite FTS5 BM25 over nodes, aliases, edges and evidence quotes + 1-2 hop neighbourhood.
* ``ppr``: seeds from alias/FTS matches, ontology-constrained Personalised PageRank over the edges active at
  ``as_of``; returns the best multi-hop subgraph + verbatim provenance within a token budget.
* ``lazy-global``: deterministic Louvain communities (cached in ``graph.sqlite``, zero LLM cost) ranked into
  community dossiers (hub entities + representative grounded claims) for query-time synthesis.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from typing import Any

import networkx as nx

from .ir import Edge, Graph, GraphStore, Node, Ontology, Workspace, evidence_text, normalize_name, normalize_text

MODES = ("local", "ppr", "lazy-global")


def personalized_pagerank(G: nx.Graph, personalization: dict[str, float], alpha: float = 0.85,
                          weight: str = "weight", max_iter: int = 200, tol: float = 1e-10) -> dict[str, float]:
    """``networkx.pagerank`` when its SciPy backend is installed, else an equivalent power iteration."""
    try:
        return nx.pagerank(G, alpha=alpha, personalization=personalization, weight=weight, max_iter=max_iter, tol=tol)
    except ImportError:
        pass
    nodes = sorted(G.nodes)
    n = len(nodes)
    if n == 0:
        return {}
    total = sum(personalization.get(x, 0.0) for x in nodes) or 1.0
    p = {x: personalization.get(x, 0.0) / total for x in nodes}
    out_w = {x: sum(d.get(weight, 1.0) for _, _, d in G.edges(x, data=True)) for x in nodes}
    x = dict(p) if any(p.values()) else {v: 1.0 / n for v in nodes}
    for _ in range(max_iter):
        prev = x
        x = {v: 0.0 for v in nodes}
        dangling = alpha * sum(prev[v] for v in nodes if out_w[v] == 0)
        for v in nodes:
            if out_w[v] == 0:
                continue
            share = alpha * prev[v] / out_w[v]
            for _, u, d in G.edges(v, data=True):
                x[u] += share * d.get(weight, 1.0)
        for v in nodes:
            x[v] += dangling * p[v] + (1.0 - alpha) * p[v]
        if sum(abs(x[v] - prev[v]) for v in nodes) < n * tol:
            break
    return x


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def active_edges(graph: Graph, as_of: str | None = None, include_superseded: bool = False) -> list[Edge]:
    if include_superseded:
        return [e for e in graph.edges if e.status in ("approved", "superseded")]
    return [e for e in graph.edges if e.active_at(as_of)]


def _evidence_ref(ev: Any, docs: dict[str, str]) -> str:
    name = docs.get(ev.doc_id, ev.doc_id)
    if ev.kind == "text" and ev.page_no:
        return f"{name} p.{ev.page_no}"
    if ev.kind == "table":
        return f"{name} {ev.sheet_or_table}[{ev.row_index}].{ev.column_name}"
    if ev.kind == "json":
        return f"{name} {ev.json_pointer}"
    if ev.kind == "notebook":
        return f"{name} cell {ev.cell_index}"
    if ev.kind == "image":
        return f"{name} image" + (f" p.{ev.page_no}" if ev.page_no else "")
    return name


class GraphIndex:
    """In-memory view of a workspace graph for retrieval and the Studio."""

    def __init__(self, ws: Workspace, graph: Graph | None = None, ontology: Ontology | None = None):
        self.ws = ws
        self.graph = graph or ws.load_graph()
        self.ont = ontology if ontology is not None else (ws.load_ontology(True) or Ontology())
        self.nodes: dict[str, Node] = self.graph.node_map()
        self.store = GraphStore(ws.sqlite_path)
        m = ws.load_manifest() if ws.exists() else None
        self.docs = {s.doc_id: s.name for s in (m.sources if m else [])}
        self._alias_index: list[tuple[str, str]] | None = None

    # seeds -------------------------------------------------------------------------------
    def alias_index(self) -> list[tuple[str, str]]:
        if self._alias_index is None:
            idx = []
            for n in self.graph.nodes:
                for name in {n.canonical_name, *n.aliases}:
                    key = normalize_name(name)
                    if len(key) >= 3:
                        idx.append((key, n.id))
            idx.sort(key=lambda kv: (-len(kv[0]), kv[0]))
            self._alias_index = idx
        return self._alias_index

    def seeds(self, question: str, limit: int = 8) -> dict[str, float]:
        q = f" {normalize_name(question)} "
        scores: dict[str, float] = defaultdict(float)
        taken: list[tuple[int, int]] = []
        for key, nid in self.alias_index():
            pos = q.find(f" {key} ")
            if pos == -1 or nid not in self.nodes:
                continue
            span = (pos, pos + len(key) + 2)
            if any(a < span[1] and span[0] < b for a, b in taken) and scores.get(nid, 0) == 0:
                continue
            taken.append(span)
            scores[nid] = max(scores[nid], 10.0 + len(key) / 10)
        for nid, s in self.store.search_nodes(question, 20):
            if nid in self.nodes:
                scores[nid] += min(5.0, s)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return dict(ranked)

    # formatting ----------------------------------------------------------------------------
    def node_info(self, nid: str, score: float | None = None) -> dict[str, Any]:
        n = self.nodes[nid]
        out = {"id": n.id, "name": n.canonical_name, "label": n.label}
        if score is not None:
            out["score"] = round(score, 5)
        return out

    def edge_info(self, e: Edge, score: float | None = None, quote_chars: int = 220) -> dict[str, Any]:
        ev = e.evidence[0] if e.evidence else None
        out = {"id": e.id, "source": e.source_id, "source_name": self.nodes[e.source_id].canonical_name if e.source_id in self.nodes else e.source_id,
               "type": e.type, "target": e.target_id,
               "target_name": self.nodes[e.target_id].canonical_name if e.target_id in self.nodes else e.target_id,
               "status": e.status, "valid_from": e.valid_from, "valid_to": e.valid_to, "confidence": e.confidence,
               "evidence_count": len(e.evidence)}
        if ev is not None:
            q = " ".join(evidence_text(ev).split())
            out["quote"] = q[:quote_chars] + ("…" if len(q) > quote_chars else "")
            out["citation"] = _evidence_ref(ev, self.docs)
            out["evidence_kind"] = ev.kind
        if score is not None:
            out["score"] = round(score, 5)
        return out

    def compact(self, result: dict[str, Any]) -> str:
        lines = [f"[{result['mode']}] Q: {result['question']}" + (f"  (as_of {result['as_of']})" if result.get("as_of") else "")]
        if result.get("nodes"):
            lines.append("Entities: " + "; ".join(f"{n['name']} ({n['label']})" for n in result["nodes"][:12]))
        for c in result.get("communities", []):
            lines.extend(self.dossier_lines(c))
        if result.get("edges"):
            lines.append("Facts:")
            for e in result["edges"]:
                when = f" [{e['valid_from'] or '…'} → {e['valid_to'] or 'now'}]" if (e.get("valid_from") or e.get("valid_to")) else ""
                lines.append(f"- {e['source_name']} -{e['type']}-> {e['target_name']}{when}  «{e.get('quote', '')}» [{e.get('citation', '')}]")
        if result.get("paths"):
            lines.append("Paths: " + " | ".join(" → ".join(self.nodes[n].canonical_name for n in p if n in self.nodes) for p in result["paths"][:4]))
        return "\n".join(lines)

    @staticmethod
    def dossier_lines(c: dict[str, Any]) -> list[str]:
        lines = [f"Community {c['community_id']} ({c['size']} entities): hubs " + ", ".join(h["name"] for h in c["hubs"])]
        for cl in c["claims"][:4]:
            lines.append(f"  - {cl['source_name']} -{cl['type']}-> {cl['target_name']}  «{cl.get('quote', '')}» [{cl.get('citation', '')}]")
        return lines

    # budget --------------------------------------------------------------------------------
    def _budget(self, edges: list[tuple[Edge, float]], max_tokens: int, reserved: int) -> list[dict[str, Any]]:
        out, used = [], reserved
        for e, s in edges:
            info = self.edge_info(e, s)
            cost = est_tokens(f"- {info['source_name']} -{info['type']}-> {info['target_name']} «{info.get('quote', '')}» [{info.get('citation', '')}]")
            if used + cost > max_tokens and out:
                break
            used += cost
            out.append(info)
        return out

    # modes -----------------------------------------------------------------------------------
    def local(self, question: str, hops: int = 2, as_of: str | None = None, max_tokens: int = 1500, top_k: int = 12) -> dict[str, Any]:
        edges = active_edges(self.graph, as_of)
        emap = {e.id: e for e in edges}
        adj: dict[str, list[Edge]] = defaultdict(list)
        for e in edges:
            adj[e.source_id].append(e)
            adj[e.target_id].append(e)
        seeds = self.seeds(question)
        edge_score: dict[str, float] = defaultdict(float)
        for eid, s in self.store.search_edges(question, 40):
            if eid in emap:
                edge_score[eid] += s
                seeds.setdefault(emap[eid].source_id, 0.0)
        for oid, s in self.store.search_evidence(question, 40):
            if oid in emap:
                edge_score[oid] += 0.5 * s
            elif oid in self.nodes:
                seeds[oid] = seeds.get(oid, 0.0) + 0.25 * s
        node_score: dict[str, float] = {k: v for k, v in seeds.items()}
        frontier = deque((nid, 0) for nid, _ in sorted(seeds.items(), key=lambda kv: -kv[1])[:5])
        seen = {nid for nid, _ in frontier}
        while frontier:
            nid, d = frontier.popleft()
            if d >= hops:
                continue
            for e in adj.get(nid, []):
                other = e.target_id if e.source_id == nid else e.source_id
                decay = 0.5 ** d
                edge_score[e.id] += decay * (1.0 + node_score.get(nid, 0.0) / 10)
                node_score[other] = max(node_score.get(other, 0.0), decay * 0.5 * (node_score.get(nid, 0.0) or 1.0))
                if other not in seen:
                    seen.add(other)
                    frontier.append((other, d + 1))
        ranked_edges = sorted(((emap[eid], s) for eid, s in edge_score.items() if eid in emap), key=lambda x: (-x[1], x[0].id))
        top_nodes = sorted(((nid, s) for nid, s in node_score.items() if nid in self.nodes), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        result = {"mode": "local", "question": question, "as_of": as_of,
                  "seeds": [self.node_info(n, s) for n, s in sorted(seeds.items(), key=lambda kv: -kv[1])[:8] if n in self.nodes],
                  "nodes": [self.node_info(n, s) for n, s in top_nodes], "paths": []}
        result["edges"] = self._budget(ranked_edges, max_tokens, est_tokens(self.compact({**result, "edges": []})))
        return result

    def constrained_graph(self, as_of: str | None) -> nx.Graph:
        G = nx.Graph()
        for nid in self.nodes:
            G.add_node(nid)
        for e in active_edges(self.graph, as_of):
            s, t = self.nodes.get(e.source_id), self.nodes.get(e.target_id)
            if s is None or t is None:
                continue
            if self.ont.relations and not (self.ont.allows(s.label, e.type, t.label) or not self.ont.closed_world):
                continue
            w = max(0.05, e.confidence) * (1.0 + 0.1 * min(5, len(e.evidence)))
            if G.has_edge(e.source_id, e.target_id):
                G[e.source_id][e.target_id]["weight"] += w
                G[e.source_id][e.target_id]["edges"].append(e.id)
            else:
                G.add_edge(e.source_id, e.target_id, weight=w, edges=[e.id])
        return G

    def ppr(self, question: str, hops: int = 2, as_of: str | None = None, max_tokens: int = 1500, top_k: int = 15,
            alpha: float = 0.85) -> dict[str, Any]:
        seeds = self.seeds(question)
        G = self.constrained_graph(as_of)
        seeds = {k: v for k, v in seeds.items() if k in G}
        result = {"mode": "ppr", "question": question, "as_of": as_of, "seeds": [], "nodes": [], "edges": [], "paths": []}
        if not seeds or G.number_of_edges() == 0:
            return self.local(question, hops, as_of, max_tokens) | {"mode": "ppr", "fallback": "local (no seeds or edges)"}
        total = sum(seeds.values())
        personalization = {n: seeds.get(n, 0.0) / total for n in G.nodes}
        pr = personalized_pagerank(G, personalization, alpha=alpha)
        ranked = sorted(pr.items(), key=lambda kv: (-kv[1], kv[0]))
        top = [n for n, s in ranked[:top_k] if s > 0]
        topset = set(top)
        emap = self.graph.edge_map()
        scored: list[tuple[Edge, float]] = []
        for u, v, data in G.subgraph(top).edges(data=True):
            for eid in data["edges"]:
                scored.append((emap[eid], pr[u] + pr[v]))
        paths = []
        seed_order = sorted(seeds, key=lambda k: -seeds[k])
        for target in [n for n in top if n not in seeds][:6]:
            best = None
            for s in seed_order[:3]:
                try:
                    p = nx.shortest_path(G.subgraph(topset | {s}), s, target)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                if len(p) - 1 <= max(hops, 3) and (best is None or len(p) < len(best)):
                    best = p
            if best:
                paths.append(best)
        on_path = set()
        for p in paths:
            for a, b in zip(p, p[1:]):
                on_path.update(G[a][b]["edges"])
        scored.sort(key=lambda x: (0 if x[0].id in on_path else 1, -x[1], x[0].id))
        result["seeds"] = [self.node_info(n, seeds[n]) for n in seed_order]
        result["nodes"] = [self.node_info(n, pr[n]) for n in top]
        result["paths"] = paths
        result["path_edges"] = sorted(on_path)
        result["edges"] = self._budget(scored, max_tokens, est_tokens(self.compact(result)))
        return result

    def communities(self, as_of: str | None = None) -> list[dict[str, Any]]:
        ghash = self.graph.content_hash() + (f"@{as_of}" if as_of else "")
        cached = self.store.load_communities(ghash) if self.ws.sqlite_path.exists() else None
        if cached is not None:
            return cached
        G = self.constrained_graph(as_of)
        G.remove_nodes_from([n for n in list(G.nodes) if G.degree(n) == 0])
        if G.number_of_nodes() == 0:
            return []
        parts = nx.algorithms.community.louvain_communities(G, weight="weight", seed=42, resolution=1.0)
        parts = sorted((sorted(p) for p in parts), key=lambda p: (-len(p), p[0]))
        emap = self.graph.edge_map()
        out = []
        for cid, members in enumerate(parts):
            sub = G.subgraph(members)
            hubs = sorted(members, key=lambda n: (-sub.degree(n, weight="weight"), n))[:5]
            claims: list[tuple[Edge, float]] = []
            for u, v, data in sub.edges(data=True):
                for eid in data["edges"]:
                    e = emap[eid]
                    claims.append((e, e.confidence + 0.1 * len(e.evidence)))
            claims.sort(key=lambda x: (-x[1], x[0].id))
            labels = Counter(self.nodes[m].label for m in members)
            out.append({"community_id": cid, "size": len(members), "members": members,
                        "hubs": [self.node_info(h) for h in hubs],
                        "labels": dict(labels.most_common()),
                        "title": " / ".join(self.nodes[h].canonical_name for h in hubs[:3]),
                        "claims": [self.edge_info(e) for e, _ in claims[:6]]})
        self.store.save_communities(ghash, out)
        return out

    def lazy_global(self, question: str, as_of: str | None = None, max_tokens: int = 1500, top_c: int = 3) -> dict[str, Any]:
        comms = self.communities(as_of)
        seeds = self.seeds(question, limit=20)
        qtokens = set(normalize_text(question).split())
        scored = []
        for c in comms:
            s = sum(seeds.get(m, 0.0) for m in c["members"])
            words = set(normalize_text(" ".join([c["title"]] + [cl.get("quote", "") for cl in c["claims"]])).split())
            s += 0.2 * len(qtokens & words)
            scored.append((s, c))
        scored.sort(key=lambda x: (-x[0], -x[1]["size"], x[1]["community_id"]))
        picked, used = [], 0
        for s, c in scored[:top_c]:
            dossier = {k: c[k] for k in ("community_id", "size", "hubs", "labels", "title", "claims")} | {"score": round(s, 4)}
            cost = est_tokens(" ".join(self.dossier_lines(dossier)))
            if used + cost > max_tokens and picked:
                break
            used += cost
            picked.append(dossier)
        return {"mode": "lazy-global", "question": question, "as_of": as_of, "communities": picked,
                "community_count": len(comms), "seeds": [self.node_info(n, v) for n, v in list(seeds.items())[:8]],
                "nodes": [], "edges": [], "paths": []}

    # views ----------------------------------------------------------------------------------
    def view(self, as_of: str | None = None, include_superseded: bool = False, include_communities: bool = True) -> dict[str, Any]:
        edges = active_edges(self.graph, as_of, include_superseded=include_superseded)
        active_ids = {e.id for e in active_edges(self.graph, as_of)}
        degree: Counter = Counter()
        for e in edges:
            if e.id in active_ids:
                degree[e.source_id] += 1
                degree[e.target_id] += 1
        comm_of: dict[str, int] = {}
        if include_communities:
            for c in self.communities(None):
                for m in c["members"]:
                    comm_of[m] = c["community_id"]
        breakpoints = sorted({t for e in self.graph.edges for t in (e.valid_from, e.valid_to) if t})
        node_mods: dict[str, set] = defaultdict(set)
        for n in self.graph.nodes:
            node_mods[n.id].update(x.kind for x in n.evidence)
        for e in edges:
            for x in e.evidence:
                node_mods[e.source_id].add(x.kind)
                node_mods[e.target_id].add(x.kind)
        return {
            "as_of": as_of, "graph_hash": self.graph.content_hash(),
            "nodes": [{"id": n.id, "name": n.canonical_name, "label": n.label, "aliases": n.aliases, "degree": degree[n.id],
                       "community": comm_of.get(n.id, -1), "modalities": sorted(node_mods[n.id]), "evidence_count": len(n.evidence),
                       "properties": n.properties} for n in self.graph.nodes],
            "edges": [{"id": e.id, "source": e.source_id, "target": e.target_id, "type": e.type, "status": e.status,
                       "active": e.id in active_ids,
                       "valid_from": e.valid_from, "valid_to": e.valid_to, "confidence": e.confidence,
                       "superseded_by": e.superseded_by, "modalities": sorted({x.kind for x in e.evidence}),
                       "evidence_count": len(e.evidence)} for e in edges],
            "labels": sorted({n.label for n in self.graph.nodes}),
            "modalities": sorted({m for s in node_mods.values() for m in s}),
            "temporal_breakpoints": breakpoints,
            "counts": {"nodes": len(self.graph.nodes), "edges": len(edges), "all_edges": len(self.graph.edges)},
        }

    def evidence_for(self, item_id: str, context_chars: int = 220) -> dict[str, Any]:
        """Full provenance for a node or edge, with chunk context for text-like evidence."""
        chunks = self.ws.load_chunks()
        item = self.nodes.get(item_id)
        kind = "node"
        if item is None:
            item = self.graph.edge_map().get(item_id)
            kind = "edge"
        if item is None:
            raise KeyError(item_id)
        out = []
        for ev in item.evidence:
            d = ev.model_dump(mode="json")
            d["source_name"] = self.docs.get(ev.doc_id, ev.doc_id)
            d["citation"] = _evidence_ref(ev, self.docs)
            if ev.kind == "text" and ev.chunk_id in chunks:
                text = chunks[ev.chunk_id].text
                from .worker_pool import find_span

                span = find_span(text, ev.verbatim_quote)
                if span:
                    a, b = span
                    d["context"] = {"before": text[max(0, a - context_chars):a], "match": text[a:b],
                                    "after": text[b:b + context_chars]}
            if ev.kind == "image":
                d["media_url"] = f"/media/{ev.image_path}"
            out.append(d)
        head = {"id": item_id, "kind": kind}
        if kind == "node":
            head.update(name=item.canonical_name, label=item.label, aliases=item.aliases, properties=item.properties)
        else:
            head.update(type=item.type, source=item.source_id, target=item.target_id, status=item.status,
                        valid_from=item.valid_from, valid_to=item.valid_to, superseded_by=item.superseded_by,
                        confidence=item.confidence, properties=item.properties,
                        source_name=self.nodes[item.source_id].canonical_name if item.source_id in self.nodes else item.source_id,
                        target_name=self.nodes[item.target_id].canonical_name if item.target_id in self.nodes else item.target_id)
        return head | {"evidence": out}


def query(ws: Workspace, question: str, mode: str = "local", hops: int = 2, as_of: str | None = None,
          max_tokens: int = 1500, fmt: str = "compact") -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    ws.require()
    t0 = time.perf_counter()
    gi = GraphIndex(ws)
    if mode == "local":
        res = gi.local(question, hops=hops, as_of=as_of, max_tokens=max_tokens)
    elif mode == "ppr":
        res = gi.ppr(question, hops=hops, as_of=as_of, max_tokens=max_tokens)
    else:
        res = gi.lazy_global(question, as_of=as_of, max_tokens=max_tokens)
    res["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    text = gi.compact(res)
    res["tokens_estimate"] = est_tokens(text)
    if fmt == "compact":
        return {"mode": res["mode"], "question": question, "as_of": as_of, "latency_ms": res["latency_ms"],
                "tokens_estimate": res["tokens_estimate"], "context": text,
                "node_ids": [n["id"] for n in res.get("nodes", [])], "edge_ids": [e["id"] for e in res.get("edges", [])]}
    return res

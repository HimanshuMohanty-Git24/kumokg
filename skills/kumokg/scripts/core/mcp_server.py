"""Zero-config local MCP server exposing a KumoKG workspace graph over stdio.

Tools: ``kg_query``, ``kg_ppr_multihop``, ``kg_get_evidence``, ``kg_time_travel``, ``kg_patch``, ``kg_doctor``,
``kg_export``. Works with the MCP Python SDK 2.x (``MCPServer``) and 1.x (``FastMCP``); when the ``[mcp]`` extra
is missing, ``serve`` raises :class:`McpUnavailable` with an install hint instead of a traceback.
"""
from __future__ import annotations

from typing import Any

from .ir import Workspace

INSTALL_HINT = ("The MCP server needs the optional [mcp] extra: "
                "uv run --project <skill_dir> --extra mcp python <skill_dir>/scripts/kumokg_cli.py serve-mcp --workspace <ws>")


class McpUnavailable(RuntimeError):
    pass


class KGTools:
    """Plain-Python implementations of every MCP tool (unit-testable without the SDK)."""

    def __init__(self, ws: Workspace):
        ws.require()
        self.ws = ws

    def kg_query(self, question: str, mode: str = "local", hops: int = 2, as_of: str | None = None,
                 max_tokens: int = 1500) -> dict[str, Any]:
        """Answer a question from the local graph (mode: local | ppr | lazy-global). Returns compact grounded context."""
        from .retrieval import query

        return query(self.ws, question, mode=mode, hops=hops, as_of=as_of, max_tokens=max_tokens, fmt="compact")

    def kg_ppr_multihop(self, question: str, hops: int = 3, as_of: str | None = None, max_tokens: int = 1500) -> dict[str, Any]:
        """Ontology-constrained Personalised PageRank multi-hop retrieval with explicit paths and verbatim provenance."""
        from .retrieval import query

        res = query(self.ws, question, mode="ppr", hops=hops, as_of=as_of, max_tokens=max_tokens, fmt="json")
        return {k: res.get(k) for k in ("mode", "question", "as_of", "seeds", "paths", "edges", "latency_ms", "tokens_estimate")}

    def kg_get_evidence(self, item_id: str) -> dict[str, Any]:
        """Full provenance (quotes, table cells, JSON pointers, notebook cells, image refs) for a node or edge id."""
        from .retrieval import GraphIndex

        return GraphIndex(self.ws).evidence_for(item_id)

    def kg_time_travel(self, as_of: str, entity: str | None = None, limit: int = 60) -> dict[str, Any]:
        """Facts that were valid at ``as_of`` (ISO date/time), optionally restricted to one entity name or id."""
        from .retrieval import GraphIndex, active_edges

        gi = GraphIndex(self.ws)
        edges = active_edges(gi.graph, as_of)
        if entity:
            ids = {entity} if entity in gi.nodes else set(gi.seeds(entity, limit=3))
            edges = [e for e in edges if e.source_id in ids or e.target_id in ids]
        return {"as_of": as_of, "entity": entity, "count": len(edges),
                "facts": [gi.edge_info(e) for e in sorted(edges, key=lambda e: e.id)[:limit]]}

    def kg_patch(self, patch: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Apply a verified bi-temporal patch: {"ops": [{"op": "add-edge"|"supersede-edge"|..., ...}], "message": "..."}."""
        from .patcher import apply_patch

        return apply_patch(self.ws, patch, dry_run=dry_run)

    def kg_doctor(self) -> dict[str, Any]:
        """Graph Health Scorecard: grounding %, schema compliance %, contradictions, orphans, coverage."""
        from .verifier import compact_doctor, doctor

        return compact_doctor(doctor(self.ws, deep=False))

    def kg_export(self, target: str, provenance_mode: str = "props") -> dict[str, Any]:
        """Export the graph to one of the 10 database targets and verify 100% parity."""
        from adapters.base import run_export

        res = run_export(self.ws, target, provenance_mode=provenance_mode, verify=True)
        return {"target": target, "out_dir": res["out_dir"], "parity": res["parity"], "files": [f["path"] for f in res["files"]]}


TOOL_NAMES = ("kg_query", "kg_ppr_multihop", "kg_get_evidence", "kg_time_travel", "kg_patch", "kg_doctor", "kg_export")


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer  # MCP Python SDK >= 2

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # MCP Python SDK 1.x

        return FastMCP
    except ImportError as exc:
        raise McpUnavailable(INSTALL_HINT) from exc


def build_server(ws: Workspace):
    cls = _server_class()
    tools = KGTools(ws)
    server = cls(name="kumokg", instructions=(
        "KumoKG knowledge graph for workspace " + str(ws.root) + ". Use kg_query for grounded answers, kg_ppr_multihop "
        "for multi-hop paths, kg_get_evidence to cite, kg_time_travel for historical states, kg_patch to correct facts "
        "(always with a verbatim quote) and kg_doctor/kg_export for health and database export."))
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        server.tool(name=name, description=(fn.__doc__ or "").strip())(fn)
    return server


def serve(ws: Workspace, transport: str = "stdio") -> None:
    build_server(ws).run(transport)

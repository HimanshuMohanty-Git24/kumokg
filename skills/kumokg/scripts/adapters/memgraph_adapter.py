"""Memgraph: plain-header CSVs, one-query-per-line ``schema.cypherl`` / ``load.cypherl`` (``LOAD CSV ... NULLIF``)
and a Bolt live push (Memgraph speaks Bolt, so the Neo4j driver is reused)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseAdapter, Parsed, Projection, columns_for, group_by, read_csv, require_driver, validate_cypher, write_csv
from .neo4j_adapter import ID_SPACE, bolt_push


def _conv(col: str, typ: str) -> str:
    fn = {"int": "toInteger", "float": "toFloat", "bool": "toBoolean"}.get(typ)
    return f"{fn}(row.`{col}`)" if fn else f"row.`{col}`"


class MemgraphAdapter(BaseAdapter):
    name = "memgraph"
    title = "Memgraph"
    extra = "memgraph"

    def write_files(self, proj: Projection, out: Path) -> None:
        schema = [f"CREATE INDEX ON :{ID_SPACE}(kumo_id);", f"CREATE CONSTRAINT ON (n:{ID_SPACE}) ASSERT n.kumo_id IS UNIQUE;"]
        load = []
        for label, nodes in group_by(proj.nodes, lambda n: n.label).items():
            cols = columns_for(nodes, ["kumo_id", "name"])
            fname = f"nodes_{label}.csv"
            write_csv(out / fname, [c for c, _ in cols], ([n.props.get(c) if c != "kumo_id" else n.id for c, _ in cols] for n in nodes))
            schema.append(f"CREATE INDEX ON :`{label}`(name);")
            props = ", ".join(f"`{c}`: {_conv(c, t)}" for c, t in cols)
            load.append(f'LOAD CSV FROM "/import/{fname}" WITH HEADER NULLIF "" AS row '
                        f"CREATE (n:{ID_SPACE}:`{label}` {{{props}}});")
        for etype, edges in group_by(proj.edges, lambda e: e.type).items():
            cols = columns_for(edges, ["kumo_id"])
            fname = f"edges_{etype}.csv"
            write_csv(out / fname, ["source", "target"] + [c for c, _ in cols],
                      ([e.source, e.target] + [e.props.get(c) for c, _ in cols] for e in edges))
            props = ", ".join(f"`{c}`: {_conv(c, t)}" for c, t in cols)
            load.append(f'LOAD CSV FROM "/import/{fname}" WITH HEADER NULLIF "" AS row '
                        f"MATCH (s:{ID_SPACE} {{kumo_id: row.source}}), (t:{ID_SPACE} {{kumo_id: row.target}}) "
                        f"CREATE (s)-[:`{etype}` {{{props}}}]->(t);")
        (out / "schema.cypherl").write_text("\n".join(schema) + "\n", encoding="utf-8", newline="\n")
        (out / "load.cypherl").write_text("\n".join(load) + "\n", encoding="utf-8", newline="\n")

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        return f"""# KumoKG -> Memgraph import

Graph hash `{proj.graph_hash}` · {c['nodes']} nodes · {c['edges']} relationships · provenance mode `{proj.provenance_mode}`.

1. Copy the CSVs into the container import directory (e.g. `docker cp . memgraph:/import`).
2. `mgconsole < schema.cypherl`
3. `mgconsole < load.cypherl`  (each line is one `LOAD CSV` query; empty cells load as null via `NULLIF ""`)

Live push: set `KUMOKG_MEMGRAPH_URI` (bolt://host:7687, optional user/password) and run
`uv run --extra memgraph ... export --target memgraph --push`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        ids, edges, syntax = [], [], {}
        for f in sorted(out.glob("nodes_*.csv")):
            header, rows = read_csv(f)
            syntax[f.name] = {"ok": header[:1] == ["kumo_id"], "detail": "header"}
            ids.extend(r["kumo_id"] for r in rows)
        for f in sorted(out.glob("edges_*.csv")):
            header, rows = read_csv(f)
            syntax[f.name] = {"ok": header[:3] == ["source", "target", "kumo_id"], "detail": "header"}
            edges.extend((r["kumo_id"], r["source"], r["target"]) for r in rows)
        for name in ("schema.cypherl", "load.cypherl"):
            text = (out / name).read_text(encoding="utf-8")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            ok, detail, n = validate_cypher(text)
            if ok and not all(ln.rstrip().endswith(";") for ln in lines):
                ok, detail = False, "every .cypherl line must be one complete query ending with ';'"
            if ok and n != len(lines):
                ok, detail = False, f"{n} statements on {len(lines)} lines"
            syntax[name] = {"ok": ok, "detail": detail, "statements": n}
        return Parsed(ids, edges, syntax)

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        own = client is None
        if own:
            neo4j = require_driver("neo4j", self.extra)
            (uri,) = self.require_env(env, "KUMOKG_MEMGRAPH_URI")
            auth = (env.get("KUMOKG_MEMGRAPH_USER", ""), env.get("KUMOKG_MEMGRAPH_PASSWORD", ""))
            client = neo4j.GraphDatabase.driver(uri, auth=auth)
        try:
            schema = [f"CREATE INDEX ON :{ID_SPACE}(kumo_id)"]
            return {"target": self.name, **bolt_push(proj, client, None, schema)}
        finally:
            if own:
                client.close()

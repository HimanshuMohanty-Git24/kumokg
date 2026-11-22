"""FalkorDB: ``falkordb-bulk-loader`` enforce-schema CSVs (typed headers), ``schema.cypher`` indexes,
``load_bulk.sh`` and a parameterised ``UNWIND ... MERGE`` live push through the ``falkordb`` client."""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from .base import (
    BaseAdapter, Parsed, Projection, columns_for, group_by, read_csv, require_driver, validate_balanced, validate_cypher,
    write_csv,
)

FTYPES = {"int": "LONG", "float": "DOUBLE", "bool": "BOOLEAN", "string": "STRING"}
ID_SPACE = "KumoNode"


class FalkorDBAdapter(BaseAdapter):
    name = "falkordb"
    title = "FalkorDB"
    extra = "falkordb"

    def write_files(self, proj: Projection, out: Path) -> None:
        schema, cmd = [], []
        for label, nodes in group_by(proj.nodes, lambda n: n.label).items():
            cols = columns_for(nodes, ["kumo_id", "name"])
            fname = f"nodes_{label}.csv"
            header = [f"kumo_id:ID({ID_SPACE})"] + [f"{c}:{FTYPES[t]}" for c, t in cols if c != "kumo_id"]
            write_csv(out / fname, header, ([n.id] + [n.props.get(c) for c, _ in cols if c != "kumo_id"] for n in nodes))
            schema.append(f"CREATE INDEX FOR (n:`{label}`) ON (n.kumo_id)")
            schema.append(f"CREATE INDEX FOR (n:`{label}`) ON (n.name)")
            cmd.append(f"  --nodes-with-label {shlex.quote(label)} {shlex.quote(fname)} \\")
        for etype, edges in group_by(proj.edges, lambda e: e.type).items():
            cols = columns_for(edges, ["kumo_id"])
            fname = f"edges_{etype}.csv"
            header = [f":START_ID({ID_SPACE})", f":END_ID({ID_SPACE})"] + [f"{c}:{FTYPES[t]}" for c, t in cols]
            write_csv(out / fname, header, ([e.source, e.target] + [e.props.get(c) for c, _ in cols] for e in edges))
            cmd.append(f"  --relations-with-type {shlex.quote(etype)} {shlex.quote(fname)} \\")
        (out / "schema.cypher").write_text(";\n".join(schema) + ";\n", encoding="utf-8", newline="\n")
        script = ["#!/usr/bin/env sh", "# Bulk-load with the official loader:  pip install falkordb-bulk-loader", "set -e",
                  'cd "$(dirname "$0")"', 'GRAPH="${FALKORDB_GRAPH:-kumokg}"', 'URL="${FALKORDB_URL:-redis://127.0.0.1:6379}"',
                  'falkordb-bulk-insert "$GRAPH" --server-url "$URL" --enforce-schema --id-type STRING \\']
        script += cmd
        script.append("  --skip-invalid-edges")
        script += ["# indexes (one query per line)", 'grep -v "^$" schema.cypher | sed "s/;$//" | while IFS= read -r q; do',
                   '  redis-cli -u "$URL" GRAPH.QUERY "$GRAPH" "$q" > /dev/null', "done", 'echo "loaded into $GRAPH"']
        (out / "load_bulk.sh").write_text("\n".join(script) + "\n", encoding="utf-8", newline="\n")

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        return f"""# KumoKG -> FalkorDB import

Graph hash `{proj.graph_hash}` · {c['nodes']} nodes · {c['edges']} edges · provenance mode `{proj.provenance_mode}`.

* Bulk: `pip install falkordb-bulk-loader && FALKORDB_URL=redis://localhost:6379 sh load_bulk.sh`
  (enforce-schema typed headers, string ids in the `{ID_SPACE}` id space, indexes from `schema.cypher`).
* Live push: set `KUMOKG_FALKORDB_HOST`, `KUMOKG_FALKORDB_PORT`, optional `KUMOKG_FALKORDB_PASSWORD`,
  `KUMOKG_FALKORDB_GRAPH`, then `uv run --extra falkordb ... export --target falkordb --push`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        ids, edges, syntax = [], [], {}
        for f in sorted(out.glob("nodes_*.csv")):
            header, rows = read_csv(f)
            ok = header[0] == f"kumo_id:ID({ID_SPACE})" and all(re.fullmatch(r"\w+:(STRING|LONG|DOUBLE|BOOLEAN)", h) for h in header[1:])
            syntax[f.name] = {"ok": ok, "detail": "enforce-schema header"}
            ids.extend(r[header[0]] for r in rows)
        for f in sorted(out.glob("edges_*.csv")):
            header, rows = read_csv(f)
            ok = header[:2] == [f":START_ID({ID_SPACE})", f":END_ID({ID_SPACE})"] and header[2] == "kumo_id:STRING"
            syntax[f.name] = {"ok": ok, "detail": "enforce-schema header"}
            edges.extend((r["kumo_id:STRING"], r[header[0]], r[header[1]]) for r in rows)
        ok, detail, n = validate_cypher((out / "schema.cypher").read_text(encoding="utf-8"))
        syntax["schema.cypher"] = {"ok": ok, "detail": detail, "statements": n}
        sh = (out / "load_bulk.sh").read_text(encoding="utf-8")
        ok, detail, _ = validate_balanced(sh, "shell")
        referenced = re.findall(r"--(?:nodes-with-label|relations-with-type) (\S+) (\S+)", sh)
        missing = [f for _, f in referenced if not (out / f).exists()]
        files = {p.name for p in out.glob("*_*.csv")}
        if ok and (missing or {f for _, f in referenced} != files):
            ok, detail = False, f"load_bulk.sh references mismatch: missing={missing}"
        syntax["load_bulk.sh"] = {"ok": ok, "detail": detail, "files": len(referenced)}
        return Parsed(ids, edges, syntax)

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        if client is None:
            falkordb = require_driver("falkordb", self.extra)
            host, port = self.require_env(env, "KUMOKG_FALKORDB_HOST", "KUMOKG_FALKORDB_PORT")
            db = falkordb.FalkorDB(host=host, port=int(port), password=env.get("KUMOKG_FALKORDB_PASSWORD") or None)
            client = db.select_graph(env.get("KUMOKG_FALKORDB_GRAPH", "kumokg"))
        stats = {"queries": 0}
        for label in proj.labels():
            client.query(f"CREATE INDEX FOR (n:`{label}`) ON (n.kumo_id)")
        for label, nodes in group_by(proj.nodes, lambda n: n.label).items():
            q = f"UNWIND $rows AS row MERGE (n:`{label}` {{kumo_id: row.kumo_id}}) SET n += row.props"
            for chunk in self.batches(nodes):
                client.query(q, {"rows": [{"kumo_id": n.id, "props": {k: v for k, v in n.props.items() if v is not None}} for n in chunk]})
                stats["queries"] += 1
        for (etype, sl, tl), edges in group_by(proj.edges, lambda e: (e.type, e.source_label, e.target_label)).items():
            q = (f"UNWIND $rows AS row MATCH (s:`{sl}` {{kumo_id: row.s}}), (t:`{tl}` {{kumo_id: row.t}}) "
                 f"MERGE (s)-[r:`{etype}` {{kumo_id: row.id}}]->(t) SET r += row.props")
            for chunk in self.batches(edges):
                client.query(q, {"rows": [{"id": e.kumo_id, "s": e.source, "t": e.target,
                                           "props": {k: v for k, v in e.props.items() if v is not None}} for e in chunk]})
                stats["queries"] += 1
        n = client.query("MATCH (n) WHERE n.kumo_id IS NOT NULL RETURN count(n)").result_set[0][0]
        e = client.query("MATCH ()-[r]->() WHERE r.kumo_id IS NOT NULL RETURN count(r)").result_set[0][0]
        return {"target": self.name, **stats, "nodes_in_db": n, "edges_in_db": e}

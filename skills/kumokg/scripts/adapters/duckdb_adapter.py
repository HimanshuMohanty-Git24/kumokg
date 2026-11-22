"""DuckDB: ``nodes.parquet`` / ``edges.parquet`` / ``evidence.parquet``, ``setup_duckpgq.sql`` (DuckPGQ
``CREATE PROPERTY GRAPH``), ``graph.graphml`` (Gephi / NetworkX) and a ready-to-query ``kumokg.duckdb`` database.
Live push ATTACHes any DuckDB/MotherDuck target (``KUMOKG_DUCKDB_PATH``) and rebuilds the tables there."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import BaseAdapter, Parsed, Projection, relational_rows, split_statements, validate_sql

NODE_SCHEMA = [("id", "VARCHAR"), ("label", "VARCHAR"), ("labels", "VARCHAR[]"), ("name", "VARCHAR"), ("aliases", "JSON"),
               ("properties", "JSON"), ("confidence", "DOUBLE"), ("created_at", "TIMESTAMPTZ")]
EDGE_SCHEMA = [("id", "VARCHAR"), ("source_id", "VARCHAR"), ("target_id", "VARCHAR"), ("type", "VARCHAR"), ("directed", "BOOLEAN"),
               ("properties", "JSON"), ("confidence", "DOUBLE"), ("valid_from", "VARCHAR"), ("valid_to", "VARCHAR"),
               ("ingested_at", "TIMESTAMPTZ"), ("superseded_by", "VARCHAR"), ("status", "VARCHAR")]
EVIDENCE_SCHEMA = [("id", "VARCHAR"), ("owner_kind", "VARCHAR"), ("owner_id", "VARCHAR"), ("kind", "VARCHAR"), ("doc_id", "VARCHAR"),
                   ("quote", "VARCHAR"), ("locator", "VARCHAR"), ("payload", "JSON")]


def _fill(con, table: str, schema: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
    con.execute(f"CREATE OR REPLACE TABLE {table} ({', '.join(f'{c} {t}' for c, t in schema)})")
    if rows:
        cols = [c for c, _ in schema]
        con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in cols)})", [[r.get(c) for c in cols] for r in rows])


class DuckDBAdapter(BaseAdapter):
    name = "duckdb"
    title = "DuckDB"
    extra = ""

    def expected_counts(self, proj: Projection) -> dict[str, int]:
        c = proj.counts()
        return {"nodes": c["domain_nodes"], "edges": c["domain_edges"]}

    def _schemas(self, mode: str):
        ev = [("evidence", "JSON")] if mode == "props" else []
        return NODE_SCHEMA + ev, EDGE_SCHEMA + ev, EVIDENCE_SCHEMA

    def write_files(self, proj: Projection, out: Path) -> None:
        import duckdb
        import networkx as nx

        rows = relational_rows(self.ws.load_graph(), proj.provenance_mode)
        ns, es, vs = self._schemas(proj.provenance_mode)
        db_path = out / "kumokg.duckdb"
        con = duckdb.connect(str(db_path))
        try:
            _fill(con, "nodes", ns, rows["nodes"])
            _fill(con, "edges", es, rows["edges"])
            _fill(con, "evidence", vs, rows["evidence"])
            con.execute("CREATE OR REPLACE VIEW active_edges AS SELECT * FROM edges WHERE status = 'approved' AND valid_to IS NULL")
            for t in ("nodes", "edges", "evidence"):
                con.execute(f"COPY (SELECT * FROM {t} ORDER BY id) TO '{(out / (t + '.parquet')).as_posix()}' (FORMAT parquet)")
            con.execute("CHECKPOINT")
        finally:
            con.close()
        sql = f"""-- KumoKG DuckPGQ setup. Run from this directory:  duckdb kumokg_pgq.duckdb < setup_duckpgq.sql
INSTALL duckpgq FROM community;
LOAD duckpgq;
CREATE OR REPLACE TABLE nodes AS SELECT * FROM read_parquet('nodes.parquet');
CREATE OR REPLACE TABLE edges AS SELECT * FROM read_parquet('edges.parquet');
CREATE OR REPLACE TABLE evidence AS SELECT * FROM read_parquet('evidence.parquet');
CREATE OR REPLACE VIEW active_edges AS SELECT * FROM edges WHERE status = 'approved' AND valid_to IS NULL;
CREATE PROPERTY GRAPH kumokg
  VERTEX TABLES (nodes LABEL entity)
  EDGE TABLES (edges SOURCE KEY (source_id) REFERENCES nodes (id) DESTINATION KEY (target_id) REFERENCES nodes (id) LABEL relation);
-- Example 2-hop query:
-- FROM GRAPH_TABLE (kumokg MATCH (a:entity)-[r:relation]->(b:entity)-[s:relation]->(c:entity)
--   WHERE a.name = 'Alice' COLUMNS (a.name AS start, r.type AS r1, b.name AS via, s.type AS r2, c.name AS reached)) LIMIT 20;
"""
        (out / "setup_duckpgq.sql").write_text(sql, encoding="utf-8", newline="\n")
        g = nx.MultiDiGraph()
        for r in rows["nodes"]:
            g.add_node(r["id"], label=r["label"], name=r["name"], confidence=r["confidence"])
        for r in rows["edges"]:
            attrs = {k: r[k] for k in ("type", "status", "confidence", "valid_from", "valid_to") if r[k] is not None}
            g.add_edge(r["source_id"], r["target_id"], key=r["id"], **attrs)
        nx.write_graphml(g, out / "graph.graphml", encoding="utf-8")

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        return f"""# KumoKG -> DuckDB

Graph hash `{proj.graph_hash}` · {c['domain_nodes']} nodes · {c['domain_edges']} edges · {len(proj.evidence_rows)} evidence rows.

* `kumokg.duckdb` — ready database: `duckdb kumokg.duckdb "SELECT type, count(*) FROM active_edges GROUP BY 1"`
* `nodes.parquet`, `edges.parquet`, `evidence.parquet` — portable columnar exports (any engine: Polars, Spark, BigQuery).
* `setup_duckpgq.sql` — SQL/PGQ property graph via the DuckPGQ community extension (`GRAPH_TABLE ... MATCH`).
* `graph.graphml` — open in Gephi or `networkx.read_graphml`.

Live push: `KUMOKG_DUCKDB_PATH=/path/target.duckdb` (or `md:kumokg` with `MOTHERDUCK_TOKEN`) + `export --target duckdb --push`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        import duckdb
        import networkx as nx

        syntax: dict[str, dict[str, Any]] = {}
        con = duckdb.connect()
        try:
            q = lambda s: con.execute(s).fetchall()  # noqa: E731
            p = {t: (out / f"{t}.parquet").as_posix() for t in ("nodes", "edges", "evidence")}
            node_ids = [r[0] for r in q(f"SELECT id FROM read_parquet('{p['nodes']}') ORDER BY id")]
            edges = [tuple(r) for r in q(f"SELECT id, source_id, target_id FROM read_parquet('{p['edges']}') ORDER BY id")]
            n_ev = q(f"SELECT count(*) FROM read_parquet('{p['evidence']}')")[0][0]
            orphan_ev = q(f"SELECT count(*) FROM read_parquet('{p['evidence']}') v WHERE v.owner_id NOT IN "
                          f"(SELECT id FROM read_parquet('{p['nodes']}') UNION ALL SELECT id FROM read_parquet('{p['edges']}'))")[0][0]
            syntax["nodes.parquet"] = {"ok": True, "detail": f"{len(node_ids)} rows"}
            syntax["edges.parquet"] = {"ok": True, "detail": f"{len(edges)} rows"}
            syntax["evidence.parquet"] = {"ok": orphan_ev == 0, "detail": f"{n_ev} rows, {orphan_ev} orphan"}
        finally:
            con.close()
        db = duckdb.connect(str(out / "kumokg.duckdb"), read_only=True)
        try:
            n_db = db.execute("SELECT count(*) FROM nodes").fetchone()[0]
            e_db = db.execute("SELECT count(*) FROM edges").fetchone()[0]
            act = db.execute("SELECT count(*) FROM active_edges").fetchone()[0]
            syntax["kumokg.duckdb"] = {"ok": n_db == len(node_ids) and e_db == len(edges), "detail": f"{n_db} nodes, {e_db} edges, {act} active"}
        finally:
            db.close()
        sql = (out / "setup_duckpgq.sql").read_text(encoding="utf-8")
        ok, detail, _ = validate_sql(sql)
        if ok:
            con = duckdb.connect()
            try:
                con.execute(f"SET file_search_path = '{out.as_posix()}'")
                created: dict[str, list[str]] = {}
                for st in split_statements(sql, "sql"):
                    head = st.split(None, 1)[0].upper()
                    if head in ("INSTALL", "LOAD") or st.upper().startswith("CREATE PROPERTY GRAPH"):
                        continue
                    con.execute(st)
                for t in ("nodes", "edges"):
                    created[t] = [r[0] for r in con.execute(f"DESCRIBE {t}").fetchall()]
                pg = next(s for s in split_statements(sql, "sql") if s.upper().startswith("CREATE PROPERTY GRAPH"))
                m = re.search(r"VERTEX TABLES \((\w+) LABEL \w+\)\s+EDGE TABLES \((\w+) SOURCE KEY \((\w+)\) REFERENCES (\w+) \((\w+)\) "
                              r"DESTINATION KEY \((\w+)\) REFERENCES (\w+) \((\w+)\) LABEL \w+\)", pg)
                if not m:
                    ok, detail = False, "CREATE PROPERTY GRAPH not recognised"
                else:
                    vt, et, sk, sref, sid, dk, dref, did = m.groups()
                    if vt not in created or et not in created or sk not in created[et] or dk not in created[et] \
                            or sid not in created[sref] or did not in created[dref]:
                        ok, detail = False, "property graph references unknown tables/columns"
                    else:
                        detail = "tables created; property graph keys resolve"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            finally:
                con.close()
        syntax["setup_duckpgq.sql"] = {"ok": ok, "detail": detail}
        try:
            gml = nx.read_graphml(out / "graph.graphml")
            ok = gml.number_of_nodes() == len(node_ids) and gml.number_of_edges() == len(edges)
            syntax["graph.graphml"] = {"ok": ok, "detail": f"{gml.number_of_nodes()} nodes, {gml.number_of_edges()} edges"}
        except Exception as exc:
            syntax["graph.graphml"] = {"ok": False, "detail": str(exc)}
        return Parsed(node_ids, edges, syntax, {"evidence_rows": n_ev})

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        import duckdb

        out = self.out_dir
        if not (out / "nodes.parquet").exists():
            self.export_bundle(provenance_mode=proj.provenance_mode)
        own = client is None
        if own:
            (path,) = self.require_env(env, "KUMOKG_DUCKDB_PATH")
            client = duckdb.connect(path)
        try:
            for t in ("nodes", "edges", "evidence"):
                client.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM read_parquet('{(out / (t + '.parquet')).as_posix()}')")
            client.execute("CREATE OR REPLACE VIEW active_edges AS SELECT * FROM edges WHERE status = 'approved' AND valid_to IS NULL")
            n = client.execute("SELECT count(*) FROM nodes").fetchone()[0]
            e = client.execute("SELECT count(*) FROM edges").fetchone()[0]
        finally:
            if own:
                client.close()
        return {"target": self.name, "nodes_in_db": n, "edges_in_db": e}

"""PostgreSQL (``--pg-mode plain|age|pgvector``): ``schema.sql`` (``kg_nodes``, ``kg_edges``, ``kg_evidence`` with
bi-temporal columns and GIN full-text indexes; Apache AGE graph DDL in ``age`` mode; a pgvector embedding column stub
in ``pgvector`` mode), ``data/*.csv``, ``load.sql`` (psql ``\\copy``), ``age_load.sql`` and a psycopg COPY push."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import (
    AdapterError, BaseAdapter, Parsed, Projection, read_csv, relational_rows, require_driver, split_statements,
    validate_sql, write_csv,
)

NODE_COLS = ["id", "label", "labels", "name", "aliases", "properties", "confidence", "created_at"]
EDGE_COLS = ["id", "source_id", "target_id", "type", "directed", "properties", "confidence", "valid_from", "valid_to",
             "ingested_at", "superseded_by", "status"]
EVIDENCE_COLS = ["id", "owner_kind", "owner_id", "kind", "doc_id", "quote", "locator", "payload"]


def pg_array(items: list[str]) -> str:
    return "{" + ",".join('"' + i.replace("\\", "\\\\").replace('"', '\\"') + '"' for i in items) + "}"


def age_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r") + "'"


def age_map(props: dict[str, Any]) -> str:
    return "{" + ", ".join(f"{k}: {age_str(v)}" for k, v in props.items() if v is not None) + "}"


class PostgresAdapter(BaseAdapter):
    name = "postgres"
    title = "PostgreSQL"
    extra = "postgres"

    @property
    def mode(self) -> str:
        m = self.options.get("pg_mode", "plain")
        if m not in ("plain", "age", "pgvector"):
            raise AdapterError("pg_mode must be plain, age or pgvector")
        return m

    @property
    def graph_name(self) -> str:
        return self.options.get("graph_name", "kumokg")

    def expected_counts(self, proj: Projection) -> dict[str, int]:
        c = proj.counts()
        return {"nodes": c["domain_nodes"], "edges": c["domain_edges"]}

    def _schema_sql(self, proj: Projection, with_evidence_col: bool) -> str:
        ev_col = ",\n  evidence JSONB NOT NULL DEFAULT '[]'" if with_evidence_col else ""
        parts = [f"""-- KumoKG relational schema (mode: {self.mode}); valid_from / valid_to are ISO-8601 text so dates and datetimes
-- round-trip exactly. valid_to IS NULL = currently valid.
CREATE TABLE IF NOT EXISTS kg_nodes (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  labels TEXT[] NOT NULL,
  name TEXT NOT NULL,
  aliases JSONB NOT NULL DEFAULT '[]',
  properties JSONB NOT NULL DEFAULT '{{}}',
  confidence DOUBLE PRECISION,
  created_at TIMESTAMPTZ{ev_col},
  search tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(aliases::text, ''))) STORED
);
CREATE TABLE IF NOT EXISTS kg_edges (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES kg_nodes(id),
  target_id TEXT NOT NULL REFERENCES kg_nodes(id),
  type TEXT NOT NULL,
  directed BOOLEAN NOT NULL DEFAULT TRUE,
  properties JSONB NOT NULL DEFAULT '{{}}',
  confidence DOUBLE PRECISION,
  valid_from TEXT,
  valid_to TEXT,
  ingested_at TIMESTAMPTZ,
  superseded_by TEXT,
  status TEXT NOT NULL CHECK (status IN ('approved', 'pending', 'quarantined', 'superseded')){ev_col}
);
CREATE TABLE IF NOT EXISTS kg_evidence (
  id TEXT PRIMARY KEY,
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('node', 'edge')),
  owner_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  doc_id TEXT,
  quote TEXT,
  locator TEXT,
  payload JSONB NOT NULL,
  search tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(quote, ''))) STORED
);
CREATE INDEX IF NOT EXISTS kg_nodes_search_gin ON kg_nodes USING GIN (search);
CREATE INDEX IF NOT EXISTS kg_nodes_properties_gin ON kg_nodes USING GIN (properties);
CREATE INDEX IF NOT EXISTS kg_nodes_label ON kg_nodes (label);
CREATE INDEX IF NOT EXISTS kg_edges_source ON kg_edges (source_id);
CREATE INDEX IF NOT EXISTS kg_edges_target ON kg_edges (target_id);
CREATE INDEX IF NOT EXISTS kg_edges_type ON kg_edges (type);
CREATE INDEX IF NOT EXISTS kg_edges_validity ON kg_edges (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS kg_evidence_owner ON kg_evidence (owner_id);
CREATE INDEX IF NOT EXISTS kg_evidence_search_gin ON kg_evidence USING GIN (search);
CREATE OR REPLACE VIEW kg_active_edges AS SELECT * FROM kg_edges WHERE status = 'approved' AND valid_to IS NULL;
"""]
        if proj.provenance_mode == "reified":
            parts.append("CREATE OR REPLACE VIEW kg_statements AS SELECT e.id AS statement_id, e.source_id, e.type, e.target_id, "
                         "e.valid_from, e.valid_to, e.status, v.id AS evidence_id, v.quote FROM kg_edges e "
                         "LEFT JOIN kg_evidence v ON v.owner_kind = 'edge' AND v.owner_id = e.id;\n")
        if self.mode == "pgvector":
            dim = int(self.options.get("vector_dim", 384))
            parts.append(f"""-- pgvector stub: populate kg_nodes.embedding with your embedding model, then use <=> for similarity search.
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS embedding vector({dim});
CREATE INDEX IF NOT EXISTS kg_nodes_embedding_hnsw ON kg_nodes USING hnsw (embedding vector_cosine_ops);
""")
        if self.mode == "age":
            g = self.graph_name
            lines = ["-- Apache AGE property graph DDL (data is loaded by age_load.sql)", "CREATE EXTENSION IF NOT EXISTS age;", "LOAD 'age';",
                     'SET search_path = ag_catalog, "$user", public;',
                     f"DO $kumo$ BEGIN PERFORM ag_catalog.create_graph('{g}'); EXCEPTION WHEN others THEN NULL; END $kumo$;"]
            for label in sorted({n.label for n in proj.nodes if not n.system}):
                lines.append(f"DO $kumo$ BEGIN PERFORM ag_catalog.create_vlabel('{g}', '{label}'); EXCEPTION WHEN others THEN NULL; END $kumo$;")
            for etype in sorted({e.type for e in proj.edges if not e.system}):
                lines.append(f"DO $kumo$ BEGIN PERFORM ag_catalog.create_elabel('{g}', '{etype}'); EXCEPTION WHEN others THEN NULL; END $kumo$;")
            lines.append('SET search_path = "$user", public;')
            parts.append("\n".join(lines) + "\n")
        return "\n".join(parts)

    def write_files(self, proj: Projection, out: Path) -> None:
        rows = relational_rows(self.ws.load_graph(), proj.provenance_mode)
        with_ev = proj.provenance_mode == "props"
        ncols = NODE_COLS + (["evidence"] if with_ev else [])
        ecols = EDGE_COLS + (["evidence"] if with_ev else [])
        (out / "data").mkdir()
        write_csv(out / "data" / "nodes.csv", ncols, ([pg_array(r[c]) if c == "labels" else r[c] for c in ncols] for r in rows["nodes"]))
        write_csv(out / "data" / "edges.csv", ecols, ([r[c] for c in ecols] for r in rows["edges"]))
        write_csv(out / "data" / "evidence.csv", EVIDENCE_COLS, ([r[c] for c in EVIDENCE_COLS] for r in rows["evidence"]))
        (out / "schema.sql").write_text(self._schema_sql(proj, with_ev), encoding="utf-8", newline="\n")
        load = ["\\set ON_ERROR_STOP on", "BEGIN;", "TRUNCATE kg_evidence, kg_edges, kg_nodes;",
                f"\\copy kg_nodes ({', '.join(ncols)}) FROM 'data/nodes.csv' WITH (FORMAT csv, HEADER true)",
                f"\\copy kg_edges ({', '.join(ecols)}) FROM 'data/edges.csv' WITH (FORMAT csv, HEADER true)",
                f"\\copy kg_evidence ({', '.join(EVIDENCE_COLS)}) FROM 'data/evidence.csv' WITH (FORMAT csv, HEADER true)",
                "COMMIT;", "ANALYZE kg_nodes;", "ANALYZE kg_edges;", "ANALYZE kg_evidence;"]
        (out / "load.sql").write_text("\n".join(load) + "\n", encoding="utf-8", newline="\n")
        if self.mode == "age":
            g = self.graph_name
            label_of = {n.id: n.label for n in proj.nodes}
            lines = ["LOAD 'age';", 'SET search_path = ag_catalog, "$user", public;']
            for n in proj.nodes:
                if n.system:
                    continue
                props = {k: v for k, v in n.props.items() if k in ("kumo_id", "name", "confidence", "labels") or not k.startswith("evidence")}
                lines.append(f"SELECT * FROM cypher('{g}', $kq$ CREATE (:{n.label} {age_map(props)}) $kq$) AS (v agtype);")
            for e in proj.edges:
                if e.system:
                    continue
                props = {k: v for k, v in e.props.items() if not k.startswith("evidence")}
                lines.append(f"SELECT * FROM cypher('{g}', $kq$ MATCH (a:{label_of[e.source]} {{kumo_id: {age_str(e.source)}}}), "
                             f"(b:{label_of[e.target]} {{kumo_id: {age_str(e.target)}}}) CREATE (a)-[:{e.type} {age_map(props)}]->(b) $kq$) AS (e agtype);")
            lines.append('SET search_path = "$user", public;')
            (out / "age_load.sql").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        age = "\n4. `psql -d kumokg -f age_load.sql`  (Apache AGE: openCypher graph `" + self.graph_name + "`)" if self.mode == "age" else ""
        return f"""# KumoKG -> PostgreSQL ({self.mode})

Graph hash `{proj.graph_hash}` · {c['domain_nodes']} nodes · {c['domain_edges']} edges · {len(proj.evidence_rows)} evidence rows.

1. `createdb kumokg`
2. `psql -d kumokg -f schema.sql`
3. `psql -d kumokg -f load.sql`   (client-side `\\copy` from `data/*.csv`, one transaction){age}

Query examples: `SELECT * FROM kg_active_edges WHERE type = 'RULES';`
`SELECT owner_id, quote FROM kg_evidence WHERE search @@ plainto_tsquery('english', 'queen');`
Time travel: `SELECT * FROM kg_edges WHERE (valid_from IS NULL OR valid_from <= '2020-01-01') AND (valid_to IS NULL OR valid_to > '2020-01-01');`

Live push: set `KUMOKG_POSTGRES_DSN` and run `uv run --extra postgres ... export --target postgres --pg-mode {self.mode} --push`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        syntax: dict[str, dict[str, Any]] = {}
        schema = (out / "schema.sql").read_text(encoding="utf-8")
        ok, detail, n = validate_sql(schema)
        tables: dict[str, list[str]] = {}
        if ok:
            for st in split_statements(schema, "sql"):
                m = re.match(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)$", st.strip(), re.S)
                if m:
                    cols = []
                    depth, cur = 0, ""
                    for ch in m.group(2):
                        depth += ch == "("
                        depth -= ch == ")"
                        if ch == "," and depth == 0:
                            cols.append(cur.strip())
                            cur = ""
                        else:
                            cur += ch
                    cols.append(cur.strip())
                    tables[m.group(1)] = [c.split()[0] for c in cols if c]
        syntax["schema.sql"] = {"ok": ok and {"kg_nodes", "kg_edges", "kg_evidence"} <= set(tables), "detail": detail, "statements": n}
        load = (out / "load.sql").read_text(encoding="utf-8")
        sql_lines, ok, detail = [], True, "ok"
        for line in load.splitlines():
            if line.startswith("\\set"):
                continue
            if line.startswith("\\copy"):
                m = re.match(r"\\copy (\w+) \(([^)]*)\) FROM '([^']+)' WITH \(FORMAT csv, HEADER true\)$", line)
                if not m:
                    ok, detail = False, f"bad \\copy: {line[:80]}"
                    break
                table, cols, fname = m.group(1), [c.strip() for c in m.group(2).split(",")], m.group(3)
                header, _ = read_csv(out / fname) if (out / fname).exists() else ([], [])
                if table not in tables or not set(cols) <= set(tables[table]) or header != cols:
                    ok, detail = False, f"\\copy {table} columns do not match schema/CSV header"
                    break
                continue
            sql_lines.append(line)
        if ok:
            ok, detail, _ = validate_sql("\n".join(sql_lines))
        syntax["load.sql"] = {"ok": ok, "detail": detail}
        _, nodes = read_csv(out / "data" / "nodes.csv")
        _, edges = read_csv(out / "data" / "edges.csv")
        _, evid = read_csv(out / "data" / "evidence.csv")
        owners = {r["id"] for r in nodes} | {r["id"] for r in edges}
        orphan_ev = [r["id"] for r in evid if r["owner_id"] not in owners]
        syntax["data/evidence.csv"] = {"ok": not orphan_ev, "detail": f"{len(evid)} rows, {len(orphan_ev)} without owner"}
        syntax["data/nodes.csv"] = {"ok": all(re.fullmatch(r"\{.*\}", r["labels"]) for r in nodes), "detail": "TEXT[] literals"}
        if self.mode == "age":
            age = (out / "age_load.sql").read_text(encoding="utf-8")
            ok, detail, _ = validate_sql(age)
            v = len(re.findall(r"\$kq\$ CREATE \(:", age))
            e = len(re.findall(r"\$kq\$ MATCH ", age))
            if ok and (v != len(nodes) or e != len(edges)):
                ok, detail = False, f"age_load.sql creates {v} vertices / {e} edges, CSV has {len(nodes)} / {len(edges)}"
            syntax["age_load.sql"] = {"ok": ok, "detail": detail}
        return Parsed([r["id"] for r in nodes], [(r["id"], r["source_id"], r["target_id"]) for r in edges], syntax,
                      {"evidence_rows": len(evid)})

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        out = self.out_dir
        if not (out / "schema.sql").exists():
            self.export_bundle(provenance_mode=proj.provenance_mode)
        own = client is None
        if own:
            psycopg = require_driver("psycopg", self.extra)
            (dsn,) = self.require_env(env, "KUMOKG_POSTGRES_DSN")
            client = psycopg.connect(dsn)
        try:
            with client.cursor() as cur:
                cur.execute((out / "schema.sql").read_text(encoding="utf-8"))
                cur.execute("TRUNCATE kg_evidence, kg_edges, kg_nodes")
                for table, fname in (("kg_nodes", "nodes.csv"), ("kg_edges", "edges.csv"), ("kg_evidence", "evidence.csv")):
                    header, _ = read_csv(out / "data" / fname)
                    with cur.copy(f"COPY {table} ({', '.join(header)}) FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
                        cp.write((out / "data" / fname).read_bytes())
                if self.mode == "age" and (out / "age_load.sql").exists():
                    cur.execute((out / "age_load.sql").read_text(encoding="utf-8"))
                cur.execute("SELECT count(*) FROM kg_nodes")
                n = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM kg_edges")
                e = cur.fetchone()[0]
            client.commit()
        finally:
            if own:
                client.close()
        return {"target": self.name, "mode": self.mode, "nodes_in_db": n, "edges_in_db": e}

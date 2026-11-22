"""Milestone 5: manifest-driven top-10 database adapters, offline bundles, live push logic and parity verification.

Live pushes are exercised against in-process doubles that enforce the semantics the real servers would (edges may only
reference already-pushed vertices, idempotent upserts, native count queries); DuckDB and the SPARQL Graph Store push run
against real engines (duckdb, rdflib). The optional docker-compose stack is covered by the ``live`` marked test.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from rdflib import Dataset, Graph as RDFGraph, URIRef
from rdflib.namespace import OWL, RDF
from typer.testing import CliRunner

from adapters import base
from adapters.base import AdapterError, DriverMissing, Sanitizer, get_adapter, load_registry, run_export
from core import patcher, verifier, worker_pool as wp
from core.ir import sha256_file

TARGETS = ["neo4j", "postgres", "memgraph", "falkordb", "neptune", "arangodb", "tigergraph", "janusgraph", "rdf", "duckdb"]
REQUIRED = {
    "neo4j": ["schema.cypher", "import.cypher", "nodes_Company.csv", "edges_WORKS_AT.csv"],
    "postgres": ["schema.sql", "load.sql", "data/nodes.csv", "data/edges.csv", "data/evidence.csv"],
    "memgraph": ["schema.cypherl", "load.cypherl", "nodes_Company.csv", "edges_WORKS_AT.csv"],
    "falkordb": ["schema.cypher", "load_bulk.sh", "nodes_Company.csv", "edges_WORKS_AT.csv"],
    "neptune": ["loader-request.json", "vertices_Company.csv", "edges_WORKS_AT.csv"],
    "arangodb": ["graph_definition.json", "import.sh", "vertices/Company.jsonl", "edges/WORKS_AT.jsonl"],
    "tigergraph": ["schema.gsql", "loading_job.gsql", "vertices_Company.csv"],
    "janusgraph": ["schema.groovy", "graph.graphson", "load.groovy"],
    "rdf": ["ontology.ttl", "shapes.shacl.ttl", "data.ttl", "data.nq", "data.jsonld"],
    "duckdb": ["nodes.parquet", "edges.parquet", "evidence.parquet", "setup_duckpgq.sql", "graph.graphml", "kumokg.duckdb"],
}


@pytest.fixture(scope="module")
def graph_ws(tmp_path_factory, synthetic_ws_template):
    import shutil
    from core.ir import Workspace

    dest = tmp_path_factory.mktemp("m5") / "ws"
    shutil.copytree(synthetic_ws_template, dest)
    ws = Workspace(dest)
    wp.extract_heuristic(ws)
    verifier.verify(ws)
    patcher.auto_decide(ws)
    patcher.commit(ws)
    g = ws.load_graph()
    old = next(e for e in g.edges if e.type == "WORKS_AT" and e.source_id == "person:elena-voss")
    res = patcher.apply_patch(ws, {"ops": [{"op": "supersede-edge", "edge_id": old.id, "valid_from": "2023-06-01",
                                            "new": {"target": "company:globex-dynamics"},
                                            "quote": "Elena Voss joined Globex Dynamics"}]})
    assert res["ok"]
    return ws


def _graph_counts(ws):
    g = ws.load_graph()
    return len(g.nodes), sum(1 for e in g.edges if e.status in ("approved", "superseded"))


# ---------------------------------------------------------------------------------------------
# registry & sanitizer & validators
# ---------------------------------------------------------------------------------------------


def test_registry_lists_all_ten_adapters(graph_ws):
    reg = load_registry()
    assert [a["name"] for a in reg] == TARGETS
    for a in reg:
        assert {"title", "module", "class", "extra", "formats", "env", "push"} <= set(a)
        adapter = get_adapter(a["name"], graph_ws)
        assert isinstance(adapter, base.BaseAdapter) and adapter.name == a["name"]
    with pytest.raises(AdapterError, match="unknown export target"):
        get_adapter("oracle", graph_ws)


def test_sanitizer_reserved_words_keys_and_collisions():
    s = Sanitizer("keep", reserved=["CHARACTER", "TYPE"], reserved_suffix="_t")
    assert s.label("Character") == "Character_t" and s.label("character") == "Character_t_2"
    assert s.prop("type") == "type_t" and s.prop("startDate") == "start_date"
    assert s.rel_type("works at") == "WORKS_AT" and s.rel_type("1st") == "R_1ST"
    assert s.symbol_map()["labels"]["Character"] == "Character_t"
    a = Sanitizer("arango_key")
    k = a.node_id("person:anne marie/2")
    assert re.fullmatch(r"[A-Za-z0-9_\-:.@()+,=;$!*'%]+", k) and k != "person:anne marie/2"
    assert a.node_id("person:anne marie/2") == k and a.symbol_map()["node_ids"]["person:anne marie/2"] == k
    assert a.node_id("company:acme") == "company:acme"


def test_structural_validators():
    assert base.validate_cypher("MATCH (n) RETURN n;\nCREATE (:A {x: 'a;b'});")[0]
    assert not base.validate_cypher("MATCH (n RETURN n;")[0]
    assert not base.validate_cypher("SELECT 1;")[0]
    ok, _, n = base.validate_sql("DO $kumo$ BEGIN PERFORM 1; END $kumo$;\nSELECT 'it''s';")
    assert ok and n == 2
    assert not base.validate_sql("CREATE TABLE t (a INT;")[0]
    assert not base.validate_balanced("echo \"unterminated", "shell")[0]


# ---------------------------------------------------------------------------------------------
# Mode A bundles + parity for every target / provenance mode
# ---------------------------------------------------------------------------------------------

CASES = [(t, m, {}) for t in TARGETS for m in ("props", "nodes", "reified")] + [
    ("postgres", "props", {"pg_mode": "age"}), ("postgres", "props", {"pg_mode": "pgvector"}),
    ("neptune", "props", {"neptune_mode": "rdf"}), ("neptune", "reified", {"neptune_mode": "rdf"})]


@pytest.mark.parametrize("target,mode,opts", CASES, ids=[f"{t}-{m}-{'-'.join(o.values()) or 'default'}" for t, m, o in CASES])
def test_export_bundle_and_parity(graph_ws, target, mode, opts):
    res = run_export(graph_ws, target, provenance_mode=mode, options=opts)
    out = Path(res["out_dir"])
    p = res["parity"]
    assert p["ok"], p["errors"]
    assert p["dangling"] == 0 and p["nodes_found"] == p["nodes_expected"] and p["edges_found"] == p["edges_expected"]
    assert all(v["ok"] for v in p["syntax"].values())
    n_nodes, n_edges = _graph_counts(graph_ws)
    if target in ("postgres", "duckdb", "rdf") or opts.get("neptune_mode") == "rdf" or mode == "props":
        assert (p["nodes_expected"], p["edges_expected"]) == (n_nodes, n_edges)
    else:
        assert p["nodes_expected"] > n_nodes and p["edges_expected"] > n_edges  # evidence / statement nodes
    for f in ["README_IMPORT.md", "symbol_map.json", "export_manifest.json"] + (REQUIRED[target] if not opts else []):
        assert (out / f).exists(), f
    manifest = json.loads((out / "export_manifest.json").read_text(encoding="utf-8"))
    assert manifest["provenance_mode"] == mode and manifest["options"] == opts
    for f in manifest["files"]:
        assert sha256_file(out / f["path"]) == f["sha256"]
    if opts.get("pg_mode") == "age":
        assert (out / "age_load.sql").exists() and "create_graph" in (out / "schema.sql").read_text(encoding="utf-8")
    if opts.get("pg_mode") == "pgvector":
        assert "vector(384)" in (out / "schema.sql").read_text(encoding="utf-8")
    if opts.get("neptune_mode") == "rdf":
        assert (out / "data.nq").exists() and json.loads((out / "loader-request.json").read_text())["format"] == "nquads"


def test_rdf_bundle_semantics_with_rdflib(graph_ws):
    res = run_export(graph_ws, "rdf", provenance_mode="nodes")
    out = Path(res["out_dir"])
    n_nodes, n_edges = _graph_counts(graph_ws)
    onto = RDFGraph().parse(out / "ontology.ttl", format="turtle")
    ns = graph_ws.load_ontology(True).namespace
    hq = URIRef(ns + "ontology/HEADQUARTERED_IN")
    assert (hq, RDF.type, OWL.FunctionalProperty) in onto and (hq, RDF.type, OWL.ObjectProperty) in onto
    classes = set(onto.subjects(RDF.type, OWL.Class))
    shapes = RDFGraph().parse(out / "shapes.shacl.ttl", format="turtle")
    sh = URIRef("http://www.w3.org/ns/shacl#NodeShape")
    assert len(set(shapes.subjects(RDF.type, sh))) == len({c for c in classes if str(c).startswith(ns)})
    ds = Dataset()
    ds.parse(out / "data.nq", format="nquads")
    edge_graphs = {str(g.identifier) for g in ds.graphs() if "/graph/edge/" in str(g.identifier)}
    assert len(edge_graphs) == n_edges
    data = RDFGraph().parse(out / "data.ttl", format="turtle")
    stmts = set(data.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == n_edges
    q = data.query(f"""PREFIX kumo: <https://kumokg.dev/vocab#>
        SELECT ?vf ?vt WHERE {{ ?st a rdf:Statement ; rdf:predicate <{ns}ontology/WORKS_AT> ;
          rdf:subject <{ns}resource/person:elena-voss> ; kumo:status "superseded" ; kumo:validFrom ?vf ; kumo:validTo ?vt }}""")
    rows = list(q)
    assert rows and str(rows[0][0]) == "2019-03-01" and str(rows[0][1]) == "2023-06-01"
    active = list(data.triples((URIRef(ns + "resource/person:elena-voss"), URIRef(ns + "ontology/WORKS_AT"), None)))
    assert [str(o) for _, _, o in active] == [ns + "resource/company:globex-dynamics"]
    jsonld = RDFGraph().parse(out / "data.jsonld", format="json-ld")
    assert len(jsonld) == len(data)
    evidence = set(data.subjects(RDF.type, URIRef("https://kumokg.dev/vocab#Evidence")))
    assert evidence


def test_duckdb_bundle_queries(graph_ws):
    import duckdb

    out = Path(run_export(graph_ws, "duckdb")["out_dir"])
    n_nodes, n_edges = _graph_counts(graph_ws)
    con = duckdb.connect()
    try:
        assert con.execute(f"SELECT count(*) FROM read_parquet('{(out / 'nodes.parquet').as_posix()}')").fetchone()[0] == n_nodes
        leads = con.execute(f"""SELECT n.name FROM read_parquet('{(out / 'edges.parquet').as_posix()}') e
            JOIN read_parquet('{(out / 'nodes.parquet').as_posix()}') n ON n.id = e.source_id
            WHERE e.type = 'LEADS' ORDER BY 1""").fetchall()
        assert ("Elena Voss",) in leads
        ev = con.execute(f"SELECT count(*) FROM read_parquet('{(out / 'evidence.parquet').as_posix()}') WHERE kind = 'table'").fetchone()[0]
        assert ev > 0
    finally:
        con.close()
    db = duckdb.connect(str(out / "kumokg.duckdb"), read_only=True)
    try:
        assert db.execute("SELECT count(*) FROM edges").fetchone()[0] == n_edges
        hist = db.execute("SELECT status, valid_to FROM edges WHERE source_id = 'person:elena-voss' AND type = 'WORKS_AT' ORDER BY valid_from").fetchall()
        assert hist == [("superseded", "2023-06-01"), ("approved", None)]
        assert db.execute("SELECT count(*) FROM active_edges").fetchone()[0] == n_edges - 1
    finally:
        db.close()
    import networkx as nx

    g = nx.read_graphml(out / "graph.graphml")
    assert g.number_of_nodes() == n_nodes and g.number_of_edges() == n_edges


def test_exports_are_deterministic(graph_ws):
    a = {f["path"]: f["sha256"] for f in run_export(graph_ws, "neo4j", verify=False)["files"]}
    b = {f["path"]: f["sha256"] for f in run_export(graph_ws, "neo4j", verify=False)["files"]}
    assert a == b
    r1 = {f["path"]: f["sha256"] for f in run_export(graph_ws, "rdf", verify=False)["files"]}
    r2 = {f["path"]: f["sha256"] for f in run_export(graph_ws, "rdf", verify=False)["files"]}
    assert r1 == r2


def test_parity_detects_tampering(graph_ws):
    out = Path(run_export(graph_ws, "neo4j")["out_dir"])
    f = out / "nodes_Person.csv"
    lines = f.read_text(encoding="utf-8").splitlines()
    f.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    p = get_adapter("neo4j", graph_ws).verify_parity()
    assert not p["ok"] and p["dangling"] > 0 and any("checksum mismatch" in e for e in p["errors"])
    assert any("node count" in e for e in p["errors"])
    out = Path(run_export(graph_ws, "rdf")["out_dir"])
    (out / "data.ttl").write_text("@prefix x: <http://x/> .\nx:a x:b ", encoding="utf-8")
    p = get_adapter("rdf", graph_ws).verify_parity()
    assert not p["ok"] and not p["syntax"]["data.ttl"]["ok"]
    out = Path(run_export(graph_ws, "tigergraph")["out_dir"])
    job = (out / "loading_job.gsql").read_text(encoding="utf-8")
    (out / "loading_job.gsql").write_text(job.replace('$"kumo_id", ', "", 1), encoding="utf-8")
    p = get_adapter("tigergraph", graph_ws).verify_parity()
    assert not p["ok"] and "values for VERTEX" in p["syntax"]["loading_job.gsql"]["detail"]
    run_export(graph_ws, "janusgraph")
    gs = graph_ws.exports_dir / "janusgraph" / "graph.graphson"
    rows = gs.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace('"inE"', '"inX"', 1)
    gs.write_text("\n".join(rows) + "\n", encoding="utf-8")
    p = get_adapter("janusgraph", graph_ws).verify_parity()
    assert not p["ok"] and "mismatch" in p["syntax"]["graph.graphson"]["detail"]


def test_tigergraph_keyword_collision_is_renamed(graph_ws, tmp_path):
    import shutil
    from core.ir import Workspace

    ws = Workspace(tmp_path / "tg")
    shutil.copytree(graph_ws.root, ws.root)
    g = ws.load_graph()
    for n in g.nodes:
        if n.label == "Person":
            n.labels = ["Character"]
    ont = ws.load_ontology(True)
    from core.structured_compiler import rename_class
    from core.ir import write_json

    rename_class(ont, "Person", "Character")
    write_json(ws.ontology_approved_path, ont)
    ws.save_graph(g)
    res = run_export(ws, "tigergraph")
    out = Path(res["out_dir"])
    assert res["parity"]["ok"], res["parity"]["errors"]
    assert "CREATE VERTEX Character_t " in (out / "schema.gsql").read_text(encoding="utf-8")
    assert json.loads((out / "symbol_map.json").read_text())["labels"]["Character"] == "Character_t"


# ---------------------------------------------------------------------------------------------
# Mode B live push against faithful doubles / real engines
# ---------------------------------------------------------------------------------------------


class GraphStoreDouble:
    """Tracks pushed vertices/edges and refuses edges whose endpoints were not pushed first."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.queries: list[str] = []

    def add_node(self, kid, data=None):
        self.nodes[kid] = data or {}

    def add_edge(self, eid, s, t, data=None):
        assert s in self.nodes and t in self.nodes, f"edge {eid} pushed before its endpoints"
        self.edges[eid] = data or {}


class BoltDouble:
    def __init__(self):
        self.store = GraphStoreDouble()
        self.closed = False

    class _Res:
        def __init__(self, rec=None):
            self.rec = rec

        def consume(self):
            return None

        def single(self):
            return self.rec

    def _run(self, q, params):
        st = self.store
        st.queries.append(q)
        if "count(n)" in q:
            return self._Res({"c": len(st.nodes)})
        if "count(r)" in q:
            return self._Res({"c": len(st.edges)})
        for row in params.get("rows", []):
            if "MERGE (n:" in q:
                st.add_node(row["kumo_id"], row["props"])
            elif "MERGE (s)-[r:" in q:
                st.add_edge(row["id"], row["s"], row["t"], row["props"])
        return self._Res()

    def session(self, database=None):
        outer = self

        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run(self, q, **params):
                return outer._run(q, params)

            def execute_write(self, fn):
                class Tx:
                    def run(self, q, **params):
                        return outer._run(q, params)
                return fn(Tx())
        return S()

    def close(self):
        self.closed = True


class FalkorDouble(BoltDouble):
    def query(self, q, params=None):
        params = params or {}
        if "count(n)" in q:
            return type("R", (), {"result_set": [[len(self.store.nodes)]]})()
        if "count(r)" in q:
            return type("R", (), {"result_set": [[len(self.store.edges)]]})()
        self._run(q, params)
        return type("R", (), {"result_set": []})()


class ArangoDouble:
    def __init__(self):
        self.cols: dict[str, dict] = {}
        self.edge_cols: set[str] = set()
        self.graphs = {}

    def has_collection(self, n):
        return n in self.cols

    def create_collection(self, n, edge=False):
        self.cols[n] = {}
        if edge:
            self.edge_cols.add(n)
        return self.collection(n)

    def collection(self, n):
        outer = self

        class C:
            def import_bulk(self, docs, on_duplicate="error"):
                for d in docs:
                    if n in outer.edge_cols:
                        for ref in (d["_from"], d["_to"]):
                            col, key = ref.split("/", 1)
                            assert key in outer.cols.get(col, {}), f"dangling {ref}"
                    outer.cols[n][d["_key"]] = d

            def count(self):
                return len(outer.cols[n])
        return C()

    def has_graph(self, n):
        return n in self.graphs

    def create_graph(self, n, edge_definitions, orphan_collections):
        assert all(d["edge_collection"] in self.edge_cols for d in edge_definitions)
        self.graphs[n] = edge_definitions


class TigerDouble:
    def __init__(self):
        self.v: dict[tuple, dict] = {}
        self.e: dict[tuple, dict] = {}
        self.schema = ""

    def gsql(self, text):
        assert "CREATE VERTEX" in text
        self.schema = text

    def upsertVertices(self, vtype, rows):
        assert f"CREATE VERTEX {vtype} (" in self.schema
        for vid, attrs in rows:
            self.v[(vtype, vid)] = attrs
        return len(rows)

    def upsertEdges(self, st, et, tt, rows):
        for s, t, attrs in rows:
            assert (st, s) in self.v and (tt, t) in self.v
            self.e[(et, s, t, attrs.get("kumo_id"))] = attrs
        return len(rows)

    def getVertexCount(self, _):
        out = {}
        for (vt, _), _ in self.v.items():
            out[vt] = out.get(vt, 0) + 1
        return out

    def getEdgeCount(self, _):
        return {"*": len(self.e)}


class GremlinDouble:
    """Minimal TinkerPop traversal interpreter for the coalesce-upsert pattern used by gremlin_push."""

    def __init__(self):
        self.v: dict[str, dict] = {}
        self.e: dict[str, dict] = {}
        outer = self

        class Anon:
            def __getattr__(self, name):
                return lambda *a: T(outer, [(name, a)])
        self.anon = Anon()

    def V(self, *a):
        return T(self, [("V", a)])

    def E(self, *a):
        return T(self, [("E", a)])


class T:
    def __init__(self, g, steps):
        self.g, self.steps = g, steps

    def __getattr__(self, name):
        return lambda *a: T(self.g, self.steps + [(name, a)])

    def _props(self, steps):
        return {a[0]: a[1] for n, a in steps if n == "property"}

    def iterate(self):
        for name, args in self.steps:
            if name != "coalesce":
                continue
            add = args[1].steps
            kind, kargs = add[0]
            props = self._props(add)
            if kind == "addV" and props["kumo_id"] not in self.g.v:
                self.g.v[props["kumo_id"]] = props
            if kind == "addE" and props["kumo_id"] not in self.g.e:
                src = next(a[0].steps for n, a in add if n == "from_")
                dst = next(a[0].steps for n, a in add if n == "to")
                s = next(x[1][1] for x in src if x[0] == "has")
                t = next(x[1][1] for x in dst if x[0] == "has")
                assert s in self.g.v and t in self.g.v
                self.g.e[props["kumo_id"]] = props
        return self

    def next(self):
        return len(self.g.v) if self.steps[0][0] == "V" else len(self.g.e)


class PsycopgDouble:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.executed: list[str] = []
        self.committed = False
        self._last = None

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql):
                outer.executed.append(sql)
                m = re.match(r"SELECT count\(\*\) FROM (\w+)", sql)
                outer._last = (len(outer.tables.get(m.group(1), [])),) if m else None
                if sql.startswith("TRUNCATE"):
                    outer.tables.clear()

            def fetchone(self):
                return outer._last

            def copy(self, sql):
                table = re.match(r"COPY (\w+) ", sql).group(1)

                class Cp:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def write(self, data):
                        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
                        if table == "kg_edges":
                            ids = {r["id"] for r in outer.tables["kg_nodes"]}
                            assert all(r["source_id"] in ids and r["target_id"] in ids for r in rows)
                        outer.tables.setdefault(table, []).extend(rows)
                return Cp()
        return Cur()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.mark.parametrize("target,double", [("neo4j", BoltDouble), ("memgraph", BoltDouble), ("falkordb", FalkorDouble),
                                           ("arangodb", ArangoDouble), ("tigergraph", TigerDouble), ("janusgraph", GremlinDouble)])
def test_live_push_logic_with_doubles(graph_ws, target, double):
    adapter = get_adapter(target, graph_ws)
    adapter.export_bundle()
    client = double()
    res = adapter.push(client=client, env={})
    assert res["ok"], res
    assert res["nodes_in_db"] == res["expected"]["nodes"] and res["edges_in_db"] == res["expected"]["edges"]
    again = adapter.push(client=client, env={})  # idempotent upsert
    assert again["ok"] and again["nodes_in_db"] == res["nodes_in_db"]


def test_postgres_push_with_copy_double(graph_ws):
    for mode in ("plain", "age"):
        adapter = get_adapter("postgres", graph_ws, {"pg_mode": mode})
        adapter.export_bundle()
        conn = PsycopgDouble()
        res = adapter.push(client=conn, env={})
        assert res["ok"] and conn.committed
        assert any("CREATE TABLE IF NOT EXISTS kg_nodes" in s for s in conn.executed)
        assert len(conn.tables["kg_evidence"]) > 0
        if mode == "age":
            assert any("cypher('kumokg'" in s for s in conn.executed)


def test_duckdb_push_to_real_database(graph_ws, tmp_path):
    adapter = get_adapter("duckdb", graph_ws)
    adapter.export_bundle()
    target = tmp_path / "pushed.duckdb"
    res = adapter.push(env={"KUMOKG_DUCKDB_PATH": str(target)})
    assert res["ok"] and target.exists()
    import duckdb

    con = duckdb.connect(str(target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM nodes").fetchone()[0] == res["expected"]["nodes"]
    finally:
        con.close()


def _sparql_store():
    stored: dict[str, RDFGraph] = {}
    ds = Dataset()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            gname = request.url.params["graph"]
            g = RDFGraph().parse(data=request.content.decode("utf-8"), format="turtle")
            stored[gname] = g
            ctx = ds.graph(URIRef(gname))
            ctx.remove((None, None, None))
            for t in g:
                ctx.add(t)
            return httpx.Response(201)
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        from urllib.parse import unquote_plus

        if "update" in form:
            ds.update(unquote_plus(form["update"]))
            return httpx.Response(200, text="ok")
        rows = list(ds.query(unquote_plus(form["query"])))
        return httpx.Response(200, json={"results": {"bindings": [{"n": {"type": "literal", "value": str(rows[0][0])}}]}})

    return stored, ds, handler


def test_rdf_graph_store_protocol_push(graph_ws):
    stored, ds, handler = _sparql_store()
    adapter = get_adapter("rdf", graph_ws)
    adapter.export_bundle()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = adapter.push(client=client, env={"KUMOKG_SPARQL_GSP_URL": "http://store.example/ds/data",
                                           "KUMOKG_SPARQL_QUERY_URL": "http://store.example/ds/query"})
    assert res["ok"], res
    assert len(stored) == 3 and any(g.endswith("graph/ontology") for g in stored)


def test_neptune_push_paths(graph_ws):
    lpg = get_adapter("neptune", graph_ws, {"neptune_mode": "lpg"})
    lpg.export_bundle()
    res = lpg.push(client=GremlinDouble(), env={"KUMOKG_NEPTUNE_ENDPOINT": "https://neptune.example:8182"})
    assert res["ok"] and res["method"] == "gremlin"
    _, ds, handler = _sparql_store()
    rdf = get_adapter("neptune", graph_ws, {"neptune_mode": "rdf"})
    rdf.export_bundle()
    res = rdf.push(client=httpx.Client(transport=httpx.MockTransport(handler)), env={"KUMOKG_NEPTUNE_ENDPOINT": "https://neptune.example:8182"})
    assert res["ok"] and res["method"] == "sparql_update" and res["update_batches"] > 0


def test_missing_driver_and_env_give_clear_errors(graph_ws):
    import importlib.util

    adapter = get_adapter("neo4j", graph_ws)
    if importlib.util.find_spec("neo4j") is None:
        with pytest.raises(DriverMissing, match="--extra neo4j"):
            adapter.push(env={})
    duck = get_adapter("duckdb", graph_ws)
    with pytest.raises(AdapterError, match="KUMOKG_DUCKDB_PATH"):
        duck.push(env={})
    rdf = get_adapter("rdf", graph_ws)
    with pytest.raises(AdapterError, match="KUMOKG_SPARQL_GSP_URL"):
        rdf.push(env={})


# ---------------------------------------------------------------------------------------------
# CLI, Studio and MCP entrypoints
# ---------------------------------------------------------------------------------------------


def test_cli_export_all_and_verify_export(graph_ws):
    from kumokg_cli import app

    runner = CliRunner()
    w = ["--workspace", str(graph_ws.root)]
    r = runner.invoke(app, ["export", "--target", "all"] + w)
    assert r.exit_code == 0, r.output
    assert len(json.loads(r.output)["results"]) == 10
    r = runner.invoke(app, ["verify-export", "--target", "all"] + w)
    out = json.loads(r.output)
    assert r.exit_code == 0 and out["passed"] == 10 and all(x["syntax_ok"] for x in out["results"])
    r = runner.invoke(app, ["export", "--target", "postgres", "--pg-mode", "age"] + w)
    assert r.exit_code == 0
    r = runner.invoke(app, ["export", "--target", "bogus"] + w)
    assert r.exit_code == 1
    r = runner.invoke(app, ["adapters"])
    assert [a["name"] for a in json.loads(r.output)["adapters"]] == TARGETS


def test_studio_export_endpoint_and_mcp_tool(graph_ws):
    from core.mcp_server import KGTools
    from studio.server import StudioServer

    srv = StudioServer(graph_ws, port=0).start()
    try:
        with httpx.Client(base_url=srv.url, timeout=60) as c:
            assert len(c.get("/api/adapters").json()["adapters"]) == 10
            r = c.post("/api/export", json={"target": "arangodb", "provenance_mode": "nodes"}, headers={"Origin": srv.url})
            body = r.json()
            assert r.status_code == 200 and body["ok"] and body["parity"]["ok"] and body["files"]
            r = c.post("/api/export", json={"target": "postgres", "options": {"pg_mode": "pgvector"}})
            assert r.json()["parity"]["ok"]
            assert c.post("/api/export", json={}).status_code == 400
    finally:
        srv.stop()
    res = KGTools(graph_ws).kg_export("janusgraph")
    assert res["parity"]["ok"] and "graph.graphson" in res["files"]


@pytest.mark.live
def test_live_docker_stack_round_trip(graph_ws):
    """Runs only with `KUMOKG_LIVE_TESTS=1 uv run pytest -m live` against docker-compose.test.yml."""
    if os.environ.get("KUMOKG_LIVE_TESTS") != "1":
        pytest.fail("set KUMOKG_LIVE_TESTS=1 and start docker-compose.test.yml to run live tests")
    env = {"KUMOKG_NEO4J_URI": "bolt://localhost:7687", "KUMOKG_NEO4J_USER": "neo4j", "KUMOKG_NEO4J_PASSWORD": "kumokg-test",
           "KUMOKG_MEMGRAPH_URI": "bolt://localhost:7688", "KUMOKG_POSTGRES_DSN": "postgresql://kumokg:kumokg@localhost:5432/kumokg",
           "KUMOKG_FALKORDB_HOST": "localhost", "KUMOKG_FALKORDB_PORT": "6379",
           "KUMOKG_SPARQL_GSP_URL": "http://localhost:7878/store", "KUMOKG_SPARQL_QUERY_URL": "http://localhost:7878/query"}
    for target, opts in (("neo4j", {}), ("memgraph", {}), ("postgres", {"pg_mode": "age"}), ("falkordb", {}), ("rdf", {})):
        adapter = get_adapter(target, graph_ws, opts)
        adapter.export_bundle()
        assert adapter.push(env=env)["ok"], target

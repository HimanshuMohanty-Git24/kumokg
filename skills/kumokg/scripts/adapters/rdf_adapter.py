"""RDF (GraphDB, Stardog, Oxigraph, Apache Jena): OWL 2 ``ontology.ttl``, SHACL ``shapes.shacl.ttl``,
``data.ttl`` (current triples + ``rdf:Statement`` reification of every edge incl. bi-temporal history),
``data.nq`` (one named graph per edge + a provenance graph), ``data.jsonld`` and a SPARQL 1.1 Graph Store
HTTP Protocol live push with SPARQL count parity."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rdflib import BNode, Dataset, Graph as RDFGraph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, PROV, RDF, RDFS, SKOS, XSD

from core.ir import Graph, Ontology, Workspace, evidence_key, evidence_text
from .base import AdapterError, BaseAdapter, Parsed, Projection, evidence_locator

KUMO = Namespace("https://kumokg.dev/vocab#")
SH = Namespace("http://www.w3.org/ns/shacl#")
XSD_OF = {"int": XSD.integer, "float": XSD.double, "bool": XSD.boolean, "date": XSD.date, "string": XSD.string}


def _ns(ont: Ontology) -> str:
    ns = ont.namespace or "https://kumokg.dev/kg/"
    return ns if ns.endswith(("/", "#")) else ns + "/"


def _lit(v: Any, ptype: str | None = None) -> Literal:
    if isinstance(v, bool):
        return Literal(v, datatype=XSD.boolean)
    if isinstance(v, int):
        return Literal(v, datatype=XSD.integer)
    if isinstance(v, float):
        return Literal(v, datatype=XSD.double)
    s = v if isinstance(v, str) else str(v)
    if ptype == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return Literal(s, datatype=XSD.date)
    return Literal(s)


class RDFBuild:
    """All RDF artefacts for one graph snapshot."""

    def __init__(self, graph: Graph, ont: Ontology, provenance_mode: str):
        self.ns = _ns(ont)
        self.O = Namespace(self.ns + "ontology/")
        self.R = Namespace(self.ns + "resource/")
        self.S = Namespace(self.ns + "statement/")
        self.E = Namespace(self.ns + "evidence/")
        self.G = Namespace(self.ns + "graph/")
        self.graph, self.ont, self.mode = graph, ont, provenance_mode
        self.nodes = {n.id: n for n in graph.nodes}
        self.edges = [e for e in graph.edges if e.status in ("approved", "superseded")
                      and e.source_id in self.nodes and e.target_id in self.nodes]

    def res(self, node_id: str) -> URIRef:
        return self.R[quote(node_id, safe=":-_.~")]

    def stmt(self, edge_id: str) -> URIRef:
        return self.S[quote(edge_id, safe="-_.~")]

    def evi(self, ev: Any) -> URIRef:
        return self.E[evidence_key(ev)]

    def cls(self, name: str) -> URIRef:
        return self.O[re.sub(r"[^A-Za-z0-9_]", "_", name)]

    def pred(self, name: str) -> URIRef:
        return self.O[re.sub(r"[^A-Za-z0-9_]", "_", name)]

    def dprop(self, key: str) -> URIRef:
        return self.O["prop_" + re.sub(r"[^A-Za-z0-9_]", "_", key)]

    def _ptype(self, label: str, key: str) -> str | None:
        c = self.ont.get_class(label)
        return c.properties[key].type if c and key in c.properties else None

    # ontology ----------------------------------------------------------------------------
    def ontology_graph(self) -> RDFGraph:
        g = RDFGraph()
        self._bind(g)
        onto = URIRef(self.ns + "ontology")
        g.add((onto, RDF.type, OWL.Ontology))
        g.add((onto, RDFS.label, Literal("KumoKG ontology")))
        labels = {n.label for n in self.graph.nodes} | self.ont.class_names()
        for name in sorted(labels):
            c = self.ont.get_class(name)
            g.add((self.cls(name), RDF.type, OWL.Class))
            g.add((self.cls(name), RDFS.label, Literal(name)))
            if c and c.description:
                g.add((self.cls(name), RDFS.comment, Literal(c.description)))
        triples = defaultdict(set)
        for r in self.ont.relations:
            triples[r.predicate].add((r.source_class, r.target_class))
        for e in self.edges:
            triples[e.type].add((self.nodes[e.source_id].label, self.nodes[e.target_id].label))
        for pred, pairs in sorted(triples.items()):
            p = self.pred(pred)
            g.add((p, RDF.type, OWL.ObjectProperty))
            g.add((p, RDFS.label, Literal(pred)))
            for role, idx in ((RDFS.domain, 0), (RDFS.range, 1)):
                classes = sorted({pair[idx] for pair in pairs})
                if len(classes) == 1:
                    g.add((p, role, self.cls(classes[0])))
                else:
                    tag = f"{pred}_{'dom' if idx == 0 else 'rng'}"
                    union = BNode(f"union_{tag}")
                    lst = BNode(f"list_{tag}")
                    Collection(g, lst, [self.cls(c) for c in classes])
                    g.add((union, RDF.type, OWL.Class))
                    g.add((union, OWL.unionOf, lst))
                    g.add((p, role, union))
            if any(r.functional for r in self.ont.rules_for(pred)):
                g.add((p, RDF.type, OWL.FunctionalProperty))
            if any(r.symmetric for r in self.ont.rules_for(pred)):
                g.add((p, RDF.type, OWL.SymmetricProperty))
        dprops = defaultdict(set)
        dtypes: dict[str, str] = {}
        for c in self.ont.entity_classes:
            for k, ps in c.properties.items():
                dprops[k].add(c.name)
                dtypes[k] = ps.type
        for n in self.graph.nodes:
            for k in n.properties:
                dprops[k].add(n.label)
        for k, classes in sorted(dprops.items()):
            p = self.dprop(k)
            g.add((p, RDF.type, OWL.DatatypeProperty))
            g.add((p, RDFS.label, Literal(k)))
            if len(classes) == 1:
                g.add((p, RDFS.domain, self.cls(next(iter(classes)))))
            if k in dtypes:
                g.add((p, RDFS.range, XSD_OF[dtypes[k]]))
        for name, kind in (("kumoId", OWL.DatatypeProperty), ("confidence", OWL.DatatypeProperty), ("validFrom", OWL.DatatypeProperty),
                           ("validTo", OWL.DatatypeProperty), ("status", OWL.DatatypeProperty), ("supersededBy", OWL.ObjectProperty),
                           ("Evidence", OWL.Class), ("quote", OWL.DatatypeProperty), ("evidenceJson", OWL.DatatypeProperty)):
            g.add((KUMO[name], RDF.type, kind))
        return g

    def shapes_graph(self) -> RDFGraph:
        g = RDFGraph()
        self._bind(g)
        g.bind("sh", SH)
        rels = defaultdict(lambda: defaultdict(set))
        functional = set()
        for r in self.ont.relations:
            rels[r.source_class][r.predicate].add(r.target_class)
            if r.functional:
                functional.add((r.source_class, r.predicate))
        for e in self.edges:
            rels[self.nodes[e.source_id].label][e.type].add(self.nodes[e.target_id].label)
        labels = sorted({n.label for n in self.graph.nodes} | self.ont.class_names())
        for name in labels:
            shape = URIRef(self.ns + "shape/" + re.sub(r"[^A-Za-z0-9_]", "_", name))
            g.add((shape, RDF.type, SH.NodeShape))
            g.add((shape, SH.targetClass, self.cls(name)))

            def prop(path, **kw):
                local = re.sub(r"[^A-Za-z0-9_]", "_", str(path).rsplit("/", 1)[-1].rsplit("#", 1)[-1])
                b = URIRef(f"{shape}/{local}")
                g.add((shape, SH.property, b))
                g.add((b, SH.path, path))
                for k, v in kw.items():
                    g.add((b, SH[k], v))
                return b

            prop(RDFS.label, minCount=Literal(1), datatype=XSD.string)
            prop(KUMO.kumoId, minCount=Literal(1), maxCount=Literal(1))
            c = self.ont.get_class(name)
            for k, ps in sorted((c.properties if c else {}).items()):
                kw = {"datatype": XSD_OF[ps.type]}
                if ps.functional:
                    kw["maxCount"] = Literal(1)
                prop(self.dprop(k), **kw)
            for pred, targets in sorted(rels.get(name, {}).items()):
                kw: dict[str, Any] = {"nodeKind": SH.IRI}
                if (name, pred) in functional:
                    kw["maxCount"] = Literal(1)
                b = prop(self.pred(pred), **kw)
                if len(targets) == 1:
                    g.add((b, SH["class"], self.cls(next(iter(targets)))))
                else:
                    items = []
                    for t in sorted(targets):
                        alt = BNode(f"or_{name}_{pred}_{t}")
                        g.add((alt, SH["class"], self.cls(t)))
                        items.append(alt)
                    lst = BNode(f"orlist_{name}_{pred}")
                    Collection(g, lst, items)
                    g.add((b, SH["or"], lst))
        return g

    # data ----------------------------------------------------------------------------------
    def _evidence_triples(self, add, owner: URIRef, evidence: list, graph_ctx=None) -> None:
        if self.mode == "props":
            if evidence:
                import json

                add((owner, KUMO.evidenceJson, Literal(json.dumps([e.model_dump(mode="json") for e in evidence], sort_keys=True,
                                                                ensure_ascii=False))), graph_ctx)
                add((owner, KUMO.primaryQuote, Literal(evidence_text(evidence[0])[:500])), graph_ctx)
            add((owner, KUMO.evidenceCount, Literal(len(evidence))), graph_ctx)
            return
        for ev in evidence:
            eu = self.evi(ev)
            add((owner, PROV.wasDerivedFrom, eu), graph_ctx)
            add((eu, RDF.type, KUMO.Evidence), graph_ctx)
            add((eu, KUMO.kind, Literal(ev.kind)), graph_ctx)
            add((eu, KUMO.docId, Literal(ev.doc_id)), graph_ctx)
            add((eu, KUMO.quote, Literal(evidence_text(ev)[:2000])), graph_ctx)
            add((eu, KUMO.locator, Literal(evidence_locator(ev))), graph_ctx)

    def _node_triples(self, add, ctx=None) -> None:
        for n in sorted(self.graph.nodes, key=lambda x: x.id):
            r = self.res(n.id)
            add((r, RDF.type, self.cls(n.label)), ctx)
            for extra in n.labels[1:]:
                add((r, RDF.type, self.cls(extra)), ctx)
            add((r, RDFS.label, Literal(n.canonical_name)), ctx)
            add((r, KUMO.kumoId, Literal(n.id)), ctx)
            add((r, KUMO.confidence, Literal(float(n.confidence))), ctx)
            for a in sorted(n.aliases):
                add((r, SKOS.altLabel, Literal(a)), ctx)
            for k, v in sorted(n.properties.items()):
                if v is None:
                    continue
                vals = v if isinstance(v, list) else [v]
                for item in vals:
                    add((r, self.dprop(k), _lit(item if not isinstance(item, dict) else str(item), self._ptype(n.label, k))), ctx)

    def data_graph(self) -> RDFGraph:
        g = RDFGraph()
        self._bind(g)
        add = lambda t, _ctx=None: g.add(t)  # noqa: E731
        self._node_triples(add)
        for n in self.graph.nodes:
            self._evidence_triples(add, self.res(n.id), n.evidence)
        for e in sorted(self.edges, key=lambda x: x.id):
            s, p, o = self.res(e.source_id), self.pred(e.type), self.res(e.target_id)
            if e.active_at(None):
                g.add((s, p, o))
            st = self.stmt(e.id)
            g.add((st, RDF.type, RDF.Statement))
            g.add((st, RDF.subject, s))
            g.add((st, RDF.predicate, p))
            g.add((st, RDF.object, o))
            self._edge_meta(add, st, e)
        return g

    def _edge_meta(self, add, subj: URIRef, e, ctx=None) -> None:
        add((subj, KUMO.kumoId, Literal(e.id)), ctx)
        add((subj, KUMO.confidence, Literal(float(e.confidence))), ctx)
        add((subj, KUMO.status, Literal(e.status)), ctx)
        if e.valid_from:
            add((subj, KUMO.validFrom, Literal(e.valid_from)), ctx)
        if e.valid_to:
            add((subj, KUMO.validTo, Literal(e.valid_to)), ctx)
        if e.superseded_by:
            add((subj, KUMO.supersededBy, self.stmt(e.superseded_by)), ctx)
        self._evidence_triples(add, subj, e.evidence, ctx)

    def dataset(self) -> Dataset:
        ds = Dataset()
        self._bind(ds)
        ent = ds.graph(self.G["entities"])
        prov = ds.graph(self.G["provenance"])

        def add(t, ctx=None):
            (ctx if ctx is not None else ent).add(t)

        self._node_triples(add, ent)
        for n in self.graph.nodes:
            self._evidence_triples(add, self.res(n.id), n.evidence, prov)
        for e in sorted(self.edges, key=lambda x: x.id):
            gname = self.G["edge/" + quote(e.id, safe="-_.~")]
            ds.graph(gname).add((self.res(e.source_id), self.pred(e.type), self.res(e.target_id)))
            self._edge_meta(add, gname, e, prov)
        return ds

    def _bind(self, g) -> None:
        g.bind("kumo", KUMO)
        g.bind("onto", self.O)
        g.bind("res", self.R)
        g.bind("prov", PROV)
        g.bind("skos", SKOS)
        g.bind("owl", OWL)

    def write(self, out: Path, files: tuple[str, ...] = ("ontology.ttl", "shapes.shacl.ttl", "data.ttl", "data.nq", "data.jsonld")) -> None:
        data = self.data_graph() if {"data.ttl", "data.jsonld"} & set(files) else None
        if "ontology.ttl" in files:
            (out / "ontology.ttl").write_text(self.ontology_graph().serialize(format="turtle"), encoding="utf-8", newline="\n")
        if "shapes.shacl.ttl" in files:
            (out / "shapes.shacl.ttl").write_text(self.shapes_graph().serialize(format="turtle"), encoding="utf-8", newline="\n")
        if "data.ttl" in files:
            (out / "data.ttl").write_text(data.serialize(format="turtle"), encoding="utf-8", newline="\n")
        if "data.nq" in files:
            text = self.dataset().serialize(format="nquads")
            lines = sorted(ln for ln in text.splitlines() if ln.strip())
            (out / "data.nq").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        if "data.jsonld" in files:
            ctx = {"@vocab": str(self.O), "kumo": str(KUMO), "res": str(self.R), "rdfs": str(RDFS), "skos": str(SKOS),
                   "prov": str(PROV), "xsd": str(XSD)}
            (out / "data.jsonld").write_text(data.serialize(format="json-ld", context=ctx, indent=1, sort_keys=True),
                                             encoding="utf-8", newline="\n")

    def counts(self) -> dict[str, int]:
        return {"nodes": len(self.graph.nodes), "edges": len(self.edges)}


def parse_rdf_bundle(out: Path, ns: str, files: tuple[str, ...]) -> Parsed:
    """Parse the RDF files with rdflib and extract entity / statement identities for parity."""
    syntax: dict[str, dict[str, Any]] = {}
    onto_prefix = ns + "ontology/"
    graphs: dict[str, Any] = {}
    fmt = {"ontology.ttl": "turtle", "shapes.shacl.ttl": "turtle", "data.ttl": "turtle", "data.nq": "nquads", "data.jsonld": "json-ld"}
    for name in files:
        try:
            g = Dataset() if name.endswith(".nq") else RDFGraph()
            g.parse(out / name, format=fmt[name])
            graphs[name] = g
            syntax[name] = {"ok": True, "detail": "parsed by rdflib", "triples": len(g)}
        except Exception as exc:
            syntax[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    node_ids: list[str] = []
    edges: list[tuple[str, str, str]] = []
    extra: dict[str, Any] = {}
    main = graphs.get("data.ttl")
    if main is None and "data.nq" in graphs:
        main = None
    if main is not None:
        typed = sorted({str(s) for s, o in main.subject_objects(RDF.type) if str(o).startswith(onto_prefix)})
        node_ids = typed
        for st in sorted(main.subjects(RDF.type, RDF.Statement), key=str):
            edges.append((str(st), str(main.value(st, RDF.subject)), str(main.value(st, RDF.object))))
        extra["active_triples"] = sum(1 for _, p, _ in main if str(p).startswith(onto_prefix) and not str(p).startswith(onto_prefix + "prop_"))
    if "data.nq" in graphs:
        ds = graphs["data.nq"]
        edge_graphs = sorted({str(c) for _, _, _, c in ds.quads((None, None, None, None)) if str(c).startswith(ns + "graph/edge/")})
        nq_nodes = sorted({str(s) for s, _, o, c in ds.quads((None, RDF.type, None, None))
                           if str(o).startswith(onto_prefix) and str(c) == ns + "graph/entities"})
        extra["nq_edge_graphs"] = len(edge_graphs)
        extra["nq_entities"] = len(nq_nodes)
        if main is None:
            node_ids = nq_nodes
            for c in edge_graphs:
                for s, _, o in ds.graph(URIRef(c)):
                    edges.append((c, str(s), str(o)))
        elif len(edge_graphs) != len(edges) or len(nq_nodes) != len(node_ids):
            syntax["data.nq"] = {"ok": False, "detail": f"named-graph parity mismatch: {len(edge_graphs)} edge graphs vs {len(edges)} "
                                                          f"statements; {len(nq_nodes)} vs {len(node_ids)} entities"}
    if "data.jsonld" in graphs and main is not None and len(graphs["data.jsonld"]) != len(main):
        syntax["data.jsonld"] = {"ok": False, "detail": f"JSON-LD has {len(graphs['data.jsonld'])} triples, Turtle {len(main)}"}
    return Parsed(node_ids, edges, syntax, extra)


class RDFAdapter(BaseAdapter):
    name = "rdf"
    title = "RDF / OWL 2 / SHACL"
    extra = "rdf"
    FILES = ("ontology.ttl", "shapes.shacl.ttl", "data.ttl", "data.nq", "data.jsonld")

    def _build(self, graph: Graph | None, mode: str) -> RDFBuild:
        return RDFBuild(graph or self.ws.load_graph(), self.ws.load_ontology(True) or Ontology(), mode)

    def expected_counts(self, proj: Projection) -> dict[str, int]:
        return {"nodes": proj.counts()["domain_nodes"], "edges": proj.counts()["domain_edges"]}

    def write_files(self, proj: Projection, out: Path) -> None:
        self._build(None, proj.provenance_mode).write(out, self.FILES)

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        ns = _ns(self.ws.load_ontology(True) or Ontology())
        return f"""# KumoKG -> RDF (GraphDB, Stardog, Oxigraph, Apache Jena)

Graph hash `{proj.graph_hash}` · {c['domain_nodes']} entities · {c['domain_edges']} edge statements · namespace `{ns}`.

| file | content |
|---|---|
| `ontology.ttl` | OWL 2: classes, object properties (domain/range, `owl:unionOf`, Functional/Symmetric), datatype properties |
| `shapes.shacl.ttl` | SHACL node shapes per class (label/kumoId cardinality, datatypes, relation `sh:class`, `sh:maxCount 1` for functional) |
| `data.ttl` | current facts as plain triples + every edge (incl. superseded history) as `rdf:Statement` with `kumo:validFrom/validTo/status` |
| `data.nq` | N-Quads: entities in `<{ns}graph/entities>`, one named graph per edge, provenance about each edge graph in `<{ns}graph/provenance>` |
| `data.jsonld` | JSON-LD of `data.ttl` |

Load: Oxigraph `oxigraph load --location db --file data.nq`; Jena `tdb2.tdbloader --loc db data.nq`;
GraphDB/Stardog: import `data.nq` (preserves named graphs). Validate with any SHACL engine (e.g. `pyshacl -s shapes.shacl.ttl data.ttl`).
Live push (SPARQL 1.1 Graph Store Protocol): set `KUMOKG_SPARQL_GSP_URL` (+ `KUMOKG_SPARQL_QUERY_URL`, optional basic auth) and
`export --target rdf --push`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        ns = _ns(self.ws.load_ontology(True) or Ontology())
        return parse_rdf_bundle(out, ns, self.FILES)

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        import httpx

        (gsp,) = self.require_env(env, "KUMOKG_SPARQL_GSP_URL")
        query_url = env.get("KUMOKG_SPARQL_QUERY_URL") or gsp.rsplit("/", 1)[0] + "/query"
        auth = (env["KUMOKG_SPARQL_USER"], env.get("KUMOKG_SPARQL_PASSWORD", "")) if env.get("KUMOKG_SPARQL_USER") else None
        build = self._build(None, proj.provenance_mode)
        g_data = build.ns + "graph/kumokg"
        bodies = {g_data: build.data_graph(), build.ns + "graph/ontology": build.ontology_graph(),
                  build.ns + "graph/shapes": build.shapes_graph()}
        http = client or httpx.Client(timeout=60, auth=auth)
        try:
            for gname, g in bodies.items():
                r = http.put(gsp, params={"graph": gname}, content=g.serialize(format="turtle").encode("utf-8"),
                             headers={"Content-Type": "text/turtle"})
                if r.status_code not in (200, 201, 204):
                    raise AdapterError(f"Graph Store PUT {gname} failed: HTTP {r.status_code} {r.text[:200]}")

            def count(q: str) -> int:
                r = http.post(query_url, data={"query": q}, headers={"Accept": "application/sparql-results+json"})
                if r.status_code != 200:
                    raise AdapterError(f"SPARQL count failed: HTTP {r.status_code} {r.text[:200]}")
                return int(r.json()["results"]["bindings"][0]["n"]["value"])

            nodes = count(f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ GRAPH <{g_data}> {{ ?s a ?c . "
                          f"FILTER(STRSTARTS(STR(?c), \"{build.ns}ontology/\")) }} }}")
            edges = count(f"SELECT (COUNT(DISTINCT ?st) AS ?n) WHERE {{ GRAPH <{g_data}> {{ ?st a <{RDF.Statement}> }} }}")
        finally:
            if client is None:
                http.close()
        return {"target": self.name, "graphs": list(bodies), "nodes_in_db": nodes, "edges_in_db": edges}

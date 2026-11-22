"""ArangoDB: per-label vertex collections and per-type edge collections as JSONL (``_key`` / ``_from`` / ``_to``),
``graph_definition.json`` (named graph edge definitions), ``import.sh`` (``arangoimport``) and a python-arango push."""
from __future__ import annotations

import json
import re
import shlex
from collections import defaultdict
from pathlib import Path
from typing import Any

from .base import BaseAdapter, Parsed, Projection, group_by, require_driver, validate_balanced

ARANGO_RESERVED_PROPS = ("_key", "_id", "_rev", "_from", "_to")


class ArangoDBAdapter(BaseAdapter):
    name = "arangodb"
    title = "ArangoDB"
    extra = "arangodb"
    id_style = "arango_key"

    def _docs(self, proj: Projection):
        label_of = {n.id: n.label for n in proj.nodes}
        vertices = {label: [{"_key": n.id, **{k: v for k, v in n.props.items() if v is not None}} for n in nodes]
                    for label, nodes in group_by(proj.nodes, lambda n: n.label).items()}
        edges = {}
        defs: dict[str, dict[str, set]] = defaultdict(lambda: {"from": set(), "to": set()})
        for etype, items in group_by(proj.edges, lambda e: e.type).items():
            docs = []
            for e in items:
                defs[etype]["from"].add(label_of[e.source])
                defs[etype]["to"].add(label_of[e.target])
                docs.append({"_key": e.id, "_from": f"{label_of[e.source]}/{e.source}", "_to": f"{label_of[e.target]}/{e.target}",
                             **{k: v for k, v in e.props.items() if v is not None}})
            edges[etype] = docs
        graph_def = {"name": self.options.get("graph_name", "kumokg"),
                     "edgeDefinitions": [{"collection": t, "from": sorted(d["from"]), "to": sorted(d["to"])} for t, d in sorted(defs.items())],
                     "orphanCollections": sorted(set(vertices) - {c for d in defs.values() for c in d["from"] | d["to"]})}
        return vertices, edges, graph_def

    def write_files(self, proj: Projection, out: Path) -> None:
        vertices, edges, graph_def = self._docs(proj)
        (out / "vertices").mkdir()
        (out / "edges").mkdir()
        for label, docs in vertices.items():
            (out / "vertices" / f"{label}.jsonl").write_text("".join(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n" for d in docs),
                                                            encoding="utf-8", newline="\n")
        for etype, docs in edges.items():
            (out / "edges" / f"{etype}.jsonl").write_text("".join(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n" for d in docs),
                                                         encoding="utf-8", newline="\n")
        (out / "graph_definition.json").write_text(json.dumps(graph_def, indent=2) + "\n", encoding="utf-8")
        lines = ["#!/usr/bin/env sh", "# Requires arangoimport + arangosh (ArangoDB client tools).", "set -e", 'cd "$(dirname "$0")"',
                 'EP="${ARANGO_ENDPOINT:-tcp://127.0.0.1:8529}"', 'DB="${ARANGO_DB:-kumokg}"', 'USER="${ARANGO_USER:-root}"',
                 'PASS="${ARANGO_PASSWORD:-}"',
                 'COMMON="--server.endpoint $EP --server.database $DB --server.username $USER --server.password $PASS --create-database true"']
        for label in vertices:
            lines.append(f"arangoimport $COMMON --collection {shlex.quote(label)} --create-collection true --type jsonl "
                         f"--on-duplicate replace --file {shlex.quote('vertices/' + label + '.jsonl')}")
        for etype in edges:
            lines.append(f"arangoimport $COMMON --collection {shlex.quote(etype)} --create-collection true --create-collection-type edge "
                         f"--type jsonl --on-duplicate replace --file {shlex.quote('edges/' + etype + '.jsonl')}")
        js = ("var g=require('@arangodb/general-graph'); var d=JSON.parse(require('fs').read('graph_definition.json')); "
              "if(!g._list().includes(d.name)){g._create(d.name,d.edgeDefinitions,d.orphanCollections);}")
        lines.append(f'arangosh --server.endpoint "$EP" --server.database "$DB" --server.username "$USER" --server.password "$PASS" '
                     f"--javascript.execute-string {shlex.quote(js)}")
        (out / "import.sh").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        return f"""# KumoKG -> ArangoDB import

Graph hash `{proj.graph_hash}` · {c['nodes']} vertices · {c['edges']} edges · provenance mode `{proj.provenance_mode}`.

* `vertices/<Label>.jsonl` — one document collection per entity class (`_key` = sanitised KumoKG id, `kumo_id` = original).
* `edges/<TYPE>.jsonl` — one edge collection per relation type (`_from` / `_to` = `<Collection>/<_key>`).
* `graph_definition.json` — named graph `kumokg` edge definitions.

Run `ARANGO_ENDPOINT=tcp://localhost:8529 ARANGO_PASSWORD=... sh import.sh`, or live push with
`KUMOKG_ARANGO_URL`, `KUMOKG_ARANGO_DB`, `KUMOKG_ARANGO_USER`, `KUMOKG_ARANGO_PASSWORD` and `export --target arangodb --push`.
Ids that contained characters ArangoDB forbids in `_key` are listed in `symbol_map.json`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        ids, edges, syntax = [], [], {}
        key_ok = re.compile(r"^[A-Za-z0-9_\-:.@()+,=;$!*'%]{1,254}$")
        collections: dict[str, set[str]] = {}
        for f in sorted((out / "vertices").glob("*.jsonl")):
            keys = set()
            ok, detail = True, "jsonl"
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as exc:
                    ok, detail = False, f"line {i + 1}: {exc}"
                    break
                if not key_ok.match(d.get("_key", "")):
                    ok, detail = False, f"invalid _key {d.get('_key')!r}"
                keys.add(d["_key"])
                ids.append(f"{f.stem}/{d['_key']}")
            collections[f.stem] = keys
            syntax[f"vertices/{f.name}"] = {"ok": ok, "detail": detail}
        for f in sorted((out / "edges").glob("*.jsonl")):
            ok, detail = True, "jsonl"
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as exc:
                    ok, detail = False, f"line {i + 1}: {exc}"
                    break
                edges.append((f"{f.stem}/{d['_key']}", d["_from"], d["_to"]))
            syntax[f"edges/{f.name}"] = {"ok": ok, "detail": detail}
        gd = json.loads((out / "graph_definition.json").read_text(encoding="utf-8"))
        defined = {d["collection"] for d in gd["edgeDefinitions"]}
        edge_files = {f.stem for f in (out / "edges").glob("*.jsonl")}
        refs = {c for d in gd["edgeDefinitions"] for c in d["from"] + d["to"]} | set(gd["orphanCollections"])
        syntax["graph_definition.json"] = {"ok": defined == edge_files and refs <= set(collections), "detail": "edge definitions"}
        ok, detail, _ = validate_balanced((out / "import.sh").read_text(encoding="utf-8"), "shell")
        syntax["import.sh"] = {"ok": ok, "detail": detail}
        return Parsed(ids, edges, syntax)

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        if client is None:
            arango = require_driver("arango", self.extra)
            url, dbname, user = self.require_env(env, "KUMOKG_ARANGO_URL", "KUMOKG_ARANGO_DB", "KUMOKG_ARANGO_USER")
            pw = env.get("KUMOKG_ARANGO_PASSWORD", "")
            cl = arango.ArangoClient(hosts=url)
            sys_db = cl.db("_system", username=user, password=pw)
            if not sys_db.has_database(dbname):
                sys_db.create_database(dbname)
            client = cl.db(dbname, username=user, password=pw)
        vertices, edges, graph_def = self._docs(proj)
        stats = {"imported": 0}
        for name, docs in vertices.items():
            col = client.collection(name) if client.has_collection(name) else client.create_collection(name)
            for chunk in self.batches(docs, 1000):
                col.import_bulk(chunk, on_duplicate="replace")
                stats["imported"] += len(chunk)
        for name, docs in edges.items():
            col = client.collection(name) if client.has_collection(name) else client.create_collection(name, edge=True)
            for chunk in self.batches(docs, 1000):
                col.import_bulk(chunk, on_duplicate="replace")
                stats["imported"] += len(chunk)
        if not client.has_graph(graph_def["name"]):
            client.create_graph(graph_def["name"], edge_definitions=[
                {"edge_collection": d["collection"], "from_vertex_collections": d["from"], "to_vertex_collections": d["to"]}
                for d in graph_def["edgeDefinitions"]], orphan_collections=graph_def["orphanCollections"])
        n = sum(client.collection(c).count() for c in vertices)
        e = sum(client.collection(c).count() for c in edges)
        return {"target": self.name, **stats, "nodes_in_db": n, "edges_in_db": e}

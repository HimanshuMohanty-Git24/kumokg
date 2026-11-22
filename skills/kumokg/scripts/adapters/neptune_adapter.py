"""Amazon Neptune.

* ``lpg`` mode: Neptune Gremlin bulk-load CSVs (``~id``, ``~label``, ``~from``, ``~to``, typed headers such as
  ``name:String``) + ``loader-request.json``.
* ``rdf`` mode: ``data.nq`` + ``data.ttl`` (shared RDF builder) + an N-Quads ``loader-request.json``.
* Live push: S3 upload + Neptune Bulk Loader when ``KUMOKG_NEPTUNE_S3_BUCKET`` is set, otherwise batched
  Gremlin (``lpg``) or SPARQL UPDATE (``rdf``) against ``KUMOKG_NEPTUNE_ENDPOINT``; parity by native counts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.ir import Ontology
from .base import AdapterError, BaseAdapter, Parsed, Projection, columns_for, group_by, read_csv, require_driver, write_csv

NEPTUNE_TYPES = {"int": "Long", "float": "Double", "bool": "Bool", "string": "String"}


class NeptuneAdapter(BaseAdapter):
    name = "neptune"
    title = "Amazon Neptune"
    extra = "neptune"

    @property
    def mode(self) -> str:
        m = self.options.get("neptune_mode", "lpg")
        if m not in ("lpg", "rdf"):
            raise AdapterError("neptune_mode must be lpg or rdf")
        return m

    def expected_counts(self, proj: Projection) -> dict[str, int]:
        if self.mode == "rdf":
            c = proj.counts()
            return {"nodes": c["domain_nodes"], "edges": c["domain_edges"]}
        return super().expected_counts(proj)

    def _loader_request(self, fmt: str) -> dict[str, Any]:
        env = self.env()
        bucket = env.get("KUMOKG_NEPTUNE_S3_BUCKET", "<your-bucket>")
        prefix = env.get("KUMOKG_NEPTUNE_S3_PREFIX", "kumokg/neptune")
        return {"source": f"s3://{bucket}/{prefix}/", "format": fmt,
                "iamRoleArn": env.get("KUMOKG_NEPTUNE_IAM_ROLE_ARN", "arn:aws:iam::<account-id>:role/<NeptuneLoadFromS3>"),
                "region": env.get("KUMOKG_NEPTUNE_REGION", "us-east-1"), "failOnError": "TRUE", "parallelism": "MEDIUM",
                "updateSingleCardinalityProperties": "TRUE", "queueRequest": "TRUE"}

    def write_files(self, proj: Projection, out: Path) -> None:
        if self.mode == "rdf":
            from .rdf_adapter import RDFBuild

            RDFBuild(self.ws.load_graph(), self.ws.load_ontology(True) or Ontology(), proj.provenance_mode).write(out, ("data.ttl", "data.nq"))
            (out / "loader-request.json").write_text(json.dumps(self._loader_request("nquads"), indent=2) + "\n", encoding="utf-8")
            return
        for label, nodes in group_by(proj.nodes, lambda n: n.label).items():
            cols = [(c, t) for c, t in columns_for(nodes, ["name"])]
            header = ["~id", "~label"] + [f"{c}:{NEPTUNE_TYPES[t]}" for c, t in cols]
            write_csv(out / f"vertices_{label}.csv", header, ([n.id, label] + [n.props.get(c) for c, _ in cols] for n in nodes))
        for etype, edges in group_by(proj.edges, lambda e: e.type).items():
            cols = columns_for(edges, ["kumo_id"])
            header = ["~id", "~from", "~to", "~label"] + [f"{c}:{NEPTUNE_TYPES[t]}" for c, t in cols]
            write_csv(out / f"edges_{etype}.csv", header, ([e.id, e.source, e.target, etype] + [e.props.get(c) for c, _ in cols] for e in edges))
        (out / "loader-request.json").write_text(json.dumps(self._loader_request("csv"), indent=2) + "\n", encoding="utf-8")

    def readme(self, proj: Projection) -> str:
        c = proj.counts()
        files = "`data.nq` + `data.ttl` (RDF)" if self.mode == "rdf" else "`vertices_*.csv` + `edges_*.csv` (Gremlin bulk-load CSV)"
        return f"""# KumoKG -> Amazon Neptune ({self.mode})

Graph hash `{proj.graph_hash}` · {c['nodes']} vertices · {c['edges']} edges · provenance mode `{proj.provenance_mode}`.

Files: {files} and `loader-request.json`.

1. `aws s3 cp . s3://<bucket>/kumokg/neptune/ --recursive --exclude "*.md" --exclude "*.json"`
2. `curl -X POST -H 'Content-Type: application/json' https://<cluster-endpoint>:8182/loader -d @loader-request.json`
3. Poll `GET https://<cluster-endpoint>:8182/loader/<loadId>`.

Live push from KumoKG: set `KUMOKG_NEPTUNE_ENDPOINT` (and optionally `KUMOKG_NEPTUNE_S3_BUCKET`,
`KUMOKG_NEPTUNE_IAM_ROLE_ARN`, `KUMOKG_NEPTUNE_REGION` for the bulk loader) then
`uv run --extra neptune ... export --target neptune --neptune-mode {self.mode} --push`.
"""

    def parse_bundle(self, out: Path) -> Parsed:
        req = json.loads((out / "loader-request.json").read_text(encoding="utf-8"))
        req_ok = {"source", "format", "iamRoleArn", "region"} <= set(req)
        if self.mode == "rdf":
            from .rdf_adapter import _ns, parse_rdf_bundle

            parsed = parse_rdf_bundle(out, _ns(self.ws.load_ontology(True) or Ontology()), ("data.ttl", "data.nq"))
            parsed.syntax["loader-request.json"] = {"ok": req_ok and req["format"] == "nquads", "detail": "loader request"}
            return parsed
        ids, edges, syntax = [], [], {}
        typed = re.compile(r"[^:~]+:(String|Long|Double|Bool|Int|Float|Date)(\[\])?$")
        for f in sorted(out.glob("vertices_*.csv")):
            header, rows = read_csv(f)
            ok = header[:2] == ["~id", "~label"] and all(typed.match(h) for h in header[2:])
            syntax[f.name] = {"ok": ok, "detail": "Neptune vertex header"}
            ids.extend(r["~id"] for r in rows)
        for f in sorted(out.glob("edges_*.csv")):
            header, rows = read_csv(f)
            ok = header[:4] == ["~id", "~from", "~to", "~label"] and all(typed.match(h) for h in header[4:])
            syntax[f.name] = {"ok": ok, "detail": "Neptune edge header"}
            edges.extend((r["~id"], r["~from"], r["~to"]) for r in rows)
        syntax["loader-request.json"] = {"ok": req_ok and req["format"] == "csv", "detail": "loader request"}
        return Parsed(ids, edges, syntax)

    def push_live(self, proj: Projection, env: dict[str, str], client: Any = None) -> dict[str, Any]:
        (endpoint,) = self.require_env(env, "KUMOKG_NEPTUNE_ENDPOINT")
        if env.get("KUMOKG_NEPTUNE_S3_BUCKET"):
            return self._bulk_load(env, endpoint, client)
        if self.mode == "rdf":
            return self._sparql_push(proj, endpoint, client)
        return self._gremlin_push(proj, endpoint, client)

    def _bulk_load(self, env: dict[str, str], endpoint: str, client: Any) -> dict[str, Any]:
        import time
        import httpx

        boto3 = require_driver("boto3", self.extra)
        s3 = boto3.client("s3", region_name=env.get("KUMOKG_NEPTUNE_REGION"))
        bucket, prefix = env["KUMOKG_NEPTUNE_S3_BUCKET"], env.get("KUMOKG_NEPTUNE_S3_PREFIX", "kumokg/neptune")
        out = self.out_dir
        uploaded = []
        for p in sorted(out.iterdir()):
            if p.suffix in (".csv", ".nq"):
                s3.upload_file(str(p), bucket, f"{prefix}/{p.name}")
                uploaded.append(p.name)
        http = client or httpx.Client(timeout=60)
        r = http.post(f"{endpoint.rstrip('/')}/loader", json=self._loader_request("nquads" if self.mode == "rdf" else "csv"))
        load_id = r.json()["payload"]["loadId"]
        status = "LOAD_IN_PROGRESS"
        for _ in range(240):
            st = http.get(f"{endpoint.rstrip('/')}/loader/{load_id}").json()["payload"]["overallStatus"]
            status = st["status"]
            if status not in ("LOAD_IN_PROGRESS", "LOAD_NOT_STARTED", "LOAD_IN_QUEUE"):
                break
            time.sleep(2)
        if status != "LOAD_COMPLETED":
            raise AdapterError(f"Neptune bulk load {load_id} ended with {status}")
        counts = self._gremlin_counts(endpoint, None) if self.mode == "lpg" else self._sparql_counts(endpoint, http)
        return {"target": self.name, "method": "bulk_loader", "load_id": load_id, "uploaded": uploaded, **counts}

    def _gremlin_counts(self, endpoint: str, g: Any) -> dict[str, int]:
        if g is None:
            gp = require_driver("gremlin_python.driver.driver_remote_connection", self.extra)
            from gremlin_python.process.anonymous_traversal import traversal

            g = traversal().with_remote(gp.DriverRemoteConnection(endpoint.replace("https", "wss") + "/gremlin", "g"))
        return {"nodes_in_db": g.V().has("kumo_id").count().next(), "edges_in_db": g.E().has("kumo_id").count().next()}

    def _gremlin_push(self, proj: Projection, endpoint: str, g: Any) -> dict[str, Any]:
        from .janusgraph_adapter import gremlin_push

        conn = None
        if g is None:
            gp = require_driver("gremlin_python.driver.driver_remote_connection", self.extra)
            from gremlin_python.process.anonymous_traversal import traversal

            conn = gp.DriverRemoteConnection(endpoint.replace("https", "wss").rstrip("/") + "/gremlin", "g")
            g = traversal().with_remote(conn)
        try:
            stats = gremlin_push(proj, g)
            return {"target": self.name, "method": "gremlin", **stats, **self._gremlin_counts(endpoint, g)}
        finally:
            if conn is not None:
                conn.close()

    def _sparql_counts(self, endpoint: str, http: Any) -> dict[str, int]:
        ont = self.ws.load_ontology(True) or Ontology()
        from .rdf_adapter import _ns

        ns = _ns(ont)

        def q(text: str) -> int:
            r = http.post(f"{endpoint.rstrip('/')}/sparql", data={"query": text}, headers={"Accept": "application/sparql-results+json"})
            return int(r.json()["results"]["bindings"][0]["n"]["value"])

        nodes = q(f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ GRAPH <{ns}graph/entities> {{ ?s a ?c . "
                  f"FILTER(STRSTARTS(STR(?c), \"{ns}ontology/\")) }} }}")
        edges = q(f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?s ?p ?o }} FILTER(STRSTARTS(STR(?g), \"{ns}graph/edge/\")) }}")
        return {"nodes_in_db": nodes, "edges_in_db": edges}

    def _sparql_push(self, proj: Projection, endpoint: str, http: Any) -> dict[str, Any]:
        import httpx

        from .rdf_adapter import RDFBuild

        ds = RDFBuild(self.ws.load_graph(), self.ws.load_ontology(True) or Ontology(), proj.provenance_mode).dataset()
        own = http is None
        http = http or httpx.Client(timeout=120)
        try:
            lines = sorted(ln for ln in ds.serialize(format="nquads").splitlines() if ln.strip())
            by_graph: dict[str, list[str]] = {}
            for ln in lines:
                m = re.match(r"^(.*)\s(<[^>]+>)\s*\.\s*$", ln)
                by_graph.setdefault(m.group(2), []).append(m.group(1) + " .")
            batches = 0
            for gname, triples in sorted(by_graph.items()):
                for i in range(0, len(triples), 500):
                    body = f"INSERT DATA {{ GRAPH {gname} {{\n" + "\n".join(triples[i:i + 500]) + "\n} }"
                    r = http.post(f"{endpoint.rstrip('/')}/sparql", data={"update": body})
                    if r.status_code != 200:
                        raise AdapterError(f"SPARQL UPDATE failed: HTTP {r.status_code} {r.text[:200]}")
                    batches += 1
            return {"target": self.name, "method": "sparql_update", "update_batches": batches, **self._sparql_counts(endpoint, http)}
        finally:
            if own:
                http.close()

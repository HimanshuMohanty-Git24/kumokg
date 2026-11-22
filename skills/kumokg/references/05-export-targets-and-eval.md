# 05 · Database export targets, parity verification and evaluation

## Commands

```
$KG adapters
$KG export --workspace <ws> --target <name|all> [--provenance-mode props|nodes|reified] [--pg-mode plain|age|pgvector] [--neptune-mode lpg|rdf] [--push]
$KG verify-export --workspace <ws> --target <name|all>
```
Bundles land in `<ws>/.kumokg/exports/<target>/` with `README_IMPORT.md`, `symbol_map.json` (every identifier rename) and
`export_manifest.json` (SHA-256 per file, expected counts, graph hash, options).

## Top-10 matrix

| target | offline files | import | `--push` |
|---|---|---|---|
| neo4j | `nodes_*.csv`, `edges_*.csv` (neo4j-admin typed headers), `schema.cypher`, `import.cypher`, `neo4j-admin-import.sh` | `cypher-shell -f schema.cypher && cypher-shell -f import.cypher` | Bolt `UNWIND ... MERGE` |
| postgres | `schema.sql`, `data/*.csv`, `load.sql`; `age_load.sql` (age); pgvector column stub (pgvector) | `psql -f schema.sql && psql -f load.sql` | psycopg `COPY` |
| memgraph | `nodes_*.csv`, `edges_*.csv`, `schema.cypherl`, `load.cypherl` | `mgconsole < schema.cypherl; mgconsole < load.cypherl` | Bolt |
| falkordb | enforce-schema `nodes_*.csv` / `edges_*.csv`, `schema.cypher`, `load_bulk.sh` | `sh load_bulk.sh` | falkordb client |
| neptune | lpg: `vertices_*.csv` / `edges_*.csv` (`~id ~label ~from ~to`, `name:String`); rdf: `data.nq` + `data.ttl`; `loader-request.json` | S3 + `POST /loader` | bulk loader, Gremlin or SPARQL UPDATE |
| arangodb | `vertices/*.jsonl` (`_key`), `edges/*.jsonl` (`_from`/`_to`), `graph_definition.json`, `import.sh` | `sh import.sh` | python-arango |
| tigergraph | `schema.gsql`, `loading_job.gsql`, typed `vertices_*.csv` / `edges_<TYPE>__<From>__<To>.csv` | `gsql schema.gsql && gsql loading_job.gsql` | pyTigerGraph upserts |
| janusgraph | `schema.groovy`, `graph.graphson` (GraphSON 3.0), `load.groovy` | `:load schema.groovy` / `:load load.groovy` | gremlinpython |
| rdf | `ontology.ttl` (OWL 2), `shapes.shacl.ttl`, `data.ttl`, `data.nq` (named graph per edge), `data.jsonld` | any store's bulk loader | SPARQL 1.1 Graph Store PUT |
| duckdb | `nodes/edges/evidence.parquet`, `setup_duckpgq.sql`, `graph.graphml`, `kumokg.duckdb` | `duckdb kumokg.duckdb` | ATTACH file or `md:` |

## Provenance modes (labelled-property-graph targets)

- `props` (default): `evidence_count`, `evidence_json`, `primary_quote`, `primary_source` on nodes and edges.
- `nodes`: evidence becomes `KumoEvidence` nodes linked by `KUMO_HAS_EVIDENCE`; edges carry `evidence_ids`.
- `reified`: additionally one `KumoStatement` node per edge with `KUMO_SUBJECT` / `KUMO_OBJECT` / `KUMO_SUPPORTED_BY`.
Relational and RDF targets always keep evidence normalised (`kg_evidence`, `evidence.parquet`, `prov:wasDerivedFrom`).

Bi-temporal history is exported everywhere: superseded edges keep `status = superseded`, `valid_to`, `superseded_by`
(RDF: only current facts are plain triples; every edge is an `rdf:Statement`).

## Parity verifier

`verify-export` re-parses the files (CSV, JSONL, SQL, Cypher, GSQL, Groovy, GraphSON, Turtle / N-Quads / JSON-LD via
rdflib, Parquet / DuckDB via duckdb, GraphML via networkx) and fails unless: node and edge counts equal the manifest and
a fresh projection of `graph.json`, zero dangling endpoints, valid syntax, checksums intact. Report `passed: 10` to the
user only when the command says so.

## Live push

Credentials come from `<ws>/.kumokg/.env` (template: `assets/templates/env.example`); drivers are optional extras:
`uv run --project <skill_dir> --extra neo4j kumokg export --target neo4j --push --workspace <ws>`
(`--extra all-db` installs every driver). Each push ends with a native count query and returns `nodes_in_db` /
`edges_in_db` vs expected. A missing driver exits 2 with the exact `--extra` hint.

## Evaluation

`$KG eval --workspace <ws> --samples 20 [--k 10] [--min-entity-recall 0.5]` samples verified 2-hop and 3-hop paths into
`.kumokg/eval_suite.json`, asks each question in `local` and `ppr` mode and reports Entity Recall@K, Edge Recall@2K,
Answer Hit@K, citation grounding % and latency. It passes when the best mode reaches the recall threshold and every
returned citation re-verifies (100%).

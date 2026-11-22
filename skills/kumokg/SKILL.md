---
name: kumokg
description: KumoKG builds verified, quote-grounded knowledge graphs from any corpus (documents, books, notes, CSV/XLSX, JSON/YAML, notebooks, Obsidian vaults, images, scanned PDFs, web topics), answers GraphRAG questions with citations, applies time-aware chat corrections and exports to Neo4j, Postgres, RDF, DuckDB and six more databases. Use it when a user wants to map, connect, extract entities and relations from, chat with, or export their data as a knowledge graph.
license: PolyForm-Noncommercial-1.0.0
compatibility: Requires Python 3.11+ and uv. Works across Claude Code, Cursor, OpenCode, and Codex.
---

# KumoKG

Every step is a CLI call that prints compact JSON. Define once:

```
KG="uv run --project <skill_dir> kumokg"     # <skill_dir> = folder of this SKILL.md
```
No uv? `python -m venv .venv && .venv/bin/pip install -e <skill_dir>` then `KG=".venv/bin/kumokg"`.
Always pass `--workspace <ws>`; state lives in `<ws>/.kumokg/`.

## Hard rules

1. NEVER read raw sources, PDFs, `chunks.jsonl`, `candidates.jsonl` or `graph.json` into context.
   Use `status`, `sample`, `verify`, `doctor`, `query` summaries instead.
2. Every claim needs a verbatim quote, table cell, JSON pointer, notebook cell or image region.
   The verifier quarantines anything ungrounded; never re-submit quarantined claims.
3. Load a reference file only when you enter its phase (zero cost until then).
4. Report numbers from CLI output only; do not estimate counts yourself.

## 0. Resume first

`$KG status --workspace <ws>`
- `graph.nodes > 0` -> go to **Use the graph**.
- otherwise continue at the phase named in `next`.

## 1. New workspace: ask once

Ask ONE concise question with only the missing choices:
- **HITL** (user approves ontology at Gate 1 and edges at Gate 2 in the browser) or **Autonomous**?
- Only if the corpus is large (> ~200 chunks after `chunk`): **Harness** (you extract, zero setup) or
  **BYOK** (`.kumokg/.env`, see `assets/templates/env.example`)?

## 2. Audit, chunk, sample  -> `references/01-source-audit-and-modalities.md`

```
$KG audit <dirs|files|urls> --workspace <ws> --mode hitl|autonomous --tier harness|byok
$KG chunk --workspace <ws>
$KG sample --workspace <ws> --n 12
```
Scanned pages appear under `needs_ocr_or_vlm` with four remediation options; excluded files carry reasons.

## 3. Ontology + Gate 1  -> `references/02-ontology-and-gate1.md`

Draft classes/relations from the sample (write a small JSON), then:
```
$KG propose-ontology --workspace <ws> --from draft.json
$KG studio --workspace <ws> --gate gate1 --blocking      # HITL
$KG approve-ontology --workspace <ws>                     # Autonomous
$KG compile-structured --workspace <ws>                   # zero-token tables/JSON/notebooks/vaults
```

## 4. Extraction + verification  -> `references/03-extraction-vlm-and-verify.md`

- Harness: `$KG media-prep`, `$KG export-batches`, answer each batch file exactly as its
  `instructions` say, then `$KG import-candidates <out.jsonl>`. Repeat until no batches remain.
- BYOK: `$KG extract --extractor byok --kind all`. Offline baseline: `--extractor heuristic`.
- Then `$KG verify --workspace <ws>` (grounding, ontology, entity resolution, contradictions).

## 5. Gate 2 + commit  -> `references/04-gate2-chat-and-mcp.md`

```
$KG studio --workspace <ws> --gate gate2 --blocking      # HITL: user commits in the browser
$KG decide --auto --workspace <ws> && $KG commit --workspace <ws>   # Autonomous
$KG doctor --workspace <ws>
```

### Waiting on a gate (quiet polling)
`--blocking` returns only after the user clicks **Approve & Continue** (Gate 1) or **Commit** (Gate 2).
Run it in the background, give the user the printed URL once, then poll quietly every ~20 s with
`$KG status --workspace <ws>` until `gates.gate1 == "approved"` / `gates.gate2 == "committed"`.
Do not narrate each poll.

## Use the graph  -> `references/04-gate2-chat-and-mcp.md`

```
$KG query "<question>" --workspace <ws> --mode local|ppr|lazy-global [--as-of 2024-01-01]
$KG patch supersede-edge --edge-id <id> --target <node> --valid-from <date> --quote "<verbatim>" --workspace <ws>
$KG studio --workspace <ws> --no-block --port 8765       # live Explorer (SSE refresh on patch)
$KG serve-mcp --workspace <ws>                           # needs: uv run --extra mcp ...
```
Answer from `context` and cite its `[source p.N]` markers. Corrections always go through `patch`
with a quote; history is preserved (`valid_to`, `superseded_by`).

## Export + eval  -> `references/05-export-targets-and-eval.md`

```
$KG export --target neo4j|postgres|memgraph|falkordb|neptune|arangodb|tigergraph|janusgraph|rdf|duckdb|all --workspace <ws>
$KG verify-export --target all --workspace <ws>
$KG eval --samples 20 --workspace <ws>
```
Live push: add `--push` (credentials in `.kumokg/.env`, driver via `uv run --extra <target>`).

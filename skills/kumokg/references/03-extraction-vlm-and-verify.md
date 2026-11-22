# 03 · Extraction (harness / BYOK / heuristic), visual media and verification

Load this file after Gate 1 is approved.

## Tier 1: you are the extractor (zero config)

```
$KG media-prep --workspace <ws>            # scanned pages -> PNG (150 DPI), images, notebook charts -> vlm_batches.jsonl
$KG export-batches --workspace <ws>        # 6 chunks (5-8) or 3 images (2-4) per task file
```
Read `.kumokg/batches/index.json`, then open ONE batch file at a time. Each contains `instructions`, `ontology`,
`calibration`, `output_schema`, `items` and `output_file`. Write JSONL to `output_file`, one claim per line:

```json
{"chunk_id": "alice-7e23::c0004", "kind": "edge", "subject": {"name": "Alice", "label": "Character"},
 "predicate": "ENCOUNTERS", "object": {"name": "White Rabbit", "label": "Character"},
 "quote": "when suddenly a White Rabbit with pink eyes ran close by her", "confidence": 0.85}
{"chunk_id": "alice-7e23::c0004", "kind": "node", "subject": {"name": "White Rabbit", "label": "Character"},
 "aliases": ["Rabbit"], "quote": "a White Rabbit with pink eyes", "confidence": 0.95}
{"chunk_id": "...", "kind": "attribute", "subject": {"name": "Alice", "label": "Character"}, "predicate": "age",
 "value": 7, "quote": "seven and a half exactly"}
{"image_item_id": "scan-1a2b:p1", "kind": "edge", "subject": "Initech Labs", "predicate": "SUPPLIES",
 "object": "Acme Robotics", "element_description": "memo line 2: supply agreement", "bbox_norm": [0.1, 0.2, 0.9, 0.3]}
```

Extraction prompt you follow for every text chunk:
> Extract only facts the chunk states. Copy `quote` character-for-character (<= 300 chars) from that chunk. Use ontology
> labels/predicates; if a fact does not fit, still emit it with your best UPPER_SNAKE predicate (it goes to review).
> Use canonical names from `ontology.aliases`. Skip anything in `calibration.negative_examples`. Emit nothing rather than
> guessing.

Then `$KG import-candidates --workspace <ws> <output_file>` (repeatable; duplicates ignored; the batch's chunks are
marked processed by `chunk_sha256`). `chunk_id: "auto"` locates the chunk from the quote. Re-run `export-batches` until
it reports 0 batches.

## Tier 2: BYOK pool

Copy `assets/templates/env.example` to `<ws>/.kumokg/.env`, set `KUMOKG_LLM_PRESET` (groq | openrouter | openai | ollama)
and `KUMOKG_LLM_API_KEY` (not needed for local Ollama). Then:
```
$KG extract --workspace <ws> --extractor byok --kind text|image|all [--concurrency 4] [--limit N]
```
Bounded `asyncio.Semaphore` concurrency, JSON-mode requests, schema validation with corrective retry, exponential
backoff on 429/5xx, and resume: processed `chunk_sha256` values are skipped on the next run.

## Offline baseline

`$KG extract --workspace <ws> --extractor heuristic` uses seed terms, aliases, structured names and cue phrases. It is
deterministic and good for smoke tests or bootstrapping; LLM claims are richer.

## Verification (`$KG verify --workspace <ws>`)

| evidence | grounding check |
|---|---|
| text / web / notebook | exact span, then whitespace/case/punctuation-normalised substring (`verified`), fuzzy >= 0.92 (`soft_verified`), else `rejected` |
| table | re-opens CSV/XLSX/Parquet/SQLite, compares `(row_index, column_name)` to `cell_value`, checks file hash |
| json | resolves the RFC 6901 pointer in JSON/YAML/XML (or vault frontmatter) and compares `raw_value` |
| image | file exists inside `.kumokg/media/` and SHA-256 matches -> `needs_visual` (human confirms at Gate 2) |

Then: ontology check (closed world -> `proposed_schema_extensions`), deterministic entity resolution (normalised names,
alias table, token overlap; genuinely ambiguous names are reported, not merged), grouping into proposals
(`source, predicate, target, valid_from`), contradiction detection on functional predicates/properties across sources,
and buckets: `conflict`, `review` (soft grounding, low confidence, schema extension, image, ambiguous), `auto`, `quarantine`.

Read only the compact summary the command prints. If `quarantine` is large, your quotes are not verbatim: fix the
prompt, not the verifier.

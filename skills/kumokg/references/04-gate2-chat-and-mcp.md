# 04 · Gate 2 review, commit, GraphRAG chat, patches and MCP

## Gate 2 (HITL)

`$KG studio --workspace <ws> --gate gate2 --blocking` then tell the user once: "Review edges at <url>/gate2 and press
**Commit**." Tabs: *Conflicts & contradictions*, *Needs review*, *Auto-verified* (one-click **Bulk approve filtered**),
*Quarantined* (read-only). The Evidence Inspector shows highlighted quotes, table cell grids, JSON pointer paths and image
thumbnails with bounding boxes. Keys: `A` approve, `R` reject, `E` edit/flip, `J`/`K` next/prev, `Enter` commit.
Poll `status` quietly until `gates.gate2 == "committed"`.

## Autonomous / CLI decisions

```
$KG decide --workspace <ws> --auto                      # approve every auto-bucket proposal
$KG decide --workspace <ws> --file decisions.json       # [{"proposal_id": "p-...", "decision": "approve", "edit": {"predicate": "BUILT_BY", "flip": false}}]
$KG commit --workspace <ws>
```
Rejections become calibration negative examples; predicate edits, flips and renamed endpoints become correction and
alias rules that the next `verify`, heuristic run and exported batches apply automatically. Commit never deletes and
never writes quarantined proposals.

## Asking the graph

| mode | use when |
|---|---|
| `local` | entity lookups, "what does X do", 1-2 hop neighbourhoods (BM25 over names, aliases, predicates, quotes) |
| `ppr` | multi-hop "how is X connected to Y" questions; returns `paths` + the subgraph with provenance |
| `lazy-global` | themes/overviews ("what are the main groups?"): ranked community dossiers, no LLM at index time |

```
$KG query "How is Alice connected to the Queen of Hearts?" --workspace <ws> --mode ppr --hops 3 --max-tokens 1500
```
Answer template: state the answer, then each supporting fact with its quote and the `[source p.N]` citation from
`context`. If `context` lacks the fact, say the graph does not contain it; never fill gaps from memory.
Use `--as-of YYYY-MM-DD` (or an ISO datetime) for "as of" / historical questions.

## Patches (chat corrections)

Every op is verified (quote must exist in a chunk) and atomic; the changelog keeps before/after; a running Studio
refreshes over SSE.

```json
{"message": "user correction", "ops": [
  {"op": "add-node", "label": "Character", "name": "Bill the Lizard", "quote": "Bill's got the other"},
  {"op": "add-alias", "node_id": "character:white-rabbit", "alias": "the Rabbit"},
  {"op": "add-edge", "source": "character:alice", "type": "ENCOUNTERS", "target": {"name": "Bill the Lizard"},
   "quote": "...", "chunk_id": "auto", "valid_from": null},
  {"op": "supersede-edge", "edge_id": "e-...", "valid_from": "1862-07-04T14:30", "new": {"target": "size:nine-feet"},
   "quote": "she was now more than nine feet high"},
  {"op": "retract-edge", "edge_id": "e-...", "valid_to": "2024-01-01", "reason": "source withdrawn"},
  {"op": "merge-nodes", "keep": "character:hatter", "merge": ["character:mad-hatter"]}]}
```
`$KG patch --file patch.json --workspace <ws>` (or flags: `patch supersede-edge --edge-id ... --target ... --valid-from ...
--quote "..."`). Closed-world violations fail unless `"allow_extension": true`. Facts without a source need
`"allow_unverified": true`; `doctor` will then report them as ungrounded. `$KG history --id <edge_id>` shows the audit trail.

## Live Explorer

`$KG studio --workspace <ws> --no-block --port 8765` -> `/explorer`: force-directed canvas (pan, zoom, drag, neighbourhood
highlight, label/community colours), search with PPR path highlighting, time-travel slider over `valid_from`/`valid_to`
breakpoints, provenance drawer, live SSE badge, health bar and one-click export of all 10 targets.

## MCP server

```
uv run --project <skill_dir> --extra mcp kumokg serve-mcp --workspace <ws>
```
Tools: `kg_query`, `kg_ppr_multihop`, `kg_get_evidence`, `kg_time_travel`, `kg_patch`, `kg_doctor`, `kg_export`.
Register it in the harness MCP config with that command; without the extra the CLI exits 2 with an install hint.

## Doctor

`$KG doctor --workspace <ws>` -> grounding %, schema compliance %, contradictions (graph and pending), orphans, dangling
edges, coverage per source, communities, review queue, a 0-100 score and (without `--fast`) a source-integrity check that
re-derives every chunk from the original files.

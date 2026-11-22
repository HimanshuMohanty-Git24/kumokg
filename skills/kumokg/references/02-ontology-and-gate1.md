# 02 · Ontology design and Gate 1

Load this file after `sample` and before any extraction.

## Draft template (write this JSON, then `propose-ontology --from draft.json`)

```json
{
  "namespace": "https://example.org/kg/",
  "prefix": "ex",
  "closed_world": true,
  "entity_classes": [
    {"name": "Character", "description": "A person or creature", "seed_terms": ["Alice", "White Rabbit"],
     "properties": {"age": {"type": "int", "functional": true}}}
  ],
  "relations": [
    {"source_class": "Character", "predicate": "ENCOUNTERS", "target_class": "Character",
     "functional": false, "symmetric": false, "cue_phrases": ["met", "saw"], "inverse_cue_phrases": ["was met by"]}
  ],
  "alias_rules": [{"canonical": "White Rabbit", "aliases": ["Rabbit"], "entity_class": "Character"}]
}
```

Rules for a good draft:
- 4-12 classes in PascalCase, predicates in UPPER_SNAKE_CASE, each with a one-line description.
- `seed_terms`: names you saw in `sample` / `discovered_terms`; they produce the grounded examples shown at Gate 1 and
  feed the offline heuristic extractor.
- `functional: true` only when one source can have one target at a time (HEADQUARTERED_IN, CAPITAL_OF). Functional
  predicates drive contradiction detection; with `valid_from`/`valid_to` they become time-aware.
- `cue_phrases` are lowercase verbs/phrases that appear between the two mentions ("works at"); `inverse_cue_phrases`
  appear when the target is mentioned first ("led by").
- Alias rules map short forms to canonical names before entity resolution.

`propose-ontology` merges your draft with structured-source mapping proposals, attaches up to 3 verbatim grounded
examples per class and lists `discovered_terms`. Structured mappings look like:

```json
{"mapping_id": "<doc_id>:employees", "kind": "table", "table": "employees", "entity_class": "Employee",
 "id_field": "employee_id", "name_field": "name", "property_fields": {"title": "string"}, "prose_fields": ["bio"],
 "relations": [{"field": "company_id", "predicate": "HAS_COMPANY", "target_class": "Company", "target_match": "id",
                "valid_from_field": "start_date"}]}
```

## CLI edits (autonomous mode or quick fixes)

```
$KG ontology-edit --workspace <ws> --rename-class Employee=Person --rename-predicate HAS_COMPANY=WORKS_AT [--open-world]
$KG approve-ontology --workspace <ws> [--file edited.json]
```
Renames update relations, mappings and alias rules consistently (merging into an existing class if needed).

## Gate 1 in the browser (HITL)

`$KG studio --workspace <ws> --gate gate1 --blocking` prints `{"studio": "http://127.0.0.1:<port>", ...}` and waits.
Tell the user, once: "Review the ontology at <url>/gate1 and click **Approve & Continue**." The page lets them edit classes,
typed properties, seed terms, the relation grammar (functional / symmetric / cue phrases), structured column-to-graph
mappings with a 3-row preview, alias rules, add discovered terms as seeds and toggle closed world.
Then poll `status` quietly until `gates.gate1 == "approved"`.

## Closed vs open world

- Closed (default): labels or triples outside the grammar never enter the graph; they surface in
  `proposed_schema_extensions` and Gate 2 "Needs review". Approving one at Gate 2 extends `ontology.approved.json`.
- Open: extensions still go to review, but approval does not require an ontology change.

## Edge cases

- Validation fails if a relation references an unknown class: fix the class name, do not delete the relation silently.
- Generic structured class names (e.g. `Employee` vs text-derived `Person`) should be merged so text and table facts
  resolve to the same nodes.
- After approval, always run `compile-structured` before extraction so text mentions link to structured entities.

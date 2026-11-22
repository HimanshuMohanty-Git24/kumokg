# 01 · Source audit, modalities, chunking and sampling

Load this file when a workspace has no `audit-report.json` yet, or when the user adds sources.

## Commands

```
$KG audit <inputs...> --workspace <ws> [--mode hitl|autonomous] [--tier harness|byok] [--dedup flag|skip] [--skip <doc_id|file>] [--full]
$KG chunk  --workspace <ws> [--size 1500] [--overlap 200]
$KG sample --workspace <ws> [--n 12] [--preview 600]
```

`<inputs>` may mix directories (recursive; dot-folders and `.kumokg` ignored), single files, `http(s)://` URLs and
URL manifests (`*.urls`, one URL per line, `#` comments allowed). Re-running `audit` replaces the inventory.

## The six modalities

| modality | extensions | what audit records | how it becomes graph data |
|---|---|---|---|
| structured | csv tsv xlsx xlsm parquet sqlite db | tables, row counts, column profiles, candidate PK, FK links (value-subset or declared), prose columns (avg > 120 chars) | zero-token compile (`TableEvidence`); prose cells are chunked |
| semi_structured | json jsonl yaml yml xml | object-array paths (`/projects/*/milestones/*`), id keys, name key, prose fields | zero-token compile (`JsonEvidence`, RFC 6901 pointers) |
| notebook_vault | ipynb md | cells by type, embedded PNG outputs (decoded to `.kumokg/media/`), frontmatter keys, headings, `[[wikilinks]]`, `key:: [[value]]` fields | notebook imports + vault links compiled; markdown cells chunked |
| unstructured | pdf docx pptx epub html txt rst | pages, chars/page, image coverage/page, metadata | chunked (`TextEvidence`) |
| image | png jpg jpeg webp svg gif bmp | size, normalised 1600px PNG, 256-bit dHash | VLM batch items (`ImageEvidence`) |
| web | URLs | status, readable text, `fetched_at`, content hash | chunked (`WebEvidence`) |

## PDF triage (exact rules)

- Per page: extractable characters (`page.get_text`) and image coverage (union of image bboxes / page area).
- `needs_ocr_or_vlm` when the **median** chars/page < 80 **and** (median coverage >= 0.5 or at least half the pages are
  image/vector pages). The report lists the exact page numbers plus four remediation options:
  `harness_vlm` (render + you read the PNGs), `byok_vlm` (vision endpoint), `local_ocr` (Tesseract via PyMuPDF), `skip`.
- Mixed PDFs stay `usable`; their image-only pages are listed with `"partial": true`.
- Excluded with explicit reasons: empty file, corrupt/unrecoverable structure, encrypted (password), zero pages,
  no text and no images.

Tell the user which remediation you recommend: Harness VLM for < 30 pages, BYOK VLM for more, local OCR only when
`tesseract` is installed (`media-prep --strategy local_ocr`, then re-run `chunk`).

## Deduplication

- Identical SHA-256 or word-shingle Jaccard >= 0.98: the lower-fidelity copy is excluded (PDF with page anchors wins).
- Jaccard >= 0.5: flagged `near_duplicate_of` and kept as corroborating evidence (`--dedup skip` excludes it to save tokens).
- Images within 10/256 dHash bits are excluded as near-identical frames.

## Web sources

401/403 -> `access denied`; 402/451 -> `paywall or legal block`; teaser text with subscribe markers -> `paywall detected`;
< 200 readable chars -> `empty or script-rendered page`. Never try to bypass these; report them to the user.

## Chunking

Deterministic, page-bounded spans (paragraph > sentence > line > word boundaries), default 1500 chars with 200 overlap.
Each chunk has `chunk_id = <doc_id>::cNNNN`, `page_no`, `char_start/char_end` (offsets in the cleaned page text),
`sha256`. Repeated per-page boilerplate (site banners, bare page numbers) is stripped before chunking.

## Reading the sample (ontology discovery without reading the corpus)

`sample` returns beginning / middle / end chunks per document (largest first) and the highest term-diversity chunks per
modality, plus `discovered_terms` (frequent capitalised phrases with counts). Use only this output to draft the ontology.
If a class is obviously missing, run `sample --n 20` again rather than opening files.

## Edge cases

- Huge spreadsheets: audit profiles every row; prefer CSV/Parquet exports over XLSX for > 200k rows.
- Encodings: text is decoded as UTF-8 with replacement; mention mojibake to the user instead of guessing.
- Obsidian notes without frontmatter or links are treated as prose.
- Never modify the user's source folders; KumoKG only writes inside `<ws>/.kumokg/`.

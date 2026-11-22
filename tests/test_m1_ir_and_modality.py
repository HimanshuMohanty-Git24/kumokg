"""Milestone 1: Kumo-IR, universal modality router, chunker/sampler and zero-token structured compiler."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from conftest import DATA_DIR
from core import modality_router as mr
from core import structured_compiler as sc
from core.ir import (
    Candidate, Edge, EntityRef, Graph, GraphStore, Node, Ontology, TableEvidence, TextEvidence, Workspace,
    atomic_write_text, fts_query, make_edge_id, make_node_id, normalize_name, normalize_text, parse_evidence,
    read_json, sha256_file, sha256_text, to_class_name, to_predicate,
)

# ---------------------------------------------------------------------------------------------
# IR
# ---------------------------------------------------------------------------------------------


def test_evidence_union_discriminates_all_six_kinds():
    samples = [
        {"kind": "text", "doc_id": "d", "chunk_id": "d::c0000", "verbatim_quote": "q", "chunk_sha256": "x"},
        {"kind": "table", "doc_id": "d", "sheet_or_table": "t", "row_index": 0, "column_name": "c", "cell_value": 1},
        {"kind": "json", "doc_id": "d", "json_pointer": "/a/0/name", "raw_value": "A"},
        {"kind": "notebook", "doc_id": "d", "cell_index": 2, "cell_type": "code", "line_span": [1, 1], "verbatim_quote": "import x"},
        {"kind": "image", "doc_id": "d", "image_path": "m.png", "element_description": "box", "bbox_norm": [0, 0, 0.5, 0.5]},
        {"kind": "web", "doc_id": "d", "url": "https://x", "fetched_at": "2024-01-01", "verbatim_quote": "q"},
    ]
    kinds = [type(parse_evidence(s)).__name__ for s in samples]
    assert kinds == ["TextEvidence", "TableEvidence", "JsonEvidence", "NotebookEvidence", "ImageEvidence", "WebEvidence"]
    with pytest.raises(ValidationError):
        parse_evidence({"kind": "json", "doc_id": "d", "json_pointer": "a/b", "raw_value": 1})
    with pytest.raises(ValidationError):
        parse_evidence({"kind": "image", "doc_id": "d", "image_path": "m", "element_description": "x", "bbox_norm": [0, 0, 2, 1]})
    with pytest.raises(ValidationError):
        parse_evidence({"kind": "sound", "doc_id": "d"})


def test_deterministic_ids_and_normalisation():
    assert make_node_id("Character", "White Rabbit") == "character:white-rabbit"
    assert make_node_id("Character", "White Rabbit") == make_node_id("Character", "White  Rabbit")
    assert make_edge_id("a", "KNOWS", "b") == make_edge_id("a", "KNOWS", "b")
    assert make_edge_id("a", "KNOWS", "b", "2020") != make_edge_id("a", "KNOWS", "b")
    assert to_predicate("works at") == "WORKS_AT" and to_predicate("headquarteredIn") == "HEADQUARTERED_IN"
    assert to_class_name("drink me bottle") == "DrinkMeBottle"
    assert normalize_name("The Queen's") == normalize_name("queen")
    assert normalize_text("`Oh  dear!’\n said") == "'oh dear!' said"


def test_edge_validation_and_bitemporal_activity():
    with pytest.raises(ValidationError):
        Edge(id="e", source_id="a", target_id="b", type="worksAt")
    e = Edge(id="e", source_id="a", target_id="b", type="WORKS_AT", valid_from="2019-03-01", valid_to="2023-06-01",
             status="superseded", superseded_by="e2")
    assert e.active_at("2020-01-01") and not e.active_at("2023-06-01") and not e.active_at("2018-01-01")
    assert not e.active_at(None)
    cur = Edge(id="e2", source_id="a", target_id="c", type="WORKS_AT", valid_from="2023-06-01")
    assert cur.active_at(None) and cur.active_at("2024-01-01") and not cur.active_at("2020-01-01")
    pending = Edge(id="e3", source_id="a", target_id="c", type="WORKS_AT", status="pending")
    assert not pending.active_at("2024-01-01")


def test_graph_store_fts5_rebuild_and_search(tmp_path):
    ev = TextEvidence(doc_id="d", chunk_id="d::c0", verbatim_quote="the White Rabbit took a watch out of its waistcoat-pocket")
    g = Graph(nodes=[Node(id="character:white-rabbit", labels=["Character"], canonical_name="White Rabbit",
                          aliases=["Rabbit"], evidence=[ev]),
                     Node(id="object:watch", labels=["Object"], canonical_name="Pocket Watch")],
              edges=[Edge(id="e1", source_id="character:white-rabbit", target_id="object:watch", type="CARRIES",
                          evidence=[ev])])
    store = GraphStore(tmp_path / "g.sqlite")
    counts = store.rebuild(g)
    assert counts == {"nodes": 2, "edges": 1, "evidence": 2}
    assert store.search_nodes("rabbit")[0][0] == "character:white-rabbit"
    assert store.search_edges("carries watch")[0][0] == "e1"
    assert store.search_evidence("waistcoat pocket")
    store.rebuild(g)  # transactional re-run stays consistent
    assert store.counts()["nodes"] == 2
    assert fts_query('what "is" the; rabbit?') == '"rabbit"'
    assert g.content_hash() == Graph.model_validate(json.loads(g.model_dump_json())).content_hash()


def test_atomic_write_and_candidate_ids(tmp_path):
    p = tmp_path / "x" / "f.json"
    atomic_write_text(p, '{"a": 1}')
    assert read_json(p) == {"a": 1}
    c1 = Candidate(kind="edge", subject=EntityRef(name="Alice", label="character"), predicate="meets",
                   object=EntityRef(name="White Rabbit", label="Character"),
                   evidence=[TableEvidence(doc_id="d", sheet_or_table="t", row_index=0, column_name="c", cell_value="x")]).finalize()
    c2 = Candidate(kind="edge", subject=EntityRef(name="alice", label="Character"), predicate="MEETS",
                   object=EntityRef(name="the White Rabbit", label="Character"),
                   evidence=[TableEvidence(doc_id="d", sheet_or_table="t", row_index=0, column_name="c", cell_value="x")]).finalize()
    assert c1.predicate == "MEETS" and c1.subject.label == "Character"
    assert c1.candidate_id == c2.candidate_id


def test_ontology_grammar_helpers():
    ont = Ontology()
    ont.ensure_class("Person")
    ont.ensure_class("Company")
    ont.ensure_relation("Person", "works at", "Company", functional=True)
    assert ont.allows("Person", "WORKS_AT", "Company") and not ont.allows("Company", "WORKS_AT", "Person")
    assert ont.is_functional("WORKS_AT")
    ont.relations[0].symmetric = True
    assert ont.allows("Company", "WORKS_AT", "Person")


# ---------------------------------------------------------------------------------------------
# Modality router
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audited(tmp_path_factory, synthetic_dir):
    ws = Workspace(tmp_path_factory.mktemp("audit_ws"))
    report = mr.audit(ws, [str(synthetic_dir)])
    return ws, report


def _entry(report, name):
    return next(f for f in report["files"] if f["name"] == name)


def test_audit_detects_all_six_modalities(audited):
    ws, report = audited
    mb = report["modality_breakdown"]
    assert mb["structured"] == 2 and mb["semi_structured"] == 2 and mb["image"] == 2
    assert mb["notebook_vault"] == 4 and mb["unstructured"] >= 7
    assert ws.audit_path.exists() and ws.manifest_path.exists()
    assert ws.load_manifest().phase == "audited"


def test_scanned_pdf_flagged_with_pages_and_remediations(audited):
    _, report = audited
    entry = _entry(report, "scanned_memo.pdf")
    assert entry["status"] == "needs_ocr_or_vlm"
    assert entry["details"]["median_chars_per_page"] < mr.PDF_MIN_CHARS_PER_PAGE
    assert entry["details"]["median_image_coverage"] >= 0.9
    flagged = next(n for n in report["needs_ocr_or_vlm"] if n["doc_id"] == entry["doc_id"])
    assert flagged["pages"] == [1, 2]
    assert [o["id"] for o in flagged["remediation_options"]] == ["harness_vlm", "byok_vlm", "local_ocr", "skip"]
    clean = _entry(report, "report.pdf")
    assert clean["status"] == "usable" and clean["details"]["page_count"] == 3
    assert clean["details"]["median_chars_per_page"] > 150


def test_corrupt_and_empty_pdfs_excluded_with_reasons(audited):
    _, report = audited
    reasons = {Path(x["path"]).name: x["reason"] for x in report["excluded_sources"]}
    assert reasons["empty.pdf"] == "empty file (0 bytes)"
    assert reasons["corrupt.pdf"].startswith("corrupt or unreadable PDF")


def test_image_normalisation_and_perceptual_dedup(audited):
    ws, report = audited
    orig = _entry(report, "diagram.png")
    copy = _entry(report, "diagram_copy.png")
    assert orig["status"] == "usable" and copy["status"] == "duplicate"
    assert (ws.media_dir / orig["details"]["media_path"]).exists()
    assert max(orig["details"]["normalized_size"]) <= mr.IMAGE_MAX_DIM
    dup = next(d for d in report["duplicates"] if d["doc_id"] == copy["doc_id"])
    assert dup["duplicate_of"] == orig["doc_id"] and dup["method"].startswith("dHash")


def test_notebook_media_extraction_and_vault_detection(audited):
    ws, report = audited
    nb = _entry(report, "analysis.ipynb")
    assert nb["details"]["markdown_cells"] == 2 and nb["details"]["code_cells"] == 2
    media = nb["details"]["embedded_images"]
    assert len(media) == 1 and (ws.media_dir / media[0]["media_path"]).read_bytes()[:4] == b"\x89PNG"
    note = _entry(report, "Elena Voss.md")
    assert note["details"]["is_vault_note"] and note["details"]["wikilinks"] == 3
    assert note["details"]["inline_fields"] == 1 and "aliases" in note["details"]["frontmatter_keys"]


def test_structured_schema_pk_fk_and_prose(audited):
    _, report = audited
    companies = _entry(report, "companies.csv")["details"]["tables"][0]
    assert companies["primary_key"] == "company_id" and companies["name_column"] == "name"
    assert companies["prose_columns"] == ["description"] and companies["row_count"] == 3
    employees = _entry(report, "employees.xlsx")["details"]["tables"][0]
    fk = employees["foreign_keys"][0]
    assert fk["column"] == "company_id" and fk["ref_table"] == "companies"
    semi = _entry(report, "projects.json")["details"]["object_arrays"]
    paths = {a["array_path"]: a for a in semi}
    assert "/projects/*" in paths and "/projects/*/milestones/*" in paths
    assert paths["/projects/*"]["prose_fields"] == ["summary"]
    assert "lead_id" in paths["/projects/*"]["id_keys"]


def test_documents_docx_pptx_html_parsed(audited):
    ws, report = audited
    for name in ("brief.docx", "deck.pptx", "about.html"):
        assert _entry(report, name)["status"] == "usable"
    rec = ws.load_manifest().source_by_id(_entry(report, "deck.pptx")["doc_id"])
    pages = mr.extract_pages(ws, rec)
    assert [p.page_no for p in pages] == [1, 2] and "Globex Dynamics" in pages[0].text
    html = ws.load_manifest().source_by_id(_entry(report, "about.html")["doc_id"])
    text = mr.extract_pages(ws, html)[0].text
    assert "headquartered in Paris" in text and "var x" not in text and "Home | About" not in text


def test_url_manifest_discovery(tmp_path):
    (tmp_path / "sources.urls").write_text("# comment\nhttps://a.example/x\nnot-a-url\nhttps://b.example/y\n")
    files, urls, missing = mr.discover([str(tmp_path), "https://c.example/z", str(tmp_path / "nope")])
    assert urls == ["https://a.example/x", "https://b.example/y", "https://c.example/z"]
    assert missing[0]["reason"] == "path does not exist" and files == []


def test_web_fetch_flags_denied_paywall_and_empty(tmp_path):
    article = "<html><head><title>Robots</title></head><body><article>" + "<p>Warehouse robots are spreading quickly. </p>" * 12 + "</article></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ok":
            return httpx.Response(200, html=article)
        if path == "/forbidden":
            return httpx.Response(403, text="no")
        if path == "/login":
            return httpx.Response(401, text="auth")
        if path == "/paywall":
            return httpx.Response(200, html="<html><body><p>Subscribe to continue reading this premium story about robots.</p>" + "<p>teaser</p>" * 40 + "</body></html>")
        return httpx.Response(200, html="<html><body><script>render()</script></body></html>")

    ws = Workspace(tmp_path / "web_ws")
    base = "https://news.example"
    report = mr.audit(ws, [f"{base}/ok", f"{base}/forbidden", f"{base}/login", f"{base}/paywall", f"{base}/spa"],
                      transport=httpx.MockTransport(handler))
    reasons = {x["path"].rsplit("/", 1)[-1]: x["reason"] for x in report["excluded_sources"]}
    assert reasons == {"forbidden": "access denied (HTTP 403)", "login": "access denied (HTTP 401)",
                       "paywall": "paywall detected", "spa": "empty or script-rendered page"}
    assert report["modality_breakdown"]["web"] == 5 and len(report["usable"]) == 1
    stats = mr.chunk_workspace(ws)
    chunk = next(iter(ws.load_chunks().values()))
    assert stats["chunks"] == 1 and chunk.modality == "web" and chunk.meta["url"] == f"{base}/ok"


# ---------------------------------------------------------------------------------------------
# Chunker & sampler
# ---------------------------------------------------------------------------------------------


def test_split_text_offsets_and_overlap():
    text = ("Alice was beginning to get very tired. " * 120).strip()
    spans = mr.split_text(text, size=500, overlap=100)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert s2 < e1, "consecutive chunks must overlap"
        assert e1 - s1 <= 500
    assert mr.split_text(text, 500, 100) == spans


def test_chunking_is_deterministic_and_exact(synthetic_ws):
    ws = synthetic_ws
    first = {c.chunk_id: c.sha256 for c in ws.load_chunks().values()}
    mr.chunk_workspace(ws)
    second = {c.chunk_id: c.sha256 for c in ws.load_chunks(refresh=True).values()}
    assert first == second and len(first) >= 15
    manifest = ws.load_manifest()
    report_pdf = next(s for s in manifest.sources if s.name == "report.pdf")
    pages = {p.page_no: p.text for p in mr.extract_pages(ws, report_pdf)}
    pdf_chunks = [c for c in ws.load_chunks().values() if c.doc_id == report_pdf.doc_id]
    assert sorted(c.page_no for c in pdf_chunks) == [1, 2, 3]
    for c in pdf_chunks:
        assert pages[c.page_no][c.char_start:c.char_end] == c.text
        assert c.sha256 == sha256_text(c.text) and c.page_or_span.startswith(f"p{c.page_no}:")
    modalities = {c.modality for c in ws.load_chunks().values()}
    assert {"text", "notebook", "table_prose", "json_prose"} <= modalities
    prose = next(c for c in ws.load_chunks().values() if c.modality == "table_prose")
    assert prose.meta["column"] in ("description", "bio") and prose.page_or_span.startswith(prose.meta["table"])


def test_stratified_sample(synthetic_ws):
    res = mr.stratified_sample(synthetic_ws, n=10)
    assert len(res["sample"]) == 10
    whys = {s["why"] for s in res["sample"]}
    assert "beginning" in whys and "high-term-diversity" in whys
    assert len({s["modality"] for s in res["sample"]}) >= 3
    terms = {t["term"] for t in res["discovered_terms"]}
    assert {"Acme Robotics", "Project Atlas"} <= terms
    assert (synthetic_ws.dir / "sample.json").exists()


# ---------------------------------------------------------------------------------------------
# Structured compiler
# ---------------------------------------------------------------------------------------------


def test_ontology_draft_mapping_proposal(synthetic_ws):
    draft = synthetic_ws.load_ontology(False)
    kinds = sorted({m.kind for m in draft.structured_mappings})
    assert kinds == ["json", "notebook", "table", "vault"]
    employees = next(m for m in draft.structured_mappings if m.table == "employees")
    assert employees.id_field == "employee_id" and employees.prose_fields == ["bio"]
    assert employees.relations[0].field == "company_id" and employees.relations[0].target_match == "id"
    projects = next(m for m in draft.structured_mappings if m.table == "/projects/*")
    assert {r.field for r in projects.relations} == {"lead_id", "partner_ids"}
    milestones = next(m for m in draft.structured_mappings if m.table == "/projects/*/milestones/*")
    assert milestones.parent_relation.predicate == "HAS_MILESTONE"
    city = draft.get_class("City")
    chunks = synthetic_ws.load_chunks()
    assert len(city.examples) >= 2
    for ex in city.examples:
        assert normalize_text(ex.quote) in normalize_text(chunks[ex.chunk_id].text)
    assert synthetic_ws.load_ontology(True).allows("Person", "WORKS_AT", "Company")


def test_zero_token_structured_compilation_evidence(synthetic_ws):
    cands = synthetic_ws.load_candidates()
    assert cands and all(c.extractor == "structured" for c in cands)
    manifest = synthetic_ws.load_manifest()
    by_doc = {s.doc_id: s for s in manifest.sources}
    kinds = {e.kind for c in cands for e in c.evidence}
    assert {"table", "json", "notebook", "text"} <= kinds
    for c in cands:
        for ev in c.evidence:
            src = Path(by_doc[ev.doc_id].path)
            if ev.kind == "table":
                cols, rows = mr.read_table(src, ev.sheet_or_table)
                assert str(rows[ev.row_index][ev.column_name]) == str(ev.cell_value)
                assert ev.file_sha256 == sha256_file(src)
            elif ev.kind == "json":
                doc = mr.load_semistructured(src) if src.suffix != ".md" else mr.parse_markdown_note(src)["frontmatter"]
                assert mr.resolve_pointer(doc, ev.json_pointer) == ev.raw_value
            elif ev.kind == "notebook":
                nb = mr.load_notebook(src)
                cell = mr.cell_source(nb["cells"][ev.cell_index]).splitlines()
                assert cell[ev.line_span[0] - 1].strip() == ev.verbatim_quote
    edges = {(c.subject.name, c.predicate, c.object.name) for c in cands if c.kind == "edge"}
    assert ("Elena Voss", "WORKS_AT", "Acme Robotics") in edges
    assert ("Acme Robotics", "HEADQUARTERED_IN", "Berlin") in edges
    assert ("Project Atlas", "LED_BY", "Elena Voss") in edges
    assert ("Project Atlas", "HAS_MILESTONE", "Atlas Beta") in edges
    assert ("Computer Vision", "USED_BY", "Project Borealis") in edges
    assert ("Atlas Fleet Analysis", "IMPORTS", "sklearn") in edges
    assert ("Elena Voss", "WORKS_ON", "Project Atlas") in edges
    works_at = next(c for c in cands if c.kind == "edge" and c.predicate == "WORKS_AT" and c.subject.name == "Elena Voss")
    assert works_at.valid_from == "2019-03-01"
    elena = next(c for c in cands if c.kind == "node" and c.subject.name == "Elena Voss" and c.aliases)
    assert "Dr. Elena Voss" in elena.aliases


def test_rename_class_and_predicate_merge():
    ont = Ontology()
    ont.ensure_class("Employee").seed_terms.append("Bob")
    ont.ensure_class("Person")
    ont.ensure_class("Company")
    ont.ensure_relation("Employee", "HAS_COMPANY", "Company")
    ont.ensure_relation("Person", "WORKS_AT", "Company", functional=True)
    sc.rename_class(ont, "Employee", "Person")
    sc.rename_predicate(ont, "HAS_COMPANY", "WORKS_AT")
    assert ont.class_names() == {"Person", "Company"}
    assert len(ont.relations) == 1 and ont.relations[0].functional
    assert ont.get_class("Person").seed_terms == ["Bob"]


def test_approve_rejects_unknown_classes(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure()
    bad = Ontology(relations=[{"source_class": "A", "predicate": "X", "target_class": "B"}])
    with pytest.raises(ValueError, match="unknown class A"):
        sc.approve_ontology(ws, bad.model_dump())
    assert not ws.ontology_approved_path.exists()


# ---------------------------------------------------------------------------------------------
# CLI + real corpus audit
# ---------------------------------------------------------------------------------------------


def test_cli_m1_flow(tmp_path, synthetic_dir):
    from kumokg_cli import app

    runner = CliRunner()
    ws = tmp_path / "cli_ws"
    r = runner.invoke(app, ["audit", str(synthetic_dir), "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["ok"] and out["modality_breakdown"]["image"] == 2
    r = runner.invoke(app, ["chunk", "--workspace", str(ws)])
    assert json.loads(r.output)["chunks"] > 10
    r = runner.invoke(app, ["sample", "--workspace", str(ws), "--n", "5"])
    assert len(json.loads(r.output)["sample"]) == 5
    r = runner.invoke(app, ["status", "--workspace", str(ws)])
    st = json.loads(r.output)
    assert st["phase"] == "chunked" and st["next"].startswith("sample")
    r = runner.invoke(app, ["status", "--workspace", str(tmp_path / "none")])
    assert json.loads(r.output)["exists"] is False


def test_real_corpus_audit_alice_is_read_only(tmp_path):
    before = {p.name: sha256_file(p) for p in DATA_DIR.iterdir() if p.is_file()}
    ws = Workspace(tmp_path / "alice_audit")
    report = mr.audit(ws, [str(DATA_DIR)])
    pdf = next(f for f in report["files"] if f["ext"] == ".pdf")
    md = next(f for f in report["files"] if f["ext"] == ".md")
    assert pdf["details"]["page_count"] == 80 and pdf["status"] == "usable"
    assert pdf["details"]["median_chars_per_page"] > 1000
    assert md["details"]["near_duplicate_of"] == pdf["doc_id"] and md["details"]["near_duplicate_similarity"] > 0.95
    assert report["needs_ocr_or_vlm"] == []
    after = {p.name: sha256_file(p) for p in DATA_DIR.iterdir() if p.is_file()}
    assert before == after

"""Milestone 2: media prep, two-tier extraction (harness batches, BYOK pool, heuristic), verifier, doctor."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from core import media_prep, patcher, verifier, worker_pool as wp
from core.ir import (
    Candidate, EntityRef, ImageEvidence, JsonEvidence, NotebookEvidence, TableEvidence, TextEvidence, WebEvidence,
    iter_jsonl, read_json, sha256_file, write_json, write_jsonl,
)


def _chunk_with(ws, needle):
    return next(c for c in ws.load_chunks().values() if needle in " ".join(c.text.split()))


def _vlm(ws):
    return {r["item_id"]: r for r in iter_jsonl(ws.vlm_batches_path)}


# ---------------------------------------------------------------------------------------------
# media prep
# ---------------------------------------------------------------------------------------------


def test_media_prep_renders_scanned_pages_and_images(synthetic_ws):
    ws = synthetic_ws
    res = media_prep.prepare_media(ws)
    assert res["rendered_pages"] == 2 and res["items"] == 4
    items = _vlm(ws)
    pages = [it for it in items.values() if it["source_kind"] == "scanned_pdf_page"]
    assert sorted(it["page_no"] for it in pages) == [1, 2]
    for it in pages:
        png = ws.media_dir / it["image_path"]
        assert png.name.endswith(f"_p{it['page_no']}.png") and png.exists()
        assert it["width"] == round(595 / 72 * 150) and sha256_file(png) == it["image_sha256"]
    kinds = sorted(it["source_kind"] for it in items.values())
    assert kinds == ["image", "notebook_output", "scanned_pdf_page", "scanned_pdf_page"]
    assert not any("diagram-copy" in k for k in items), "perceptual duplicate must not be batched"
    assert len({it["batch_id"] for it in items.values()}) == 2
    assert media_prep.prepare_media(ws)["items"] == 4  # idempotent


def test_local_ocr_strategy_reports_missing_tesseract(synthetic_ws):
    if shutil.which("tesseract"):
        res = media_prep.prepare_media(synthetic_ws, strategy="local_ocr")
        assert res["documents"][0]["pages"] == 2
    else:
        with pytest.raises(RuntimeError, match="Tesseract"):
            media_prep.prepare_media(synthetic_ws, strategy="local_ocr")


# ---------------------------------------------------------------------------------------------
# Tier 1 harness batches
# ---------------------------------------------------------------------------------------------


def test_export_batches_sizes_and_attachments(synthetic_ws):
    ws = synthetic_ws
    media_prep.prepare_media(ws)
    res = wp.export_batches(ws, chunks_per_batch=6, images_per_batch=3)
    assert res["text_batches"] >= 3 and res["image_batches"] == 2
    index = read_json(ws.batches_dir / "index.json")
    for b in index["batches"]:
        payload = read_json(b["file"])
        assert payload["ontology"]["classes"] and "negative_examples" in payload["calibration"]
        assert payload["output_schema"]["required"] == ["claims"]
        if payload["kind"] == "text":
            assert 1 <= len(payload["items"]) <= 8 and all("text" in it and "chunk_sha256" in it for it in payload["items"])
        else:
            assert 1 <= len(payload["items"]) <= 4 and "media_dir" in payload
    sizes = [b["items"] for b in index["batches"] if b["kind"] == "text"]
    assert all(s == 6 for s in sizes[:-1])
    assert wp.export_batches(ws, chunks_per_batch=99)["batches"] < res["batches"]  # clamped to 8 -> fewer batches
    assert all(b["items"] <= 8 for b in read_json(ws.batches_dir / "index.json")["batches"])


def test_import_candidates_resumable_with_auto_chunk_location(synthetic_ws):
    ws = synthetic_ws
    wp.export_batches(ws)
    index = read_json(ws.batches_dir / "index.json")
    first = index["batches"][0]
    chunk = _chunk_with(ws, "Elena Voss leads Project Atlas")
    out = Path(first["output_file"])
    lines = [
        {"chunk_id": chunk.chunk_id, "kind": "edge", "subject": {"name": "Elena Voss", "label": "Person"},
         "predicate": "LEADS", "object": {"name": "Project Atlas", "label": "Project"},
         "quote": "Elena Voss leads Project Atlas", "confidence": 0.9},
        {"chunk_id": "auto", "kind": "edge", "subject": "Marcus Lee", "predicate": "leads",
         "object": "Project Borealis", "quote": "Marcus Lee leads Project Borealis from the Paris office"},
        {"chunk_id": "nope::c9999", "kind": "node", "subject": "Ghost", "quote": "boo"},
        {"kind": "edge", "subject": "X"},
    ]
    out.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken json\n", encoding="utf-8")
    res = wp.import_candidates(ws, [out])
    assert res["imported"] == 2 and len(res["errors"]) == 3
    assert {e["line"] for e in res["errors"]} == {3, 4, 5}
    harness = [c for c in ws.load_candidates() if c.extractor == "harness"]
    marcus = next(c for c in harness if c.subject.name == "Marcus Lee")
    ev = marcus.evidence[0]
    assert isinstance(ev, TextEvidence) and ev.page_no == 2 and marcus.predicate == "LEADS"
    page_chunk = ws.load_chunks()[ev.chunk_id]
    assert page_chunk.text[ev.char_start - page_chunk.char_start:ev.char_end - page_chunk.char_start] == ev.verbatim_quote
    again = wp.import_candidates(ws, [out])
    assert again["imported"] == 0 and again["duplicates"] == 2 and again["chunks_marked_processed"] == 0
    remaining = wp.export_batches(ws)
    assert remaining["items"] < sum(b["items"] for b in index["batches"])


# ---------------------------------------------------------------------------------------------
# heuristic extractor
# ---------------------------------------------------------------------------------------------


def test_heuristic_extractor_finds_cue_relations_deterministically(synthetic_ws):
    ws = synthetic_ws
    r1 = wp.extract_heuristic(ws)
    ids1 = sorted(c.candidate_id for c in ws.load_candidates() if c.extractor == "heuristic")
    r2 = wp.extract_heuristic(ws)
    ids2 = sorted(c.candidate_id for c in ws.load_candidates() if c.extractor == "heuristic")
    assert ids1 == ids2 and r1["candidates"] == r2["candidates"] > 0
    edges = {(c.subject.name, c.predicate, c.object.name): c for c in ws.load_candidates()
             if c.extractor == "heuristic" and c.kind == "edge"}
    for key in [("Acme Robotics", "HEADQUARTERED_IN", "Munich"), ("Elena Voss", "LEADS", "Project Atlas"),
                ("Marcus Lee", "LEADS", "Project Borealis"), ("Priya Raman", "WORKS_AT", "Acme Robotics"),
                ("Tomas Novak", "WORKS_AT", "Initech Labs"), ("Globex Dynamics", "HEADQUARTERED_IN", "Paris"),
                ("Project Atlas", "DEVELOPED_BY", "Acme Robotics")]:
        assert key in edges, key
    assert isinstance(edges[("Project Atlas", "DEVELOPED_BY", "Acme Robotics")].evidence[0], NotebookEvidence)
    for c in edges.values():
        ch = verifier.verify_evidence(c.evidence[0], verifier.VerifyContext(ws))
        assert ch["status"] == "verified", (c.subject.name, c.predicate, ch)
    led = [c for k, c in edges.items() if k[1] == "LEADS"]
    assert all(c.subject.label == "Person" for c in led)


def test_inverse_cue_phrase_flips_direction(synthetic_ws):
    ws = synthetic_ws
    ont = ws.require_ontology()
    gz = wp.build_gazetteer(ws, ont, ws.load_calibration())
    ms = gz.mentions("Warehouse autonomy programme led by Elena Voss today.")
    assert [m["canonical"] for m in ms] == ["Elena Voss"]
    vault_chunk = _chunk_with(ws, "Warehouse autonomy programme led by")
    wp.extract_heuristic(ws, chunk_ids=[vault_chunk.chunk_id])
    # the vault note mentions only one entity in that sentence (the note subject is implicit) -> no edge
    assert not any(c.extractor == "heuristic" and c.kind == "edge" and c.evidence[0].doc_id == vault_chunk.doc_id
                   for c in ws.load_candidates())


# ---------------------------------------------------------------------------------------------
# Tier 2 BYOK pool
# ---------------------------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, delay=0.01):
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.first_invalid_done = False
        self.throttled = set()
        self.delay = delay

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            body = json.loads(request.content)
            assert request.url.path.endswith("/chat/completions") and body["response_format"]["type"] == "json_object"
            assert request.headers["authorization"] == "Bearer test-key"
            user = body["messages"][1]["content"]
            item = json.loads(user)["items"][0]
            chunk_id = item["chunk_id"]
            if not self.first_invalid_done:
                self.first_invalid_done = True
                return httpx.Response(200, json={"choices": [{"message": {"content": "Sure! here are claims: [oops"}}]})
            if chunk_id not in self.throttled and len(self.throttled) < 2:
                self.throttled.add(chunk_id)
                return httpx.Response(429, json={"error": "rate limited"})
            text = item["text"]
            claims = []
            for name in ("Acme Robotics", "Globex Dynamics", "Elena Voss"):
                if name in text:
                    claims.append({"kind": "node", "subject": {"name": name}, "quote": name, "confidence": 0.8})
            claims.append({"kind": "edge", "subject": "Imaginary Corp", "predicate": "OWNS", "object": "Mars",
                           "quote": "Imaginary Corp owns Mars outright", "confidence": 0.9})
            return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps({"claims": claims}) + "\n```"}}]})
        finally:
            self.in_flight -= 1


def test_byok_config_presets_and_missing_key(synthetic_ws, monkeypatch):
    ws = synthetic_ws
    for k in list(__import__("os").environ):
        if k.startswith("KUMOKG_"):
            monkeypatch.delenv(k, raising=False)
    ws.env_path.write_text("KUMOKG_LLM_PRESET=groq\nKUMOKG_LLM_API_KEY=gsk_test\n", encoding="utf-8")
    cfg = wp.load_byok_config(ws)
    assert cfg["base_url"] == wp.PRESETS["groq"]["base_url"] and cfg["model"] and not cfg["missing"]
    ws.env_path.write_text("KUMOKG_LLM_PRESET=ollama\n", encoding="utf-8")
    assert wp.load_byok_config(ws)["missing"] == []  # local endpoints need no key
    ws.env_path.write_text("KUMOKG_LLM_PRESET=openrouter\n", encoding="utf-8")
    assert wp.load_byok_config(ws)["missing"] == ["api_key"]
    with pytest.raises(RuntimeError, match="BYOK config incomplete"):
        wp.run_byok(ws)
    monkeypatch.setenv("KUMOKG_LLM_MODEL", "override-model")
    assert wp.load_byok_config(ws)["model"] == "override-model"


def test_byok_pool_concurrency_retry_validation_and_resume(synthetic_ws):
    ws = synthetic_ws
    llm = FakeLLM()
    env = {"KUMOKG_LLM_BASE_URL": "https://llm.example/v1", "KUMOKG_LLM_API_KEY": "test-key", "KUMOKG_LLM_MODEL": "m"}
    first = wp.run_byok(ws, transport=httpx.MockTransport(llm), overrides=env, concurrency=3, backoff=0.001, limit=5)
    assert first["ok"] == 5 and first["failed"] == 0 and first["retries"] >= 3
    assert llm.max_in_flight <= 3 and first["max_in_flight"] <= 3
    total = sum(1 for c in ws.load_chunks().values() if c.modality in wp.TEXT_MODALITIES)
    second = wp.run_byok(ws, transport=httpx.MockTransport(llm), overrides=env, concurrency=4, backoff=0.001)
    assert second["tasks"] == total - 5 and second["failed"] == 0
    calls_before = llm.calls
    third = wp.run_byok(ws, transport=httpx.MockTransport(llm), overrides=env)
    assert third["tasks"] == 0 and llm.calls == calls_before
    byok = [c for c in ws.load_candidates() if c.extractor == "byok"]
    assert byok and not ws.p("byok_partial.jsonl").exists()
    rep = verifier.verify(ws)
    fabricated = [c for c in byok if c.subject.name == "Imaginary Corp"]
    assert fabricated and all(rep["candidates"][c.candidate_id]["verdict"] == "rejected" for c in fabricated)
    grounded_nodes = [c for c in byok if c.kind == "node"]
    assert all(rep["candidates"][c.candidate_id]["verdict"] == "verified" for c in grounded_nodes)


def test_byok_gives_up_on_persistent_errors(synthetic_ws):
    def handler(request):
        return httpx.Response(503, text="down")

    env = {"KUMOKG_LLM_BASE_URL": "https://llm.example/v1", "KUMOKG_LLM_API_KEY": "k", "KUMOKG_LLM_MODEL": "m"}
    res = wp.run_byok(synthetic_ws, transport=httpx.MockTransport(handler), overrides=env, limit=2, max_retries=2, backoff=0.001)
    assert res["failed"] == 2 and res["requests"] == 6 and res["failures"][0]["error"] == "HTTP 503"
    assert wp.processed_ledger(synthetic_ws, {"byok"}) == set()


def test_claim_schema_validation():
    with pytest.raises(Exception):
        wp.Claim.model_validate({"kind": "edge", "subject": "A", "quote": "x"})
    with pytest.raises(Exception):
        wp.Claim.model_validate({"kind": "node", "subject": "A", "quote": ""})
    with pytest.raises(Exception):
        wp.Claim.model_validate({"kind": "node", "subject": "A", "image_item_id": "i", "bbox_norm": [0, 0, 2, 1]})
    c = wp.Claim.model_validate({"kind": "node", "subject": "A", "quote": "A"})
    assert c.subject.name == "A" and c.confidence == 0.7
    assert wp.extract_json_object("noise ```json\n{\"claims\": []}\n``` trailing") == '{"claims": []}'


# ---------------------------------------------------------------------------------------------
# verifier
# ---------------------------------------------------------------------------------------------


def test_verifier_text_paths(synthetic_ws):
    ws = synthetic_ws
    ctx = verifier.VerifyContext(ws)
    ch = _chunk_with(ws, "Acme Robotics is headquartered in Munich")
    start = ch.text.index("Acme Robotics is headquartered")
    exact = TextEvidence(doc_id=ch.doc_id, chunk_id=ch.chunk_id, char_start=ch.char_start + start,
                         char_end=ch.char_start + start + 37, verbatim_quote=ch.text[start:start + 37], chunk_sha256=ch.sha256)
    assert verifier.verify_evidence(exact, ctx)["detail"] == "exact span match"
    moved = exact.model_copy(update={"char_start": 0, "char_end": 5, "verbatim_quote": "ACME   robotics is HEADQUARTERED in"})
    res = verifier.verify_evidence(moved, ctx)
    assert res["status"] == "verified" and "offsets corrected" in res["detail"]
    typo = exact.model_copy(update={"char_start": None, "char_end": None,
                                    "verbatim_quote": "Acme Robotics is headquartred in Munich, where its founders opened the first warehouse automation lab"})
    soft = verifier.verify_evidence(typo, ctx)
    assert soft["status"] == "soft_verified" and soft["ratio"] >= 0.92
    fake = exact.model_copy(update={"char_start": None, "char_end": None,
                                    "verbatim_quote": "Acme Robotics was founded on the Moon by penguins"})
    assert verifier.verify_evidence(fake, ctx)["status"] == "rejected"
    assert verifier.verify_evidence(exact.model_copy(update={"chunk_sha256": "deadbeef"}), ctx)["status"] == "rejected"
    assert verifier.verify_evidence(exact.model_copy(update={"chunk_id": "zzz::c0000"}), ctx)["status"] == "rejected"


def test_verifier_structured_notebook_image_web_paths(synthetic_ws, tmp_path):
    ws = synthetic_ws
    ctx = verifier.VerifyContext(ws)
    m = ws.load_manifest()
    comp = next(s for s in m.sources if s.name == "companies.csv")
    good = TableEvidence(doc_id=comp.doc_id, sheet_or_table="companies", row_index=0, column_name="hq_city",
                         cell_value="Berlin", file_sha256=comp.sha256)
    assert verifier.verify_evidence(good, ctx)["status"] == "verified"
    assert verifier.verify_evidence(good.model_copy(update={"cell_value": "Munich"}), ctx)["detail"].startswith("cell mismatch")
    assert "out of range" in verifier.verify_evidence(good.model_copy(update={"row_index": 99}), ctx)["detail"]
    assert "unknown column" in verifier.verify_evidence(good.model_copy(update={"column_name": "ceo"}), ctx)["detail"]
    emp = next(s for s in m.sources if s.name == "employees.xlsx")
    xl = TableEvidence(doc_id=emp.doc_id, sheet_or_table="employees", row_index=2, column_name="name",
                       cell_value="Priya Raman", file_sha256=emp.sha256)
    assert verifier.verify_evidence(xl, ctx)["status"] == "verified"
    proj = next(s for s in m.sources if s.name == "projects.json")
    js = JsonEvidence(doc_id=proj.doc_id, json_pointer="/projects/1/milestones/0/name", raw_value="Borealis Pilot",
                      file_sha256=proj.sha256)
    assert verifier.verify_evidence(js, ctx)["status"] == "verified"
    assert "does not resolve" in verifier.verify_evidence(js.model_copy(update={"json_pointer": "/projects/7/name"}), ctx)["detail"]
    tax = next(s for s in m.sources if s.name == "taxonomy.yaml")
    yml = JsonEvidence(doc_id=tax.doc_id, json_pointer="/technologies/1/maturity", raw_value="pilot", file_sha256=tax.sha256)
    assert verifier.verify_evidence(yml, ctx)["status"] == "verified"
    nb = next(s for s in m.sources if s.name == "analysis.ipynb")
    nbe = NotebookEvidence(doc_id=nb.doc_id, cell_index=2, cell_type="code", line_span=(3, 3),
                           verbatim_quote="from sklearn.cluster import KMeans", file_sha256=nb.sha256)
    assert verifier.verify_evidence(nbe, ctx)["status"] == "verified"
    assert "cell type" in verifier.verify_evidence(nbe.model_copy(update={"cell_type": "markdown"}), ctx)["detail"]
    media_prep.prepare_media(ws)
    item = next(it for it in _vlm(ws).values() if it["source_kind"] == "image")
    img = ImageEvidence(doc_id=item["doc_id"], image_path=item["image_path"], element_description="box",
                        image_sha256=item["image_sha256"])
    assert verifier.verify_evidence(img, ctx)["status"] == "needs_visual"
    assert verifier.verify_evidence(img.model_copy(update={"image_sha256": "0" * 64}), ctx)["detail"] == "image hash mismatch"
    assert verifier.verify_evidence(img.model_copy(update={"image_path": "../../etc/passwd"}), ctx)["status"] == "rejected"


def test_verifier_web_evidence(tmp_path):
    from core import modality_router as mr
    from core.ir import Workspace

    html = "<html><body><article>" + "<p>Globex Dynamics opened a robotics lab in Lyon last spring.</p>" * 6 + "</article></body></html>"
    ws = Workspace(tmp_path / "w")
    mr.audit(ws, ["https://x.example/a"], transport=httpx.MockTransport(lambda r: httpx.Response(200, html=html)))
    mr.chunk_workspace(ws)
    ch = next(iter(ws.load_chunks().values()))
    idx = wp.SourceIndex(ws)
    ev = wp.make_text_evidence(idx, ch, "opened a robotics lab in Lyon")
    assert isinstance(ev, WebEvidence) and ev.url == "https://x.example/a"
    ctx = verifier.VerifyContext(ws)
    assert verifier.verify_evidence(ev, ctx)["status"] == "verified"
    assert verifier.verify_evidence(ev.model_copy(update={"verbatim_quote": "closed a bakery in Rome"}), ctx)["status"] == "rejected"
    assert verifier.verify_evidence(ev.model_copy(update={"content_sha256": "x"}), ctx)["status"] == "rejected"


def test_hallucinated_edges_quarantined_with_full_precision(synthetic_ws):
    ws = synthetic_ws
    wp.extract_heuristic(ws)
    genuine = [
        ("Elena Voss", "LEADS", "Project Atlas", "Elena Voss leads Project Atlas, the flagship autonomy program"),
        ("Globex Dynamics", "PARTNERS_WITH", "Acme Robotics", "Globex Dynamics partners with Acme Robotics on Project Atlas field trials"),
        ("Initech Labs", "HEADQUARTERED_IN", "Munich", "Initech Labs is headquartered in Munich"),
    ]
    fabricated = [
        ("Elena Voss", "LEADS", "Project Borealis", "Elena Voss leads Project Borealis and its cold-storage pilots"),
        ("Marcus Lee", "WORKS_AT", "Initech Labs", "Marcus Lee works at Initech Labs as head of sensors"),
        ("Globex Dynamics", "HEADQUARTERED_IN", "Berlin", "Globex Dynamics is headquartered in Berlin near the river"),
        ("Priya Raman", "LEADS", "Project Atlas", "Priya Raman leads Project Atlas since the founding of the company"),
        ("Tomas Novak", "WORKS_AT", "Acme Robotics", "Tomas Novak works at Acme Robotics in the quality team"),
    ]
    report_chunks = [c for c in ws.load_chunks().values() if c.doc_id.startswith("report-")]
    lines = []
    for s, p, o, q in genuine + fabricated:
        target_chunk = next((c for c in report_chunks if q.split(",")[0] in " ".join(c.text.split())), report_chunks[0])
        lines.append({"chunk_id": target_chunk.chunk_id, "kind": "edge", "subject": s, "predicate": p, "object": o,
                      "quote": q, "confidence": 0.95})
    out = ws.p("harness_claims.jsonl")
    out.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    assert wp.import_candidates(ws, [out])["imported"] == 8
    rep = verifier.verify(ws)
    harness = {c.candidate_id: (c.subject.name, c.predicate, c.object.name) for c in ws.load_candidates() if c.extractor == "harness"}
    rejected = {harness[cid] for cid, r in rep["candidates"].items() if cid in harness and r["verdict"] == "rejected"}
    assert rejected == {(s, p, o) for s, p, o, _ in fabricated}
    quarantined = {(p["source"]["name"], p["predicate"], p["target"]["name"]) for p in rep["proposals"] if p["bucket"] == "quarantine"}
    assert quarantined == {(s, p, o) for s, p, o, _ in fabricated}
    patcher.auto_decide(ws, buckets=("auto", "review", "conflict", "quarantine"))
    patcher.commit(ws)
    committed = {(e.source_id, e.type, e.target_id) for e in ws.load_graph().edges}
    assert ("person:elena-voss", "LEADS", "project:project-borealis") not in committed
    assert verifier.doctor(ws, deep=False)["grounding"]["pct"] == 100.0


def test_closed_world_schema_extensions_and_flip_suggestion(synthetic_ws):
    ws = synthetic_ws
    ch = _chunk_with(ws, "Initech Labs is headquartered in Munich")
    lines = [
        {"chunk_id": ch.chunk_id, "kind": "edge", "subject": {"name": "Initech Labs", "label": "Company"},
         "predicate": "SELLS_SENSORS_TO", "object": {"name": "Project Borealis", "label": "Project"},
         "quote": "supplies vision sensors to Project Borealis", "confidence": 0.9},
        {"chunk_id": ch.chunk_id, "kind": "edge", "subject": {"name": "Munich", "label": "City"},
         "predicate": "HEADQUARTERED_IN", "object": {"name": "Initech Labs", "label": "Company"},
         "quote": "Initech Labs is headquartered in Munich", "confidence": 0.9},
        {"chunk_id": ch.chunk_id, "kind": "node", "subject": {"name": "Vision Sensor", "label": "Component"},
         "quote": "vision sensors", "confidence": 0.9},
    ]
    out = ws.p("ext.jsonl")
    out.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    wp.import_candidates(ws, [out])
    rep = verifier.verify(ws)
    exts = rep["proposed_schema_extensions"]
    assert any(e.get("predicate") == "SELLS_SENSORS_TO" and e["closed_world"] for e in exts)
    assert any(e.get("class") == "Component" for e in exts)
    sells = next(p for p in rep["proposals"] if p["predicate"] == "SELLS_SENSORS_TO")
    assert sells["bucket"] == "review" and any("relation grammar" in r for r in sells["reasons"])
    flipped = next(p for p in rep["proposals"] if p["source"]["name"] == "Munich")
    assert flipped["ontology"]["status"] == "flip_suggested" and flipped["bucket"] == "review"


def test_entity_resolution_alias_token_overlap_and_ambiguity(synthetic_ws):
    ws = synthetic_ws
    ont = ws.require_ontology()
    res = verifier.EntityResolver(ont, ws.load_calibration())
    for c in ws.load_candidates():
        if c.kind == "node" and c.extractor == "structured":
            res.register(c.subject.label, c.subject.name, c.aliases)
    assert res.resolve(EntityRef(name="Acme", label="Company"))["name"] == "Acme Robotics"
    assert res.resolve(EntityRef(name="Dr. Elena Voss", label="Person"))["method"] == "alias"
    voss = res.resolve(EntityRef(name="Voss", label="Person"))
    assert voss["name"] == "Elena Voss" and voss["method"] == "token_overlap"
    cross = res.resolve(EntityRef(name="Priya Raman", label="Note"))
    assert cross["label"] == "Person" and cross["method"] == "cross_label"
    amb = res.resolve(EntityRef(name="Project", label="Project"))
    assert amb["method"] == "ambiguous" and set(amb["ambiguous_with"]) == {"Project Atlas", "Project Borealis"}
    assert res.ambiguous and res.resolve(EntityRef(name="she", label="Person"))["method"] == "pronoun"
    # a longer or possessive mention is a different entity, never folded into the shorter canonical name
    sister = res.resolve(EntityRef(name="Elena Voss's Sister", label="Person"))
    assert sister["name"] == "Elena Voss's Sister" and sister["method"] == "new"
    team = res.resolve(EntityRef(name="Acme Robotics Hardware Team", label="Company"))
    assert team["name"] != "Acme Robotics"


def test_cross_source_contradiction_detected_and_reported_by_doctor(synthetic_ws):
    ws = synthetic_ws
    wp.extract_heuristic(ws)
    rep = verifier.verify(ws)
    hq = [c for c in rep["contradictions"] if c["predicate"] == "HEADQUARTERED_IN" and c["source_name"] == "Acme Robotics"]
    assert len(hq) == 1
    values = {v["target_name"]: v for v in hq[0]["values"]}
    assert set(values) == {"Berlin", "Munich"} and hq[0]["modalities"] == ["table", "text"]
    assert all(p["bucket"] == "conflict" for p in rep["proposals"] if p["proposal_id"] in {v["ref"] for v in hq[0]["values"]})
    patcher.auto_decide(ws)
    patcher.commit(ws)
    card = verifier.doctor(ws)
    assert any(c["predicate"] == "HEADQUARTERED_IN" and c["scope"] == "candidates" for c in card["contradictions"])
    # once a reviewer commits both values, the contradiction is reported at graph scope
    conflict_ids = [p["proposal_id"] for p in rep["proposals"] if p["bucket"] == "conflict"]
    patcher.record_decisions(ws, [{"proposal_id": pid, "decision": "approve"} for pid in conflict_ids])
    patcher.commit(ws)
    card = verifier.doctor(ws)
    assert any(c["scope"] == "graph" and c["predicate"] == "HEADQUARTERED_IN" for c in card["contradictions"])


def test_property_contradiction_between_table_and_text(synthetic_ws):
    ws = synthetic_ws
    ch = _chunk_with(ws, "Helios Robotics Industry Report 2024")
    claim = {"chunk_id": ch.chunk_id, "kind": "attribute", "subject": {"name": "Acme Robotics", "label": "Company"},
             "predicate": "founded", "value": 2024, "quote": "Helios Robotics Industry Report 2024"}
    out = ws.p("attr.jsonl")
    out.write_text(json.dumps(claim), encoding="utf-8")
    wp.import_candidates(ws, [out])
    rep = verifier.verify(ws)
    prop = [c for c in rep["contradictions"] if c["kind"] == "functional_property"]
    assert prop and prop[0]["predicate"] == "founded" and {str(v["value"]) for v in prop[0]["values"]} == {"2009", "2024"}


def test_calibration_feedback_loop(synthetic_ws):
    ws = synthetic_ws
    wp.extract_heuristic(ws)
    rep = verifier.verify(ws)
    target = next(p for p in rep["proposals"] if p["predicate"] == "DEVELOPED_BY")
    wrong = next(p for p in rep["proposals"] if p["predicate"] == "SUPPLIES")
    res = patcher.record_decisions(ws, [
        {"proposal_id": wrong["proposal_id"], "decision": "reject", "reason": "supplier relation is to the company"},
        {"proposal_id": target["proposal_id"], "decision": "approve", "edit": {"predicate": "BUILT_BY"}},
    ])
    assert res["calibration_rules_added"] == {"negative_examples": 1, "predicate_corrections": 1,
                                              "direction_corrections": 0, "alias_merges": 0}
    cal = ws.load_calibration()
    assert cal["negative_examples"][0]["predicate"] == "SUPPLIES"
    rep2 = verifier.verify(ws)
    again = next(p for p in rep2["proposals"] if p["source"]["name"] == wrong["source"]["name"] and p["target"]["name"] == wrong["target"]["name"]
                 and p["predicate"] == "SUPPLIES")
    assert again["bucket"] == "quarantine" and any("calibration" in r for r in again["reasons"])
    corrected = [p for p in rep2["proposals"] if p["predicate"] == "BUILT_BY"]
    assert corrected and corrected[0]["ontology"]["status"] in ("schema_extension", "ok")
    batches = wp.export_batches(ws)
    payload = read_json(batches["files"][0])
    assert payload["calibration"]["predicate_corrections"][0]["to"] == "BUILT_BY"


def test_commit_and_doctor_scorecard(synthetic_ws):
    ws = synthetic_ws
    wp.extract_heuristic(ws)
    verifier.verify(ws)
    patcher.auto_decide(ws)
    stats = patcher.commit(ws)
    assert stats["edges_added"] > 20 and stats["skipped_quarantined"] == 0
    g = ws.load_graph()
    assert ws.sqlite_path.exists() and {"graph_nodes", "graph_edges"} <= set(stats)
    works = [e for e in g.edges if e.type == "WORKS_AT" and e.source_id == "person:priya-raman"]
    assert len(works) == 1 and works[0].valid_from == "2021-01-10" and {x.kind for x in works[0].evidence} == {"table", "text"}
    card = verifier.doctor(ws)
    assert card["grounding"]["pct"] == 100.0 and card["schema_compliance"]["pct"] == 100.0
    assert card["source_integrity"]["pct"] == 100.0 and card["dangling_edges"] == [] and card["score"] >= 90
    cov = card["coverage_per_source"]
    report_doc = next(k for k in cov if k.startswith("report-"))
    assert cov[report_doc]["edges"] >= 3 and cov[report_doc]["chunks"] == 3
    again = patcher.commit(ws)
    assert again["edges_added"] == 0 and again["nodes_added"] == 0  # idempotent
    # inject damage: orphan + dangling + tampered chunk
    from core.ir import Node, Edge

    g.nodes.append(Node(id="city:atlantis", labels=["City"], canonical_name="Atlantis"))
    g.edges.append(Edge(id="e-dangling", source_id="person:ghost", target_id="company:acme-robotics", type="WORKS_AT"))
    ws.save_graph(g)
    rows = list(iter_jsonl(ws.chunks_path))
    for r in rows:
        if r["doc_id"].startswith("report-") and "Munich" in r["text"]:
            r["text"] = r["text"].replace("Munich", "Hamburg")
    write_jsonl(ws.chunks_path, rows)
    card = verifier.doctor(ws)
    assert "city:atlantis" in card["orphan_nodes"] and card["dangling_edges"][0]["edge_id"] == "e-dangling"
    assert card["grounding"]["pct"] < 100.0 and card["source_integrity"]["pct"] < 100.0
    assert card["score"] < 100


def test_cli_m2_flow(synthetic_ws):
    from typer.testing import CliRunner
    from kumokg_cli import app

    runner = CliRunner()
    w = ["--workspace", str(synthetic_ws.root)]
    for cmd in (["media-prep"], ["extract", "--extractor", "heuristic"], ["verify"], ["decide", "--auto"], ["commit"], ["doctor", "--fast"]):
        r = runner.invoke(app, cmd + w)
        assert r.exit_code == 0, (cmd, r.output)
    out = json.loads(r.output)
    assert out["grounding_pct"] == 100.0 and out["nodes"] > 10
    r = runner.invoke(app, ["extract", "--extractor", "byok"] + w)
    assert r.exit_code == 2

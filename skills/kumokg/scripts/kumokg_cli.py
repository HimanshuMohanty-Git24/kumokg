#!/usr/bin/env python3
"""KumoKG CLI: one entrypoint for audit -> ontology -> extraction -> verification -> review -> commit ->
query / patch / eval -> export.

Every command prints a compact JSON summary so coding agents never need to read raw sources,
``chunks.jsonl`` or ``graph.json`` into their context.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import typer  # noqa: E402

from core.ir import Workspace, read_json  # noqa: E402

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False,
                  help="KumoKG: verified knowledge graphs from any corpus, with GraphRAG and top-10 DB export.")

WS_OPT = typer.Option(".", "--workspace", "-w", help="Workspace root (state lives in <workspace>/.kumokg/).")


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def emit(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def fail(msg: str, code: int = 1, hint: str | None = None) -> None:
    payload = {"ok": False, "error": msg}
    if hint:
        payload["hint"] = hint
    typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
    raise typer.Exit(code)


def _ws(workspace: str, must_exist: bool = True) -> Workspace:
    ws = Workspace(workspace)
    if must_exist and not ws.exists():
        fail(f"no KumoKG workspace at {ws.root}", hint="run `kumokg init` or `kumokg audit <sources>` first")
    return ws


# ----------------------------------------------------------------------------------------------
# Milestone 1: workspace, audit, chunking, sampling, ontology draft, structured compiler
# ----------------------------------------------------------------------------------------------


@app.command()
def init(workspace: str = WS_OPT,
         mode: str = typer.Option("hitl", help="hitl | autonomous"),
         tier: str = typer.Option("harness", help="harness | byok")) -> None:
    """Create (or resume) a workspace and print its manifest summary."""
    if mode not in ("hitl", "autonomous") or tier not in ("harness", "byok"):
        fail("mode must be hitl|autonomous and tier harness|byok")
    ws = Workspace(workspace)
    m = ws.ensure(mode=mode, tier=tier)
    emit({"ok": True, "workspace": str(ws.root), "mode": m.mode, "tier": m.extraction_tier, "phase": m.phase})


@app.command()
def status(workspace: str = WS_OPT) -> None:
    """Instant resume summary: phase, gates, counts (never dumps raw state)."""
    ws = Workspace(workspace)
    if not ws.exists():
        emit({"ok": True, "exists": False, "next": "kumokg audit <sources> --workspace <dir>"})
        return
    m = ws.load_manifest()
    graph = read_json(ws.graph_path, {}) or {}
    n_chunks = sum(1 for _ in open(ws.chunks_path, encoding="utf-8")) if ws.chunks_path.exists() else 0
    n_cands = sum(1 for _ in open(ws.candidates_path, encoding="utf-8")) if ws.candidates_path.exists() else 0
    emit({
        "ok": True, "exists": True, "workspace": str(ws.root), "mode": m.mode, "tier": m.extraction_tier,
        "phase": m.phase, "gates": m.gates, "sources": len(m.sources),
        "usable_sources": sum(1 for s in m.sources if s.status == "usable"),
        "chunks": n_chunks, "candidates": n_cands,
        "ontology": "approved" if ws.ontology_approved_path.exists() else ("draft" if ws.ontology_draft_path.exists() else None),
        "graph": {"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", []))},
        "next": _next_step(ws, m.phase),
    })


def _next_step(ws: Workspace, phase: str) -> str:
    if ws.graph_path.exists():
        return "query | patch | doctor | export"
    steps = {
        "init": "audit", "audited": "chunk", "chunked": "sample -> propose-ontology",
        "ontology_draft": "studio --gate gate1 --blocking (or approve-ontology)",
        "ontology_approved": "compile-structured -> extract | export-batches",
        "structured_compiled": "extract | export-batches", "extracted": "verify",
        "verified": "studio --gate gate2 --blocking (or decide --auto)", "decided": "commit",
    }
    return steps.get(phase, "status")


@app.command()
def audit(sources: List[str] = typer.Argument(..., help="Directories, files, URL manifests (*.urls) or URLs."),
          workspace: str = WS_OPT,
          mode: str = typer.Option("hitl", help="hitl | autonomous"),
          tier: str = typer.Option("harness", help="harness | byok"),
          dedup: str = typer.Option("flag", help="flag (keep near-duplicates) | skip (exclude them)"),
          skip: List[str] = typer.Option([], "--skip", help="doc_id or file name to exclude"),
          full: bool = typer.Option(False, "--full", help="print the full report instead of the compact summary")) -> None:
    """Scan sources, route modalities, triage PDFs (OCR/VLM), dedupe and write audit-report.json."""
    from core.modality_router import audit as run_audit, compact_audit

    ws = Workspace(workspace)
    report = run_audit(ws, sources, mode=mode, tier=tier, dedup=dedup, skip=skip)
    emit(report if full else {"ok": True, **compact_audit(report), "report": str(ws.audit_path)})


@app.command()
def chunk(workspace: str = WS_OPT, size: int = typer.Option(1500, help="target chunk size (chars)"),
          overlap: int = typer.Option(200, help="overlap (chars)")) -> None:
    """Deterministically chunk every usable text-bearing source into chunks.jsonl."""
    from core.modality_router import chunk_workspace

    emit({"ok": True, **chunk_workspace(_ws(workspace), size=size, overlap=overlap)})


@app.command()
def sample(workspace: str = WS_OPT, n: int = typer.Option(12, help="number of chunks"),
           preview: int = typer.Option(600, help="max chars per chunk preview")) -> None:
    """Stratified sample (beginning/middle/end + high term diversity) for ontology discovery."""
    from core.modality_router import stratified_sample

    emit({"ok": True, **stratified_sample(_ws(workspace), n=n, preview_chars=preview)})


@app.command("propose-ontology")
def propose_ontology_cmd(workspace: str = WS_OPT,
                         from_file: Optional[Path] = typer.Option(None, "--from", help="agent-authored ontology draft JSON")) -> None:
    """Write ontology.draft.json (structured mappings + agent draft + grounded examples)."""
    from core.structured_compiler import propose_ontology

    ws = _ws(workspace)
    draft = json.loads(from_file.read_text(encoding="utf-8")) if from_file else None
    ont = propose_ontology(ws, draft)
    emit({"ok": True, "draft": str(ws.ontology_draft_path), "classes": sorted(ont.class_names()),
          "relations": [f"{r.source_class}-{r.predicate}->{r.target_class}" for r in ont.relations],
          "structured_mappings": [m.mapping_id for m in ont.structured_mappings],
          "examples": {c.name: len(c.examples) for c in ont.entity_classes},
          "next": "kumokg studio --gate gate1 --blocking   (or: kumokg approve-ontology)"})


@app.command("ontology-edit")
def ontology_edit(workspace: str = WS_OPT,
                  rename_class: List[str] = typer.Option([], "--rename-class", help="Old=New"),
                  rename_predicate: List[str] = typer.Option([], "--rename-predicate", help="OLD=NEW"),
                  closed_world: Optional[bool] = typer.Option(None, "--closed-world/--open-world")) -> None:
    """Apply Gate 1 style edits to ontology.draft.json from the command line."""
    from core import structured_compiler as sc
    from core.ir import write_json

    ws = _ws(workspace)
    ont = ws.load_ontology(False)
    if ont is None:
        fail("no ontology draft", hint="kumokg propose-ontology")
    for spec in rename_class:
        old, _, new = spec.partition("=")
        sc.rename_class(ont, old.strip(), new.strip())
    for spec in rename_predicate:
        old, _, new = spec.partition("=")
        sc.rename_predicate(ont, old.strip(), new.strip())
    if closed_world is not None:
        ont.closed_world = closed_world
    write_json(ws.ontology_draft_path, ont)
    emit({"ok": True, "classes": sorted(ont.class_names()), "relations": len(ont.relations)})


@app.command("approve-ontology")
def approve_ontology_cmd(workspace: str = WS_OPT,
                         file: Optional[Path] = typer.Option(None, "--file", help="approve this JSON instead of the draft")) -> None:
    """Approve the ontology (CLI equivalent of Gate 1's Approve & Continue)."""
    from core.structured_compiler import approve_ontology

    ws = _ws(workspace)
    data = json.loads(file.read_text(encoding="utf-8")) if file else None
    try:
        ont = approve_ontology(ws, data)
    except (ValueError, FileNotFoundError) as exc:
        fail(str(exc))
    emit({"ok": True, "approved": str(ws.ontology_approved_path), "fingerprint": ont.fingerprint(),
          "classes": len(ont.entity_classes), "relations": len(ont.relations), "closed_world": ont.closed_world})


@app.command("compile-structured")
def compile_structured_cmd(workspace: str = WS_OPT) -> None:
    """Zero-token compilation of tables / JSON / YAML / notebooks / Obsidian vaults into candidates."""
    from core.structured_compiler import compile_structured

    emit({"ok": True, **compile_structured(_ws(workspace))})


# ----------------------------------------------------------------------------------------------
# Milestone 2: media prep, two-tier extraction, verification, decisions, commit, doctor
# ----------------------------------------------------------------------------------------------


@app.command("media-prep")
def media_prep_cmd(workspace: str = WS_OPT,
                   strategy: str = typer.Option("render", help="render (VLM batches) | local_ocr (Tesseract)"),
                   images_per_batch: int = typer.Option(3, help="2-4 images per VLM batch"),
                   dpi: int = typer.Option(150, help="render DPI for scanned pages")) -> None:
    """Render scanned PDF pages, collect images/notebook charts and write vlm_batches.jsonl."""
    from core.media_prep import prepare_media

    try:
        emit({"ok": True, **prepare_media(_ws(workspace), strategy=strategy, images_per_batch=images_per_batch, dpi=dpi)})
    except RuntimeError as exc:
        fail(str(exc), code=2)


@app.command("export-batches")
def export_batches_cmd(workspace: str = WS_OPT, chunks_per_batch: int = typer.Option(6, help="5-8 chunks per text task"),
                       images_per_batch: int = typer.Option(3, help="2-4 images per image task"),
                       kind: str = typer.Option("all", help="all | text | image"),
                       include_processed: bool = typer.Option(False, help="re-export chunks already imported")) -> None:
    """Tier 1: write tiny harness task files (ontology + calibration attached)."""
    from core.worker_pool import export_batches

    emit({"ok": True, **export_batches(_ws(workspace), chunks_per_batch, images_per_batch, kind, include_processed)})


@app.command("import-candidates")
def import_candidates_cmd(files: List[Path] = typer.Argument(..., help="JSONL claim files produced by the agent"),
                          workspace: str = WS_OPT,
                          extractor: str = typer.Option("harness", help="label recorded on the candidates")) -> None:
    """Tier 1: ingest agent-produced claims (resumable by chunk_sha256; duplicates ignored)."""
    from core.worker_pool import import_candidates

    emit({"ok": True, **import_candidates(_ws(workspace), [str(f) for f in files], extractor=extractor)})


@app.command()
def extract(workspace: str = WS_OPT, extractor: str = typer.Option("heuristic", help="heuristic | byok"),
            kind: str = typer.Option("text", help="byok: text | image | all"),
            concurrency: Optional[int] = typer.Option(None, help="byok: parallel requests"),
            limit: Optional[int] = typer.Option(None, help="byok: max tasks this run")) -> None:
    """Run the offline heuristic baseline or the resumable Tier 2 BYOK pool."""
    from core import worker_pool as wp

    ws = _ws(workspace)
    if extractor == "heuristic":
        emit({"ok": True, **wp.extract_heuristic(ws)})
    elif extractor == "byok":
        try:
            emit({"ok": True, **wp.run_byok(ws, kind=kind, concurrency=concurrency, limit=limit)})
        except RuntimeError as exc:
            fail(str(exc), code=2)
    else:
        fail("extractor must be heuristic or byok")


@app.command()
def verify(workspace: str = WS_OPT, threshold: Optional[float] = typer.Option(None, help="auto-approve confidence")) -> None:
    """Programmatic grounding + ontology check + entity resolution + contradiction detection."""
    from core.verifier import compact_verify, verify as run_verify

    emit({"ok": True, **compact_verify(run_verify(_ws(workspace), threshold)), "report": str(_ws(workspace).verify_path)})


@app.command()
def decide(workspace: str = WS_OPT,
           auto: bool = typer.Option(False, "--auto", help="approve every auto-verified high-confidence proposal"),
           approve: List[str] = typer.Option([], "--approve", help="proposal id"),
           reject: List[str] = typer.Option([], "--reject", help="proposal id"),
           file: Optional[Path] = typer.Option(None, "--file", help="JSON list of {proposal_id, decision, edit}")) -> None:
    """CLI equivalent of Gate 2 decisions (feeds calibration.json)."""
    from core.patcher import auto_decide, record_decisions

    ws = _ws(workspace)
    items = [{"proposal_id": p, "decision": "approve"} for p in approve] + \
            [{"proposal_id": p, "decision": "reject"} for p in reject]
    if file:
        items += json.loads(file.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"ok": True}
    if items:
        out["explicit"] = record_decisions(ws, items, by="cli")
    if auto:
        out["auto"] = auto_decide(ws)
    emit(out)


@app.command()
def commit(workspace: str = WS_OPT) -> None:
    """Commit approved proposals into graph.json + graph.sqlite (append-only changelog)."""
    from core.patcher import commit as run_commit

    emit({"ok": True, **run_commit(_ws(workspace), by="cli")})


@app.command()
def doctor(workspace: str = WS_OPT, full: bool = typer.Option(False, "--full"),
           fast: bool = typer.Option(False, "--fast", help="skip source re-derivation integrity check")) -> None:
    """Graph Health Scorecard: grounding %, schema compliance %, contradictions, orphans, coverage."""
    from core.verifier import compact_doctor, doctor as run_doctor

    card = run_doctor(_ws(workspace), deep=not fast)
    emit(card if full else {"ok": True, **compact_doctor(card)})


# ----------------------------------------------------------------------------------------------
# Milestone 3: Studio
# ----------------------------------------------------------------------------------------------


@app.command()
def studio(workspace: str = WS_OPT, port: int = typer.Option(8765, help="0 = ephemeral port"),
           gate: Optional[str] = typer.Option(None, help="gate1 | gate2 (land on that page)"),
           blocking: bool = typer.Option(False, "--blocking/--no-block",
                                         help="--blocking exits after the gate is approved/committed; --no-block serves until Ctrl+C"),
           open_browser: bool = typer.Option(False, "--open", help="open the page in the default browser"),
           timeout: Optional[float] = typer.Option(None, help="give up waiting after N seconds")) -> None:
    """Local Studio: Gate 1 ontology review, Gate 2 edge review, live Explorer + export (127.0.0.1 only)."""
    from studio.server import run_studio

    if gate not in (None, "gate1", "gate2"):
        fail("gate must be gate1 or gate2")
    ws = _ws(workspace)
    result = run_studio(ws, port=port, gate=gate, blocking=blocking, open_browser=open_browser, timeout=timeout)
    emit({"ok": bool(result.get("ok", True)), "result": result, "status_after": ws.load_manifest().gates})


# ----------------------------------------------------------------------------------------------
# Milestone 4: retrieval, patching, eval, MCP
# ----------------------------------------------------------------------------------------------


@app.command()
def query(question: str = typer.Argument(..., help="natural-language question"), workspace: str = WS_OPT,
          mode: str = typer.Option("local", help="local | ppr | lazy-global"),
          hops: int = typer.Option(2, help="neighbourhood / path depth"),
          as_of: Optional[str] = typer.Option(None, "--as-of", help="bi-temporal instant (YYYY-MM-DD or ISO datetime)"),
          fmt: str = typer.Option("compact", "--format", help="compact | json"),
          max_tokens: int = typer.Option(1500, "--max-tokens", help="context budget")) -> None:
    """Local GraphRAG: BM25 neighbourhood, ontology-constrained PPR multi-hop, or lazy global communities."""
    from core.retrieval import query as run_query

    try:
        res = run_query(_ws(workspace), question, mode=mode, hops=hops, as_of=as_of, max_tokens=max_tokens, fmt=fmt)
    except ValueError as exc:
        fail(str(exc))
    emit({"ok": True, **res})


def _ref(value: Optional[str], label: Optional[str]) -> Any:
    if value is None:
        return None
    return value if ":" in value and " " not in value else {"name": value, "label": label}


@app.command()
def patch(op: Optional[str] = typer.Argument(None, help="add-node | merge-nodes | add-edge | supersede-edge | retract-edge | add-alias"),
          workspace: str = WS_OPT,
          file: Optional[Path] = typer.Option(None, "--file", help="JSON patch document {ops: [...]}"),
          source: Optional[str] = typer.Option(None, help="node id or entity name"),
          source_label: Optional[str] = typer.Option(None, "--source-label"),
          target: Optional[str] = typer.Option(None, help="node id or entity name"),
          target_label: Optional[str] = typer.Option(None, "--target-label"),
          edge_type: Optional[str] = typer.Option(None, "--type", help="relation type"),
          edge_id: Optional[str] = typer.Option(None, "--edge-id"),
          node_id: Optional[str] = typer.Option(None, "--node-id"),
          label: Optional[str] = typer.Option(None), name: Optional[str] = typer.Option(None),
          alias: Optional[str] = typer.Option(None), keep: Optional[str] = typer.Option(None),
          merge: List[str] = typer.Option([], "--merge"),
          valid_from: Optional[str] = typer.Option(None, "--valid-from"), valid_to: Optional[str] = typer.Option(None, "--valid-to"),
          quote: Optional[str] = typer.Option(None, help="verbatim evidence quote (verified against chunks)"),
          chunk_id: Optional[str] = typer.Option(None, "--chunk-id", help="chunk of the quote (default: auto-locate)"),
          reason: Optional[str] = typer.Option(None), message: str = typer.Option("", help="changelog message"),
          create: bool = typer.Option(False, help="create missing endpoint nodes"),
          allow_extension: bool = typer.Option(False, "--allow-extension", help="permit ontology extensions"),
          allow_unverified: bool = typer.Option(False, "--allow-unverified", help="record an unverified user assertion"),
          dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Atomic, verified, bi-temporal graph mutation (+ changelog + live Studio refresh)."""
    from core.patcher import apply_patch

    ws = _ws(workspace)
    if file:
        doc = json.loads(file.read_text(encoding="utf-8"))
    elif op:
        o: dict[str, Any] = {"op": op}
        if quote:
            o["quote"] = quote
            o["chunk_id"] = chunk_id or "auto"
        if op == "add-node":
            o.update(label=label, name=name, aliases=[alias] if alias else [])
        elif op == "add-alias":
            o.update(node_id=node_id, alias=alias)
        elif op == "merge-nodes":
            o.update(keep=keep, merge=merge)
        elif op == "add-edge":
            o.update(source=_ref(source, source_label), target=_ref(target, target_label), type=edge_type,
                     valid_from=valid_from, valid_to=valid_to, create=create)
        elif op == "supersede-edge":
            new: dict[str, Any] = {"create": create}
            if target:
                new["target"] = _ref(target, target_label)
            if source:
                new["source"] = _ref(source, source_label)
            if edge_type:
                new["type"] = edge_type
            o.update(edge_id=edge_id, valid_from=valid_from, new=new)
        elif op == "retract-edge":
            o.update(edge_id=edge_id, valid_to=valid_to, reason=reason or "")
        doc = {"ops": [o], "message": message, "author": "cli"}
    else:
        fail("give an OP or --file")
    doc.setdefault("allow_extension", allow_extension)
    doc.setdefault("allow_unverified", allow_unverified)
    res = apply_patch(ws, doc, dry_run=dry_run)
    if not res["ok"]:
        fail(res["error"])
    emit(res)


@app.command()
def history(workspace: str = WS_OPT, item: Optional[str] = typer.Option(None, "--id", help="node or edge id"),
            limit: int = typer.Option(20)) -> None:
    """Show the append-only changelog (optionally for one node/edge)."""
    from core.patcher import history as run_history

    emit({"ok": True, "changes": run_history(_ws(workspace), item, limit)})


@app.command("eval")
def eval_cmd(workspace: str = WS_OPT, samples: int = typer.Option(20), k: int = typer.Option(10),
             seed: int = typer.Option(13), min_entity_recall: float = typer.Option(0.5, "--min-entity-recall"),
             full: bool = typer.Option(False, "--full")) -> None:
    """Golden 2-hop/3-hop benchmark: Entity/Edge Recall@K, citation grounding %, latency (local vs ppr)."""
    from core.eval_harness import run_eval

    rep = run_eval(_ws(workspace), samples=samples, k=k, seed=seed, min_entity_recall=min_entity_recall)
    out = rep if full else {k2: rep[k2] for k2 in ("questions", "k", "summary", "pass", "criteria")}
    emit({"ok": rep["pass"], **out})
    if not rep["pass"]:
        raise typer.Exit(1)


@app.command("serve-mcp")
def serve_mcp(workspace: str = WS_OPT, transport: str = typer.Option("stdio", help="stdio | sse | streamable-http")) -> None:
    """Expose the workspace graph as MCP tools (needs the [mcp] extra)."""
    from core.mcp_server import McpUnavailable, serve

    try:
        serve(_ws(workspace), transport=transport)
    except McpUnavailable as exc:
        fail(str(exc), code=2)


# ----------------------------------------------------------------------------------------------
# Milestone 5: database adapters
# ----------------------------------------------------------------------------------------------


@app.command()
def adapters() -> None:
    """List the 10 export targets (formats, pip extra, env vars for live push)."""
    from adapters.base import load_registry

    emit({"ok": True, "adapters": [{k: a[k] for k in ("name", "title", "extra", "formats", "env", "modes")} for a in load_registry()]})


def _targets(target: str) -> list[str]:
    from adapters.base import load_registry

    names = [a["name"] for a in load_registry()]
    if target == "all":
        return names
    if target not in names:
        fail(f"unknown target {target!r}", hint=f"choose one of: {', '.join(names)} or all")
    return [target]


@app.command()
def export(workspace: str = WS_OPT, target: str = typer.Option(..., "--target", help="adapter name or 'all'"),
           provenance_mode: str = typer.Option("props", "--provenance-mode", help="props | nodes | reified"),
           pg_mode: str = typer.Option("plain", "--pg-mode", help="postgres: plain | age | pgvector"),
           neptune_mode: str = typer.Option("lpg", "--neptune-mode", help="neptune: lpg | rdf"),
           push: bool = typer.Option(False, "--push", help="also push live using .kumokg/.env credentials"),
           no_verify: bool = typer.Option(False, "--no-verify", help="skip the parity check")) -> None:
    """Mode A offline bundle (+ parity) and optional Mode B live push for one or all targets."""
    from adapters.base import AdapterError, DriverMissing, run_export

    ws = _ws(workspace)
    results = []
    for t in _targets(target):
        opts = {"pg_mode": pg_mode} if t == "postgres" else {"neptune_mode": neptune_mode} if t == "neptune" else {}
        try:
            res = run_export(ws, t, provenance_mode=provenance_mode, options=opts, verify=not no_verify, push=push)
        except DriverMissing as exc:
            fail(str(exc), code=2)
        except (AdapterError, FileNotFoundError) as exc:
            fail(f"{t}: {exc}")
        row = {"target": t, "out_dir": res["out_dir"], "files": len(res["files"]), "expected": res["expected"]}
        if "parity" in res:
            p = res["parity"]
            row["parity"] = {k: p[k] for k in ("ok", "nodes_found", "edges_found", "dangling", "errors")}
        if "push" in res:
            row["push"] = res["push"]
        results.append(row)
    ok = all(r.get("parity", {}).get("ok", True) for r in results)
    emit({"ok": ok, "provenance_mode": provenance_mode, "results": results})
    if not ok:
        raise typer.Exit(1)


@app.command("verify-export")
def verify_export(workspace: str = WS_OPT, target: str = typer.Option(..., "--target", help="adapter name or 'all'")) -> None:
    """Re-parse exported files and assert 100% node/edge parity, zero dangling endpoints and valid syntax."""
    from adapters.base import get_adapter

    ws = _ws(workspace)
    results = []
    for t in _targets(target):
        p = get_adapter(t, ws).verify_parity()
        results.append({k: p.get(k) for k in ("target", "ok", "provenance_mode", "nodes_expected", "nodes_found",
                                               "edges_expected", "edges_found", "dangling", "checksums_verified", "errors")}
                       | {"syntax_ok": all(v.get("ok") for v in (p.get("syntax") or {}).values()), "files_checked": len(p.get("syntax") or {})})
    ok = all(r["ok"] for r in results)
    emit({"ok": ok, "targets": len(results), "passed": sum(r["ok"] for r in results), "results": results})
    if not ok:
        raise typer.Exit(1)


def main() -> None:
    _configure_stdio()
    app()


if __name__ == "__main__":
    main()

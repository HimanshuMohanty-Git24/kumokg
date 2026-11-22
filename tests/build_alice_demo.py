"""Real-world end-to-end run on ./data/ (Alice's Adventures in Wonderland) into ``.kumokg_alice_demo/``.

Every step goes through the public CLI (``kumokg_cli.app``), exactly as an agent following SKILL.md would run it.
The harness (Tier 1) answers come from ``tests/fixtures/alice/harness_claims.jsonl`` — claims authored by the coding
agent from the exported batches, each with a verbatim quote — complemented by the offline heuristic extractor.

Run directly: ``uv run python tests/build_alice_demo.py [workspace]``.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "skills" / "kumokg" / "scripts"))

from typer.testing import CliRunner  # noqa: E402

from kumokg_cli import app  # noqa: E402

FIXTURES = HERE / "fixtures" / "alice"
SIZE_PATCH_QUOTE = "in fact she was now more than nine feet high"
NARRATIVE_T0 = "1862-07-04T14:05"
NARRATIVE_T1 = "1862-07-04T14:30"


class StepError(RuntimeError):
    pass


def kg(*args: str, expect: int = 0) -> dict[str, Any]:
    res = CliRunner().invoke(app, list(args), catch_exceptions=False)
    if res.exit_code != expect:
        raise StepError(f"kumokg {' '.join(args)} exited {res.exit_code}: {res.output[-2000:]}")
    return json.loads(res.output)


def build_alice_demo(workspace: Path, data_dir: Path, log: Any = print) -> dict[str, Any]:
    from core.ir import Workspace, iter_jsonl, read_json
    from core.worker_pool import SourceIndex

    workspace = Path(workspace)
    if workspace.exists():
        shutil.rmtree(workspace)
    w = ["--workspace", str(workspace)]
    out: dict[str, Any] = {}

    out["audit"] = kg("audit", str(data_dir), *w, "--mode", "hitl", "--tier", "harness")
    log(f"audit: {out['audit']['status_breakdown']}")
    out["chunk"] = kg("chunk", *w)
    out["sample"] = kg("sample", *w, "--n", "12")
    log(f"chunks: {out['chunk']['chunks']}, sample: {len(out['sample']['sample'])}")
    out["propose"] = kg("propose-ontology", *w, "--from", str(FIXTURES / "ontology_draft.json"))
    out["approve"] = kg("approve-ontology", *w)
    out["compile"] = kg("compile-structured", *w)
    out["media"] = kg("media-prep", *w)
    out["batches"] = kg("export-batches", *w)
    log(f"harness batches: {out['batches']['batches']}")

    # Tier 1: the agent's answers (verbatim-quote claims) are written next to the batches and imported.
    ws = Workspace(workspace)
    idx = SourceIndex(ws)
    claims = []
    for row in iter_jsonl(FIXTURES / "harness_claims.jsonl"):
        anchor = row.pop("batch_anchor", None)
        if anchor:
            row["chunk_id"] = idx.locate_chunk(anchor).chunk_id
        claims.append(row)
    answer = ws.batches_dir / "harness-answers.out.jsonl"
    answer.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in claims), encoding="utf-8")
    out["import"] = kg("import-candidates", str(answer), *w)
    out["heuristic"] = kg("extract", *w, "--extractor", "heuristic")
    out["verify"] = kg("verify", *w)
    log(f"verify: {out['verify']['summary']['buckets']}")

    # Gate 2 (scripted reviewer): approve everything auto-verified, resolve the size conflict in favour of the dated
    # chapter-1 fact; everything else stays pending for visual review in the Studio.
    rep = read_json(ws.verify_path)
    size = next(p for p in rep["proposals"] if p["predicate"] == "HAS_SIZE" and p["target"]["name"] == "Ten Inches High")
    decisions = ws.p("gate2-decisions.json")
    decisions.write_text(json.dumps([{"proposal_id": size["proposal_id"], "decision": "approve"}]), encoding="utf-8")
    out["decide"] = kg("decide", *w, "--auto", "--file", str(decisions))
    out["commit"] = kg("commit", *w)
    log(f"commit: +{out['commit']['edges_added']} edges, +{out['commit']['nodes_added']} nodes")

    # Live bi-temporal chat patch: Alice grows to nine feet in chapter II.
    graph = ws.load_graph()
    ten = next(e for e in graph.edges if e.type == "HAS_SIZE" and e.target_id == "size:ten-inches-high")
    out["patch"] = kg("patch", "supersede-edge", *w, "--edge-id", ten.id, "--target", "Nine Feet High",
                      "--target-label", "Size", "--create", "--valid-from", NARRATIVE_T1, "--quote", SIZE_PATCH_QUOTE,
                      "--message", "Chat: after the Eat Me cake Alice is more than nine feet high (chapter II)")
    out["history"] = kg("history", *w, "--id", ten.id)

    out["doctor"] = kg("doctor", *w)
    out["query_local"] = kg("query", "Who did Alice meet at the Mad Tea-Party?", *w, "--mode", "local")
    out["query_ppr"] = kg("query", "How is Alice connected to the Knave of Hearts and the stolen tarts?", *w, "--mode", "ppr", "--hops", "3")
    out["query_global"] = kg("query", "Which groups of characters and places make up the story?", *w, "--mode", "lazy-global")
    out["size_before"] = kg("query", "How tall is Alice?", *w, "--mode", "local", "--as-of", "1862-07-04T14:10", "--format", "json")
    out["size_after"] = kg("query", "How tall is Alice?", *w, "--mode", "local", "--as-of", "1862-07-04T15:00", "--format", "json")
    out["eval"] = kg("eval", *w, "--samples", "15")
    log(f"eval: {out['eval']['summary']}")
    out["export"] = kg("export", *w, "--target", "all")
    out["verify_export"] = kg("verify-export", *w, "--target", "all")
    log(f"verify-export: {out['verify_export']['passed']}/{out['verify_export']['targets']}")
    out["status"] = kg("status", *w)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / ".kumokg_alice_demo"
    data = next((p for p in sorted(REPO.iterdir()) if p.is_dir() and p.name.lower() == "data"), REPO / "data")
    summary = build_alice_demo(target, data)
    print(json.dumps({k: summary[k] for k in ("doctor", "eval", "status")}, indent=2, default=str))

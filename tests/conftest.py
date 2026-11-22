"""Shared fixtures for the KumoKG test-suite.

The synthetic multi-modal corpus is generated once per session; helpers build fully-prepared
workspaces (audited, chunked, ontology approved with Gate 1 edits, structured-compiled) so each
milestone's tests can start from the phase they exercise.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "kumokg"
SCRIPTS_DIR = SKILL_DIR / "scripts"
# the real-world corpus folder ships as `Data/` on some checkouts; resolve it case-insensitively (Linux is case-sensitive)
DATA_DIR = next((p for p in sorted(REPO_ROOT.iterdir()) if p.is_dir() and p.name.lower() == "data"), REPO_ROOT / "data")
TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

for p in (str(SCRIPTS_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import generate_fixtures as gf  # noqa: E402
from core import modality_router as mr  # noqa: E402
from core import structured_compiler as sc  # noqa: E402
from core.ir import Workspace  # noqa: E402


@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synthetic_corpus")
    gf.generate(out)
    return out


def gate1_edits(ont):
    """Apply the reviewer edits a human would make at Gate 1 on the synthetic corpus."""
    for old, new in gf.GATE1_CLASS_RENAMES.items():
        sc.rename_class(ont, old, new)
    for old, new in gf.GATE1_PREDICATE_RENAMES.items():
        sc.rename_predicate(ont, old, new)
    for m in ont.structured_mappings:
        if m.kind == "table" and m.table == "companies":
            m.property_fields.pop("hq_city", None)
            from core.ir import FieldRelation

            m.relations.append(FieldRelation(field="hq_city", predicate="HEADQUARTERED_IN", target_class="City",
                                             target_match="name"))
        if m.kind == "table" and m.table == "employees":
            for r in m.relations:
                if r.predicate == "WORKS_AT":
                    r.valid_from_field = "start_date"
    return ont


def build_synthetic_workspace(root: Path, corpus: Path, compile_structured: bool = True) -> Workspace:
    ws = Workspace(root)
    mr.audit(ws, [str(corpus)], mode="autonomous")
    mr.chunk_workspace(ws)
    ont = sc.propose_ontology(ws, gf.AGENT_DRAFT)
    gate1_edits(ont)
    sc.approve_ontology(ws, ont.model_dump())
    if compile_structured:
        sc.compile_structured(ws)
    return ws


@pytest.fixture(scope="session")
def synthetic_ws_template(tmp_path_factory, synthetic_dir) -> Path:
    root = tmp_path_factory.mktemp("synthetic_ws_template")
    build_synthetic_workspace(root, synthetic_dir)
    return root


@pytest.fixture()
def synthetic_ws(tmp_path, synthetic_ws_template) -> Workspace:
    """A fresh copy of the prepared synthetic workspace for tests that mutate state."""
    dest = tmp_path / "ws"
    shutil.copytree(synthetic_ws_template, dest)
    return Workspace(dest)

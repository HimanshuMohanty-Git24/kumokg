"""Milestone 6: agentskills.io compliance, byte/line budgets, progressive disclosure and self-contained packaging."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib

import yaml

from conftest import REPO_ROOT, SKILL_DIR

SKILL_MD = SKILL_DIR / "SKILL.md"
REFS = ["01-source-audit-and-modalities.md", "02-ontology-and-gate1.md", "03-extraction-vlm-and-verify.md",
        "04-gate2-chat-and-mcp.md", "05-export-targets-and-eval.md"]


def _frontmatter():
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.S)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return yaml.safe_load(m.group(1)), m.group(2), text


def test_skill_md_budget_and_frontmatter():
    fm, body, text = _frontmatter()
    raw = SKILL_MD.read_bytes()
    assert len(raw) <= 6144, f"SKILL.md is {len(raw)} bytes"
    assert len(text.splitlines()) <= 220
    assert set(fm) == {"name", "description", "license", "compatibility"}
    assert fm["name"] == "kumokg" == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fm["name"]) and len(fm["name"]) <= 64
    assert fm["license"] == "PolyForm-Noncommercial-1.0.0"
    assert fm["compatibility"] == "Requires Python 3.11+ and uv. Works across Claude Code, Cursor, OpenCode, and Codex."
    desc = fm["description"]
    assert 50 <= len(desc) <= 500
    assert desc.startswith("KumoKG ") and not re.match(r"^(I|You|We)\b", desc)
    for kw in ("documents", "books", "notes", "CSV", "JSON", "notebooks", "images", "scanned PDFs", "web", "GraphRAG", "export", "Use it when"):
        assert kw in desc, kw
    approx_tokens = len(raw) / 4
    assert approx_tokens < 1600


def test_skill_md_state_machine_contract():
    _, body, _ = _frontmatter()
    assert "NEVER read raw sources, PDFs, `chunks.jsonl`" in body and "`graph.json`" in body
    assert "status --workspace" in body and "Resume first" in body
    assert "ask ONE concise question" in body.replace("Ask ONE", "ask ONE")
    for word in ("HITL", "Autonomous", "Harness", "BYOK", "--blocking", "poll", "--no-block"):
        assert word in body, word
    for ref in REFS:
        assert f"references/{ref}" in body, ref
    for cmd in ("audit", "chunk", "sample", "propose-ontology", "approve-ontology", "compile-structured", "media-prep",
                "export-batches", "import-candidates", "verify", "decide", "commit", "doctor", "query", "patch", "studio",
                "serve-mcp", "export", "verify-export", "eval"):
        assert re.search(rf"\$KG {re.escape(cmd)}\b", body), cmd


def test_references_exist_and_are_focused():
    for ref in REFS:
        p = SKILL_DIR / "references" / ref
        assert p.exists(), ref
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# 0") and len(text) > 1500 and "```" in text
    assert "harness_vlm" in (SKILL_DIR / "references" / REFS[0]).read_text(encoding="utf-8")
    assert '"cue_phrases"' in (SKILL_DIR / "references" / REFS[1]).read_text(encoding="utf-8")
    assert '"chunk_id"' in (SKILL_DIR / "references" / REFS[2]).read_text(encoding="utf-8")
    assert "supersede-edge" in (SKILL_DIR / "references" / REFS[3]).read_text(encoding="utf-8")
    ref5 = (SKILL_DIR / "references" / REFS[4]).read_text(encoding="utf-8")
    for t in ("neo4j", "postgres", "memgraph", "falkordb", "neptune", "arangodb", "tigergraph", "janusgraph", "rdf", "duckdb"):
        assert f"| {t} |" in ref5, t


def test_no_paths_escape_the_skill_directory():
    for p in SKILL_DIR.rglob("*"):
        if p.is_dir() or ".venv" in p.parts or "__pycache__" in p.parts or p.suffix in (".png", ".duckdb", ".parquet", ".lock"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        assert "../" not in text and "..\\" not in text, f"{p} references a parent path"
        for link in (re.findall(r"\]\(([^)#]+)\)", text) if p.suffix == ".md" else []):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            assert (p.parent / link).resolve().is_relative_to(SKILL_DIR.resolve()), f"{p}: {link}"
            assert (p.parent / link).exists(), f"{p}: broken link {link}"
    for rel in re.findall(r"`((?:references|assets|scripts)/[\w./-]+)`", SKILL_MD.read_text(encoding="utf-8")):
        assert (SKILL_DIR / rel).exists(), rel


def test_pyproject_dependency_policy():
    py = tomllib.loads((SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    proj = py["project"]
    assert proj["name"] == "kumokg" and proj["requires-python"] == ">=3.11"
    deps = {re.split(r"[<>=\[]", d)[0].lower() for d in proj["dependencies"]}
    assert deps == {"pydantic", "typer", "pymupdf", "httpx", "pyyaml", "openpyxl", "pillow", "networkx", "rdflib", "duckdb",
                    "beautifulsoup4", "python-docx", "python-pptx", "python-dotenv", "pytest"}
    extras = proj["optional-dependencies"]
    assert set(extras) == {"docling", "mcp", "neo4j", "postgres", "memgraph", "falkordb", "neptune", "arangodb", "tigergraph",
                           "janusgraph", "rdf", "all-db"}
    all_db = {re.split(r"[<>=\[]", d)[0].lower() for d in extras["all-db"]}
    for name in ("neo4j", "postgres", "memgraph", "falkordb", "neptune", "arangodb", "tigergraph", "janusgraph", "rdf"):
        assert {re.split(r"[<>=\[]", d)[0].lower() for d in extras[name]} <= all_db, name
    assert proj["scripts"]["kumokg"] == "kumokg_cli:main"
    reg = json.loads((SKILL_DIR / "scripts" / "adapters" / "registry.json").read_text(encoding="utf-8"))
    for a in reg["adapters"]:
        assert not a["extra"] or a["extra"] in extras


def test_env_template_covers_every_variable():
    env = (SKILL_DIR / "assets" / "templates" / "env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^(KUMOKG_\w+)=", env, re.M))
    reg = json.loads((SKILL_DIR / "scripts" / "adapters" / "registry.json").read_text(encoding="utf-8"))
    for a in reg["adapters"]:
        assert set(a["env"]) <= keys, a["name"]
    used = set()
    for py in (SKILL_DIR / "scripts").rglob("*.py"):
        used |= set(re.findall(r'"(KUMOKG_[A-Z0-9_]+)"', py.read_text(encoding="utf-8")))
    used -= {"KUMOKG_STUDIO_LOG"}
    assert used <= keys, used - keys
    assert {"KUMOKG_LLM_BASE_URL", "KUMOKG_LLM_API_KEY", "KUMOKG_LLM_MODEL", "KUMOKG_VLM_MODEL"} <= keys


def test_code_has_no_placeholders():
    pattern = re.compile(r"\b(TODO|FIXME|XXX)\b|raise NotImplementedError|^\s*\.\.\.\s*$", re.M)
    for p in list((SKILL_DIR / "scripts").rglob("*.py")) + list((SKILL_DIR / "assets").rglob("*.js")):
        text = p.read_text(encoding="utf-8")
        hits = [m.group(0) for m in pattern.finditer(text)]
        abstract_ok = p.name == "base.py" and all(h.strip() == "..." for h in hits)
        assert not hits or abstract_ok, f"{p.name}: {hits}"


def test_skill_runs_from_an_isolated_copy(tmp_path):
    copy = tmp_path / "installed" / "kumokg"
    shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    cli = copy / "scripts" / "kumokg_cli.py"
    ws = tmp_path / "ws"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.txt").write_text("The Mock Turtle told Alice a very long story about the sea school. " * 5, encoding="utf-8")
    env = {"PATH": __import__("os").environ["PATH"], "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
           "PYTHONIOENCODING": "utf-8"}
    probe = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import studio.server as s, core.ir as i; "
                            "print(s.ASSETS_DIR); print(i.__file__)", str(copy / "scripts")],
                           capture_output=True, text=True, env=env, timeout=120)
    assert probe.returncode == 0, probe.stderr
    assets_dir, core_file = probe.stdout.splitlines()
    assert assets_dir.startswith(str(copy)) and core_file.startswith(str(copy))
    for args in (["adapters"], ["audit", str(tmp_path / "docs"), "--workspace", str(ws)], ["chunk", "--workspace", str(ws)],
                 ["status", "--workspace", str(ws)]):
        r = subprocess.run([sys.executable, str(cli)] + args, capture_output=True, text=True, env=env, timeout=120, encoding="utf-8")
        assert r.returncode == 0, (args, r.stderr)
        json.loads(r.stdout)
    assert json.loads(r.stdout)["chunks"] >= 1


def test_readme_documents_install_modes_and_targets():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for s in ("npx skills@latest add HimanshuMohanty-Git24/kumokg --skill kumokg", "--agent codex --global",
              "--agent claude-code --global", "~/.agents/skills/kumokg/", "~/.claude/skills/kumokg/", ".cursor/skills/kumokg/",
              "HITL", "Autonomous", "BYOK", "KUMOKG_LLM_PRESET", "verify-export", "serve-mcp", "uv run pytest",
              "PolyForm Noncommercial", "COMMERCIAL_LICENSE.md"):
        assert s in readme, s
    for img in ("gate1-ontology.png", "gate2-edge-review.png", "explorer-graph.png", "explorer-time-travel.png"):
        assert f"docs/assets/{img}" in readme and (REPO_ROOT / "docs" / "assets" / img).stat().st_size > 20_000, img
    assert not re.search(r"\bMIT\b", readme)
    rows = [ln for ln in readme.splitlines() if ln.startswith("| ")]
    for t in ("Neo4j", "PostgreSQL", "Memgraph", "FalkorDB", "Neptune", "ArangoDB", "TigerGraph", "JanusGraph", "RDF", "DuckDB"):
        assert any(ln.split("|")[1].strip().startswith((t, f"Amazon {t}")) for ln in rows), t
    assert (REPO_ROOT / "docker-compose.test.yml").exists() and (SKILL_DIR / "LICENSE").exists()


def test_skill_code_parses_on_the_minimum_python():
    """requires-python is >=3.11: every script must parse with a real 3.11 interpreter (e.g. no PEP 701 f-strings)."""
    code = ("import ast, pathlib, sys; [ast.parse(p.read_text(encoding='utf-8'), str(p)) "
            "for p in pathlib.Path(sys.argv[1]).rglob('*.py')]; print(sys.version_info[:2])")
    if sys.version_info[:2] == (3, 11):
        cmd = [sys.executable, "-c", code, str(SKILL_DIR / "scripts")]
    else:
        uv = shutil.which("uv")
        assert uv, "uv is required to fetch a Python 3.11 interpreter for this check"
        cmd = [uv, "run", "--python", "3.11", "--no-project", "python", "-c", code, str(SKILL_DIR / "scripts")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "(3, 11)" in r.stdout


POLYFORM_NC_SECTIONS = ("# PolyForm Noncommercial License 1.0.0", "<https://polyformproject.org/licenses/noncommercial/1.0.0>",
                        "## Acceptance", "## Copyright License", "## Distribution License", "## Notices",
                        "## Changes and New Works License", "## Patent License", "## Noncommercial Purposes", "## Personal Uses",
                        "## Noncommercial Organizations", "## Fair Use", "## No Other Rights", "## Patent Defense",
                        "## Violations", "## No Liability", "## Definitions")


def test_dual_license_polyform_noncommercial_plus_commercial():
    notice = "Required Notice: Copyright 2026 Himanshu Mohanty (https://github.com/HimanshuMohanty-Git24/kumokg)"
    root, skill = (REPO_ROOT / "LICENSE").read_bytes(), (SKILL_DIR / "LICENSE").read_bytes()
    assert root == skill, "the skill ships the same LICENSE as the repository"
    text = root.decode("utf-8")
    assert text.splitlines()[0] == notice
    pos = [text.index(h) for h in POLYFORM_NC_SECTIONS]
    assert pos == sorted(pos), "PolyForm sections out of order"
    assert "within 32 days of receiving notice" in text and "MIT" not in text
    com_root, com_skill = (REPO_ROOT / "COMMERCIAL_LICENSE.md").read_text(encoding="utf-8"), (SKILL_DIR / "COMMERCIAL_LICENSE.md").read_text(encoding="utf-8")
    assert com_root == com_skill
    for s in ("PolyForm Noncommercial License 1.0.0", "### Individual / Startup", "### Team", "### Enterprise",
              "codehimanshu24@gmail.com", "https://linkedin.com/in/himanshumohanty", "https://github.com/HimanshuMohanty-Git24",
              "Internal company or enterprise workflows", "Commercial products and SaaS", "Paid consulting and agency work",
              "Commercial distribution", "Academic and public research", "Non-commercial organizations"):
        assert s in com_root, s
    proj = tomllib.loads((SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["license"] == "PolyForm-Noncommercial-1.0.0" and set(proj["license-files"]) == {"LICENSE", "COMMERCIAL_LICENSE.md"}
    assert not any("License ::" in c for c in proj.get("classifiers", [])), "PEP 639: no license classifiers with an SPDX expression"
    for p in (SKILL_DIR / "SKILL.md", SKILL_DIR / "pyproject.toml", REPO_ROOT / "pyproject.toml"):
        assert not re.search(r"\bMIT\b", p.read_text(encoding="utf-8")), p


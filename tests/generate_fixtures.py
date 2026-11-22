"""Automated synthetic multi-modal test data factory for KumoKG.

``generate(out_dir)`` writes a realistic "Helios robotics" corpus covering every modality and every
deliberate failure mode the pipeline must handle:

* clean multi-page text PDF (with a cross-source contradiction and a bi-temporal update sentence)
* scanned, image-only PDF with zero text layer
* corrupted PDF and empty (0-byte) PDF
* CSV + XLSX with primary/foreign keys and long free-text columns
* nested JSON and YAML with ID references
* Jupyter notebook (markdown + code + embedded base64 PNG chart)
* Obsidian vault (frontmatter, aliases, ``[[wikilinks]]`` and ``key:: [[value]]`` inline fields)
* standalone diagram PNG + a near-identical copy (perceptual dedup)
* DOCX, PPTX and HTML documents

Run directly: ``uv run python tests/generate_fixtures.py <out_dir>``.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import sys
from pathlib import Path

COMPANIES = [
    ("C1", "Acme Robotics", 2009, "Berlin",
     "Acme Robotics designs autonomous mobile robots for warehouse logistics, combining computer vision, fleet "
     "orchestration software and safety-certified hardware for large European retailers."),
    ("C2", "Globex Dynamics", 2012, "Paris",
     "Globex Dynamics builds simulation platforms and digital twins that let manufacturers rehearse robot "
     "deployments before a single machine is installed on the factory floor."),
    ("C3", "Initech Labs", 2015, "Munich",
     "Initech Labs manufactures rugged depth cameras and embedded vision sensors used by mobile robots that "
     "operate in dusty, poorly lit industrial environments across Germany."),
]

EMPLOYEES = [
    ("E1", "Elena Voss", "C1", "Chief Scientist", "2019-03-01",
     "Elena Voss spent a decade in academic robotics before moving to industry, where she now steers research on "
     "multi-robot coordination and safe human-robot interaction in crowded warehouses."),
    ("E2", "Marcus Lee", "C2", "CTO", "2016-07-15",
     "Marcus Lee oversees the simulation engine roadmap and previously built physics engines for video games "
     "before turning his attention to industrial digital twins and large-scale testing."),
    ("E3", "Priya Raman", "C1", "Robotics Engineer", "2021-01-10",
     "Priya Raman writes the motion-planning stack for the warehouse fleet and maintains the open benchmark that "
     "compares planners on narrow-aisle navigation scenarios with moving obstacles."),
    ("E4", "Tomas Novak", "C3", "Vision Lead", "2018-11-05",
     "Tomas Novak leads the depth-sensing team, focusing on calibration routines that keep stereo cameras "
     "accurate despite vibration, temperature swings and years of continuous operation."),
]

PROJECTS = {
    "projects": [
        {"id": "P1", "name": "Project Atlas", "lead_id": "E1", "partner_ids": ["C2"], "budget_musd": 12.5,
         "summary": "Project Atlas coordinates hundreds of autonomous robots inside a single distribution centre, "
                    "using shared maps, traffic rules and a central scheduler that negotiates right of way.",
         "milestones": [{"name": "Atlas Alpha", "date": "2021-05-01"}, {"name": "Atlas Beta", "date": "2022-09-15"}]},
        {"id": "P2", "name": "Project Borealis", "lead_id": "E2", "partner_ids": ["C1", "C3"], "budget_musd": 8.0,
         "summary": "Project Borealis evaluates cold-storage robotics, where batteries drain quickly, floors are icy and "
                    "cameras fog up whenever a robot moves between temperature zones.",
         "milestones": [{"name": "Borealis Pilot", "date": "2023-02-20"}]},
    ]
}

TAXONOMY_YAML = """technologies:
  - id: T1
    name: Computer Vision
    maturity: production
    used_by_ids: [P1, P2]
  - id: T2
    name: Swarm Planning
    maturity: pilot
    used_by_ids: [P1]
"""

REPORT_PAGES = [
    "Helios Robotics Industry Report 2024\n\n"
    "Acme Robotics is headquartered in Munich, where its founders opened the first warehouse automation lab.\n"
    "Elena Voss leads Project Atlas, the flagship autonomy program of the company.\n"
    "Analysts expect the warehouse robotics market to double within five years as labour shortages persist.",
    "Partnerships\n\n"
    "Globex Dynamics partners with Acme Robotics on Project Atlas field trials in two distribution centres.\n"
    "Marcus Lee leads Project Borealis from the Paris office of Globex Dynamics.\n"
    "Priya Raman works at Acme Robotics as a robotics engineer focused on narrow-aisle navigation.",
    "Leadership Changes\n\n"
    "In June 2023 Elena Voss joined Globex Dynamics as Chief Scientist after a decade of research.\n"
    "Initech Labs is headquartered in Munich and supplies vision sensors to Project Borealis.\n"
    "Tomas Novak works at Initech Labs and calibrates the depth cameras used in cold storage.",
]

SCANNED_PAGES = [
    "SCANNED MEMO - CONFIDENTIAL\n\nInitech Labs signed a supply agreement\nwith Acme Robotics for 4,000 depth cameras.",
    "Page 2 of memo\n\nDelivery starts in Munich during Q3.\nApproved by Tomas Novak.",
]

AGENT_DRAFT = {
    "namespace": "https://kumokg.dev/helios/",
    "prefix": "helios",
    "closed_world": True,
    "entity_classes": [
        {"name": "Company", "description": "A robotics company", "seed_terms": []},
        {"name": "Person", "description": "An employee or researcher", "seed_terms": []},
        {"name": "Project", "description": "A funded R&D programme", "seed_terms": []},
        {"name": "City", "description": "A city", "seed_terms": ["Berlin", "Munich", "Paris"]},
        {"name": "Technology", "description": "A technology area", "seed_terms": []},
        {"name": "Milestone", "description": "A project milestone", "seed_terms": []},
    ],
    "relations": [
        {"source_class": "Company", "predicate": "HEADQUARTERED_IN", "target_class": "City", "functional": True,
         "cue_phrases": ["headquartered in", "based in"]},
        {"source_class": "Person", "predicate": "WORKS_AT", "target_class": "Company", "functional": True,
         "cue_phrases": ["works at", "employed by"]},
        {"source_class": "Person", "predicate": "LEADS", "target_class": "Project",
         "cue_phrases": ["leads"], "inverse_cue_phrases": ["led by"]},
        {"source_class": "Company", "predicate": "PARTNERS_WITH", "target_class": "Company", "symmetric": True,
         "cue_phrases": ["partners with", "partnered with"]},
        {"source_class": "Project", "predicate": "DEVELOPED_BY", "target_class": "Company",
         "cue_phrases": ["built by", "developed by"]},
        {"source_class": "Company", "predicate": "SUPPLIES", "target_class": "Project",
         "cue_phrases": ["supplies"]},
    ],
    "alias_rules": [{"canonical": "Acme Robotics", "aliases": ["Acme"], "entity_class": "Company"}],
}

# Gate 1 edits a reviewer applies on top of the auto-proposed structured mappings.
GATE1_CLASS_RENAMES = {"Employee": "Person"}
GATE1_PREDICATE_RENAMES = {"HAS_COMPANY": "WORKS_AT", "HAS_LEAD": "LED_BY", "HAS_PARTNER": "PARTNERS_WITH",
                           "HAS_USED_BY": "USED_BY"}


def _pdf_text(path: Path, pages: list[str]) -> None:
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(pymupdf.Rect(60, 60, 535, 800), text, fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()


def _pdf_scanned(path: Path, pages: list[str]) -> None:
    """Render text to raster images and build a PDF with *no* text layer."""
    import pymupdf

    out = pymupdf.open()
    for text in pages:
        tmp = pymupdf.open()
        p = tmp.new_page(width=595, height=842)
        p.insert_textbox(pymupdf.Rect(60, 80, 535, 800), text, fontsize=16, fontname="helv")
        pix = p.get_pixmap(dpi=96)
        tmp.close()
        page = out.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
    out.save(str(path))
    out.close()


def _chart_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (480, 300), "white")
    d = ImageDraw.Draw(img)
    d.line([(40, 260), (460, 260)], fill="black", width=2)
    d.line([(40, 20), (40, 260)], fill="black", width=2)
    for i, h in enumerate([120, 180, 90, 210, 150]):
        x = 70 + i * 75
        d.rectangle([x, 260 - h, x + 40, 260], fill=(54, 98, 160), outline="black")
    d.text((150, 275), "Atlas robots per site", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _diagram(path: Path, tweak: bool = False) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 520), (250, 250, 250))
    d = ImageDraw.Draw(img)
    boxes = [((60, 60, 300, 160), "Atlas Control Plane"), ((360, 60, 600, 160), "Fleet Scheduler"),
             ((660, 60, 860, 160), "Vision Sensors"), ((360, 320, 600, 420), "Robot Fleet")]
    for (x0, y0, x1, y1), label in boxes:
        d.rectangle([x0, y0, x1, y1], outline=(30, 30, 30), width=3, fill=(225, 232, 245))
        d.text((x0 + 18, y0 + 40), label, fill=(10, 10, 10))
    for a, b in [((300, 110), (360, 110)), ((600, 110), (660, 110)), ((480, 160), (480, 320))]:
        d.line([a, b], fill=(30, 30, 30), width=3)
    if tweak:
        img.putpixel((5, 5), (240, 240, 240))
        img.putpixel((6, 5), (241, 240, 240))
    img.save(path, format="PNG")


def _notebook(path: Path) -> None:
    chart = base64.b64encode(_chart_png()).decode()
    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                     "language_info": {"name": "python"}},
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Atlas Fleet Analysis\n"]},
            {"cell_type": "markdown", "metadata": {},
             "source": ["This notebook studies telemetry from Project Atlas, which was developed by Acme Robotics ",
                        "with simulation support from Globex Dynamics.\n"]},
            {"cell_type": "code", "metadata": {}, "execution_count": 1, "outputs": [],
             "source": ["import pandas as pd\n", "import matplotlib.pyplot as plt\n", "from sklearn.cluster import KMeans\n"]},
            {"cell_type": "code", "metadata": {}, "execution_count": 2,
             "source": ["df = pd.read_csv('telemetry.csv')\n", "df.groupby('site').size().plot(kind='bar')\n"],
             "outputs": [{"output_type": "display_data", "metadata": {},
                          "data": {"image/png": chart, "text/plain": ["<Figure size 480x300>"]}}]},
        ],
    }
    path.write_text(json.dumps(nb, indent=1), encoding="utf-8")


def _vault(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Elena Voss.md").write_text(
        "---\ntype: Person\naliases: [Dr. Elena Voss, E. Voss]\nrole: Chief Scientist\n---\n"
        "# Elena Voss\n\nElena leads research at [[Acme Robotics]] on multi-robot coordination.\n"
        "works_on:: [[Project Atlas]]\n\nShe mentors [[Priya Raman]] on planning benchmarks.\n", encoding="utf-8")
    (root / "Acme Robotics.md").write_text(
        "---\ntype: Company\naliases: [Acme]\n---\n# Acme Robotics\n\nWarehouse robotics firm.\n"
        "partner:: [[Globex Dynamics]]\n", encoding="utf-8")
    (root / "Project Atlas.md").write_text(
        "---\ntype: Project\n---\n# Project Atlas\n\nWarehouse autonomy programme led by [[Elena Voss]].\n",
        encoding="utf-8")


def generate(out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    _pdf_text(out / "report.pdf", REPORT_PAGES)
    _pdf_scanned(out / "scanned_memo.pdf", SCANNED_PAGES)
    (out / "corrupt.pdf").write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog garbage \x00\x01\x02 trailer")
    (out / "empty.pdf").write_bytes(b"")

    with open(out / "companies.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company_id", "name", "founded", "hq_city", "description"])
        w.writerows(COMPANIES)

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "employees"
    ws.append(["employee_id", "name", "company_id", "title", "start_date", "bio"])
    for row in EMPLOYEES:
        ws.append(list(row))
    wb.save(out / "employees.xlsx")

    (out / "projects.json").write_text(json.dumps(PROJECTS, indent=2), encoding="utf-8")
    (out / "taxonomy.yaml").write_text(TAXONOMY_YAML, encoding="utf-8")
    _notebook(out / "analysis.ipynb")
    _vault(out / "vault")
    _diagram(out / "diagram.png")
    _diagram(out / "diagram_copy.png", tweak=True)

    import docx

    d = docx.Document()
    d.add_heading("Board Brief", level=1)
    d.add_paragraph("Tomas Novak works at Initech Labs and reviews every sensor shipment for Project Borealis.")
    d.save(out / "brief.docx")

    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Roadmap 2025"
    slide.placeholders[1].text = "Project Atlas expands to Paris with Globex Dynamics as integration partner."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Risks"
    tb = s2.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
    tb.text_frame.text = "Battery supply for Project Borealis remains the main schedule risk."
    prs.save(out / "deck.pptx")

    (out / "about.html").write_text(
        "<html><head><title>About Globex</title><script>var x=1;</script></head><body><nav>Home | About</nav>"
        "<article><h1>About Globex Dynamics</h1><p>Globex Dynamics is headquartered in Paris and employs four hundred "
        "simulation engineers.</p><p>Its digital twins are used by warehouse operators across Europe to plan robot "
        "fleets before deployment.</p></article></body></html>", encoding="utf-8")

    for p in sorted(out.rglob("*")):
        if p.is_file():
            files[p.relative_to(out).as_posix()] = str(p)
    (out / "README.txt").write_text("Synthetic KumoKG fixture corpus (Helios robotics). Generated by tests/generate_fixtures.py.\n"
                                     "Contains every modality KumoKG routes plus deliberate failure cases for the verifier.\n",
                                     encoding="utf-8")
    files["README.txt"] = str(out / "README.txt")
    return files


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "synthetic_corpus")
    print(json.dumps(generate(target), indent=2))

"""Validate saved outputs and build an offline notebook preview for visual QA."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

import nbformat
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def prose(source):
    """Small preview renderer for the notebook's headings/prose; no remote scripts."""
    text = html.escape(source)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    lines = []
    for paragraph in text.split("\n\n"):
        if paragraph.startswith("### "):
            lines.append(f"<h3>{paragraph[4:]}</h3>")
        elif paragraph.startswith("## "):
            lines.append(f"<h2>{paragraph[3:]}</h2>")
        elif paragraph.startswith("# "):
            lines.append(f"<h1>{paragraph[2:]}</h1>")
        else:
            lines.append(f"<p>{paragraph}</p>")
    return "\n".join(lines)


def preview(notebook, destination):
    parts = ["""<!doctype html><html lang="vi"><meta charset="utf-8">
<title>CAMS EAC4 — executed notebook preview</title>
<style>body{font:16px/1.5 Arial,sans-serif;color:#24313c;background:#f6f7f8;margin:0}
main{max-width:1180px;margin:24px auto;padding:28px;background:white}h1{font-size:28px}
h2{border-bottom:1px solid #ddd;padding-bottom:8px;margin-top:40px}p{white-space:pre-wrap}
img{display:block;max-width:100%;height:auto;margin:18px auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}
details{background:#f5f6f8;padding:10px;margin:12px 0}.table{overflow:auto;margin:16px 0}
table{border-collapse:collapse;font-size:13px}th,td{padding:6px;border-bottom:1px solid #ddd;text-align:right}
code{background:#f1f3f4;padding:2px 4px}.stream{font-size:13px;color:#556}
</style><main>"""]
    for index, cell in enumerate(notebook.cells):
        if cell.cell_type == "markdown":
            parts.append(prose(cell.source))
        else:
            parts.append(f"<details><summary>Code cell {index + 1}</summary><pre>{html.escape(cell.source)}</pre></details>")
            for output in cell.get("outputs", []):
                data = output.get("data", {})
                if "image/png" in data:
                    parts.append(f'<img alt="Executed notebook figure" src="data:image/png;base64,{data["image/png"]}">')
                elif "text/html" in data:
                    parts.append(f'<div class="table">{data["text/html"]}</div>')
                elif "text/markdown" in data:
                    parts.append(prose(data["text/markdown"]))
                elif "text/plain" in data:
                    parts.append(f'<pre>{html.escape(data["text/plain"])}</pre>')
                elif output.output_type == "stream":
                    parts.append(f'<pre class="stream">{html.escape(output.text)}</pre>')
    parts.append("</main></html>")
    destination.write_text("\n".join(parts), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pm1-only", action="store_true")
    args = parser.parse_args()
    root = ROOT / "reports" / "validation_pm1" if args.pm1_only else ROOT / "reports"
    source = root / "01_cams_eac4_eda_pm1.ipynb" if args.pm1_only else ROOT / "notebooks" / "01_cams_eac4_eda.ipynb"
    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    code_cells = [c for c in notebook.cells if c.cell_type == "code"]
    assert all(c.execution_count is not None for c in code_cells), "Unexecuted code cells"
    outputs = [o for c in code_cells for o in c.get("outputs", [])]
    assert not any(o.output_type == "error" for o in outputs), "Notebook contains execution errors"
    report = (root / "cams_eac4_eda_report.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\((figures/[^)]+|tables/[^)]+)\)", report)
    assert all((root / p).is_file() for p in links), "Broken local report link"
    assert len(re.findall(r"^## \d+\.", report, re.M)) == 10, "Expected ten report sections"
    inventory = pd.read_csv(root / "tables" / "inventory.csv")
    if args.pm1_only:
        assert (inventory.status == "available").sum() == 1
        assert (inventory.status == "missing").sum() == 13
    images = sum("image/png" in o.get("data", {}) for o in outputs)
    assert images > 0, "Missing rendered notebook figures"
    destination = root / "notebook_preview.html"
    preview(notebook, destination)
    checks = {"notebook": str(source), "executed_code_cells": len(code_cells), "notebook_image_outputs": images, "execution_errors": 0, "report_sections": 10, "local_report_links_checked": len(links), "available_variables": inventory[inventory.status == "available"].short_name.tolist(), "missing_variables": inventory[inventory.status == "missing"].short_name.tolist(), "html_preview": str(destination)}
    (root / "artifact_validation.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

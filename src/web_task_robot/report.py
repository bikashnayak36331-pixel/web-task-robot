"""A self-contained HTML report for each run (open it in any browser)."""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import RunResult

STYLE = """
body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2933;margin:28px;background:#f8fafc}
h1{font-size:20px;margin:0 0 4px}.muted{color:#616e7c}.cards{display:flex;gap:12px;margin:18px 0}
.card{background:#fff;border-radius:8px;padding:12px 18px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:110px}
.card b{display:block;font-size:24px}table{border-collapse:collapse;background:#fff;width:100%;
box-shadow:0 1px 3px rgba(0,0,0,.08)}th{background:#1f2933;color:#fff;text-align:left;padding:7px 9px;font-size:12px}
td{padding:6px 9px;border-bottom:1px solid #e4e7eb;font-size:13px;vertical-align:top}
.done{background:#d1f2e3}.failed{background:#f8d7da}.skipped{background:#eceff1}
img{max-width:180px;border:1px solid #cbd2d9;border-radius:4px}
.note{background:#fff4ce;border:1px solid #c99a2e;border-radius:6px;padding:10px 14px;margin:0 0 16px}
"""


def write_report(result: RunResult, columns: list[str]):
    job = result.job
    path = result.out_dir / "report.html"
    cards = [
        ("Rows", len(result.rows)),
        ("Done", result.count("done")),
        ("Failed", result.count("failed")),
        ("Skipped", result.count("skipped")),
        ("Time", f"{result.seconds}s"),
    ]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = []
    for r in result.rows:
        cells = []
        for c in columns:
            v = r.get(c, "")
            if c == "screenshot" and v:
                cells.append(
                    f'<td><a href="{html.escape(str(v))}"><img src="{html.escape(str(v))}" alt="screenshot"></a></td>'
                )
            elif c == "status":
                cells.append(f'<td class="{html.escape(str(v))}">{html.escape(str(v))}</td>')
            else:
                cells.append(f"<td>{html.escape(str(v))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    note = f'<div class="note">{html.escape(result.note)}</div>' if result.note else ""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(job.name)} · run report</title>
<style>{STYLE}</style></head><body>
<h1>{html.escape(job.name)}</h1>
<div class="muted">{job.type} job · {html.escape(job.path.name)} · finished {datetime.now():%Y-%m-%d %H:%M:%S} ·
output: {html.escape(result.output.name if result.output else "")}</div>
<div class="cards">{"".join(f'<div class="card"><span class="muted">{k}</span><b>{v}</b></div>' for k, v in cards)}</div>
{note}<table><tr>{head}</tr>{"".join(body)}</table></body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path

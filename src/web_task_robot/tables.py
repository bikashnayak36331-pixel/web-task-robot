"""Read input rows from CSV/Excel and write results to CSV, Excel or JSON."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import JobError

STATUS_FILL = {
    "done": PatternFill("solid", fgColor="D1F2E3"),
    "failed": PatternFill("solid", fgColor="F8D7DA"),
    "skipped": PatternFill("solid", fgColor="E8EAED"),
}


def read_rows(path: Path, sheet: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    if not path.exists():
        raise JobError(f"Input file not found: {path}")
    if path.suffix.lower() == ".csv":
        text = _decode(path.read_bytes())
        first_line = text.split("\n", 1)[0]
        # Excel in many countries saves "CSV" with semicolons; tab-separated files are common too.
        delimiter = max((",", ";", "\t"), key=first_line.count) if first_line else ","
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = [{h.strip(): (v or "").strip() for h, v in r.items() if isinstance(h, str) and h} for r in reader]
    else:
        wb = load_workbook(path, read_only=True, data_only=True)
        if sheet and sheet not in wb.sheetnames:
            raise JobError(f"Sheet '{sheet}' not found in {path.name}. Sheets: {', '.join(wb.sheetnames)}")
        ws = wb[sheet] if sheet else wb.worksheets[0]
        values = list(ws.iter_rows(values_only=True))
        wb.close()
        if not values:
            return [], []
        headers = [str(h).strip() if h is not None else "" for h in values[0]]
        rows = [
            {h: ("" if v is None else v) for h, v in zip(headers, r, strict=False) if h}
            for r in values[1:]
            if any(v not in (None, "") for v in r)
        ]
        headers = [h for h in headers if h]
    rows = [r for r in rows if any(str(v).strip() for v in r.values())]  # skip blank lines
    if not headers:
        raise JobError(f"{path.name} has no header row")
    return headers, rows


def _decode(data: bytes) -> str:
    """UTF-8 (with or without a BOM), or the Windows encoding that older Excel versions use for "CSV"."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _xlsx_value(value: Any) -> Any:
    """Make a value safe for a worksheet: no control characters, and text is never turned into a formula."""
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


def write_rows(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = [{h: r.get(h, "") for h in headers} for r in rows]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    elif suffix == ".csv":
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    elif suffix == ".xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Results"
        ws.append(headers)
        for r in rows:
            ws.append([_xlsx_value(r.get(h, "")) for h in headers])
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if cell.data_type == "f":  # scraped text such as "=HYPERLINK(...)" stays text
                    cell.data_type = "s"
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F2933")
        if "status" in headers:
            col = headers.index("status") + 1
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                fill = STATUS_FILL.get(str(row[0].value))
                if fill:
                    row[0].fill = fill
        for i, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r.get(h, ""))) for r in rows[:500]])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(8, width + 2))
        ws.freeze_panes = "A2"
        wb.save(path)
    else:
        raise JobError(f"Output must end in .csv, .xlsx or .json, not {path.suffix}")
    return path

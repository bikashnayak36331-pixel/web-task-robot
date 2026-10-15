"""Run jobs in a real Chromium browser with Playwright."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .config import Job, JobError, Step, check_columns, check_env, render, truthy
from .report import write_report
from .tables import read_rows, write_rows

Progress = Callable[[str], None]


class RowFailed(Exception):
    """A row failed. `retryable` is False for problems a retry cannot fix (e.g. a validation error)."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class RunResult:
    job: Job
    out_dir: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    output: Path | None = None
    report: Path | None = None
    seconds: float = 0.0

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.get("status") == status)

    @property
    def ok(self) -> bool:
        return self.count("failed") == 0


class _DocStatus:
    """Remembers the HTTP status of the last page (document) the main frame loaded."""

    def __init__(self, page: Page):
        self.status = 200
        self.url = ""
        page.on("response", self._on_response)
        self.page = page

    def _on_response(self, response) -> None:
        req = response.request
        if req.resource_type == "document" and response.frame == self.page.main_frame:
            self.status, self.url = response.status, response.url


def _check_server(doc: _DocStatus) -> None:
    if doc.status >= 500:
        raise RowFailed(f"The site returned HTTP {doc.status} for {doc.url}", retryable=True)
    if doc.status == 429:
        raise RowFailed("The site says too many requests (HTTP 429)", retryable=True)


_INVALID_JS = """() => {
  const el = document.querySelector('form :invalid');
  return el ? (el.id ? '#' + el.id : el.name || el.tagName.toLowerCase()) + ': ' + el.validationMessage : null;
}"""


def _check_error(page: Page, job: Job) -> None:
    """Stop early (no retries) when the site rejects the data: an error message or a browser validation error."""
    if job.error:
        loc = page.locator(job.error)
        if loc.count() and loc.first.is_visible():
            raise RowFailed("Site said: " + " ".join(loc.first.inner_text().split()), retryable=False)
    invalid = page.evaluate(_INVALID_JS)
    if invalid:
        raise RowFailed("Form not accepted by the browser: " + invalid, retryable=False)


def run_steps(
    page: Page, steps: list[Step], row: dict[str, Any] | None, job: Job, doc: _DocStatus, shots_dir: Path | None = None
) -> dict[str, Any]:
    """Run browser steps for one row; returns any values captured with `extract`."""
    captured: dict[str, Any] = {}
    opts = job.options
    for step in steps:
        a = step.args
        sel = render(a["selector"], row) if "selector" in a else None
        try:
            if step.kind == "goto":
                page.goto(render(a["url"], row), wait_until="domcontentloaded")
                _check_server(doc)
            elif step.kind in ("fill", "type"):
                value = render(a["value"], row)
                if step.kind == "type" or opts.typing_delay_ms:
                    loc = page.locator(sel)
                    loc.fill("")
                    loc.press_sequentially(value, delay=opts.typing_delay_ms or 40)
                else:
                    page.fill(sel, value)
            elif step.kind == "select":
                page.select_option(sel, render(a["value"], row))
            elif step.kind in ("check", "uncheck"):
                when = a.get("when")
                wanted = True if when is None else truthy(render(when, row))
                if step.kind == "check":
                    page.set_checked(sel, wanted)
                elif wanted:
                    page.uncheck(sel)
            elif step.kind in ("click", "press"):
                if step.kind == "click":
                    page.click(sel)
                else:
                    page.press(sel, render(a["key"], row))
                page.wait_for_load_state("domcontentloaded")
                _check_server(doc)
                _check_error(page, job)
            elif step.kind == "wait_for":
                page.wait_for_selector(sel, state=a.get("state", "visible"))
            elif step.kind == "wait_ms":
                page.wait_for_timeout(a["ms"])
            elif step.kind == "extract":
                loc = page.locator(sel).first
                captured[str(a["as"])] = (
                    loc.get_attribute(a["attr"]) if a.get("attr") else " ".join(loc.inner_text().split())
                )
            elif step.kind == "assert_text":
                text = page.locator(sel).first.inner_text()
                expected = render(a["contains"], row)
                if expected not in text:
                    raise RowFailed(
                        f"Expected '{expected}' in {sel}, found '{' '.join(text.split())[:80]}'", retryable=False
                    )
            elif step.kind == "screenshot" and shots_dir is not None:
                shots_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots_dir / f"{render(a['name'], row)}.png"))
        except RowFailed:
            raise
        except PlaywrightError as e:
            _check_error(page, job)  # a visible site error explains the failure better than a timeout
            first = str(e).strip().splitlines()[0]
            raise RowFailed(f"{step.where} {step.kind}: {first}") from None
    return captured


def _login(page: Page, job: Job, doc: _DocStatus) -> None:
    if not job.login_url:
        return
    page.goto(render(job.login_url), wait_until="domcontentloaded")
    _check_server(doc)
    try:
        run_steps(page, job.login, None, job, doc)
    except RowFailed as e:
        raise JobError(f"Login failed: {e}") from None


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", name)[:80] or "row"


def _launch(p, job: Job, video_dir: Path | None):
    browser = p.chromium.launch(headless=job.options.headless, slow_mo=job.options.slow_mo_ms)
    kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
    if video_dir:
        kwargs["record_video_dir"] = str(video_dir)
        kwargs["record_video_size"] = {"width": 1280, "height": 800}
    context = browser.new_context(**kwargs)
    page = context.new_page()
    page.set_default_timeout(job.options.timeout_ms)
    return browser, context, page


def run_fill(
    job: Job,
    out_dir: Path,
    *,
    limit: int | None = None,
    fresh: bool = False,
    progress: Progress = print,
    video_dir: Path | None = None,
) -> RunResult:
    headers, rows = read_rows(job.input, job.sheet)
    check_columns(job, headers)
    check_env(job)
    if limit:
        rows = rows[:limit]
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = out_dir / "screenshots"
    checkpoint_path = out_dir.parent / "checkpoint.json"
    done: dict[str, Any] = {}
    if fresh and checkpoint_path.exists():
        checkpoint_path.unlink()
    elif checkpoint_path.exists():
        done = json.loads(checkpoint_path.read_text(encoding="utf-8"))

    result = RunResult(job, out_dir)
    extracted_cols: list[str] = []
    start = time.monotonic()
    with sync_playwright() as p:
        browser, context, page = _launch(p, job, video_dir)
        doc = _DocStatus(page)
        try:
            _login(page, job, doc)
            seen: dict[str, int] = {}
            for n, row in enumerate(rows, 1):
                key = str(row[job.key]) if job.key else f"row-{n}"
                label = f"[{n:>3}/{len(rows)}] {key}"
                if job.key and key in seen:
                    result.rows.append(
                        {
                            **row,
                            "status": "skipped",
                            "attempts": 0,
                            "seconds": 0,
                            "message": f"Same {job.key} as row {seen[key]} of the input file",
                        }
                    )
                    progress(f"{label}: skipped (duplicate of row {seen[key]})")
                    continue
                seen[key] = n
                if key in done:
                    result.rows.append(
                        {
                            **row,
                            "status": "skipped",
                            "message": "Done in an earlier run",
                            "attempts": 0,
                            "seconds": 0,
                            **done[key],
                        }
                    )
                    progress(f"{label}: skipped (done earlier)")
                    continue
                t0 = time.monotonic()
                status, message, captured, attempts = "failed", "", {}, 0
                for attempts in range(1, job.options.retries + 2):
                    try:
                        captured = run_steps(page, job.steps, row, job, doc, shots)
                        if job.success:
                            page.wait_for_selector(job.success, state="visible")
                        status, message = "done", ""
                        break
                    except RowFailed as e:
                        message = str(e)
                        if not e.retryable or attempts > job.options.retries:
                            break
                        progress(f"{label}: attempt {attempts} failed ({message}); retrying")
                        page.wait_for_timeout(job.options.retry_delay_ms)
                    except PlaywrightError as e:
                        message = "Success check: " + str(e).strip().splitlines()[0]
                        if attempts > job.options.retries:
                            break
                        page.wait_for_timeout(job.options.retry_delay_ms)
                shot = ""
                mode = job.options.screenshots
                if mode == "each" or (mode == "failure" and status == "failed"):
                    shots.mkdir(parents=True, exist_ok=True)
                    shot_path = shots / f"{n:04d}-{_safe(key)}-{status}.png"
                    page.screenshot(path=str(shot_path), full_page=True)
                    shot = str(shot_path.relative_to(out_dir))
                for k in captured:
                    if k not in extracted_cols:
                        extracted_cols.append(k)
                secs = round(time.monotonic() - t0, 2)
                result.rows.append(
                    {
                        **row,
                        **captured,
                        "status": status,
                        "message": message,
                        "attempts": attempts,
                        "seconds": secs,
                        "screenshot": shot,
                    }
                )
                if status == "done":
                    done[key] = captured
                    tmp = checkpoint_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(done, indent=1), encoding="utf-8")
                    tmp.replace(checkpoint_path)  # atomic: a crash never leaves a half-written checkpoint
                progress(f"{label}: {status}" + (f" ({message})" if message else "") + f" in {secs}s")
                if job.options.delay_ms and n < len(rows):
                    page.wait_for_timeout(job.options.delay_ms)
        finally:
            context.close()
            browser.close()
    result.seconds = round(time.monotonic() - start, 1)
    cols = headers + extracted_cols + ["status", "message", "attempts", "seconds", "screenshot"]
    result.output = write_rows(job.output or out_dir / "results.xlsx", cols, result.rows)
    result.report = write_report(result, cols)
    return result


def _parse(value: str | None, how: str) -> Any:
    if value is None:
        return ""
    value = " ".join(value.split())
    if how in ("number", "int"):
        m = re.search(r"-?\d[\d,]*\.?\d*", value)
        if not m:
            return ""
        n = float(m.group(0).replace(",", ""))
        return int(n) if how == "int" else n
    return value


def run_extract(job: Job, out_dir: Path, *, progress: Progress = print, video_dir: Path | None = None) -> RunResult:
    check_env(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(job, out_dir)
    start = time.monotonic()
    with sync_playwright() as p:
        browser, context, page = _launch(p, job, video_dir)
        doc = _DocStatus(page)
        try:
            _login(page, job, doc)
            page.goto(render(job.start_url), wait_until="domcontentloaded")
            _check_server(doc)
            for page_no in range(1, job.max_pages + 1):
                page.wait_for_selector(job.item, state="attached")
                items = page.locator(job.item).all()
                for item in items:
                    row: dict[str, Any] = {}
                    for name, spec in job.fields.items():
                        target = item.locator(spec["selector"]).first if spec.get("selector") else item
                        if spec.get("selector") and target.count() == 0:
                            row[name] = ""
                            continue
                        raw = target.get_attribute(spec["attr"]) if spec.get("attr") else target.inner_text()
                        if spec.get("absolute") and raw:
                            raw = urljoin(page.url, raw)
                        row[name] = _parse(raw, spec.get("parse", "text"))
                    row["page"] = page_no
                    result.rows.append(row)
                progress(f"page {page_no}: {len(items)} items ({len(result.rows)} total)")
                if not job.next_page or page.locator(job.next_page).count() == 0:
                    break
                if page_no == job.max_pages:
                    progress(f"Stopped at max_pages={job.max_pages}")
                    break
                page.wait_for_timeout(job.options.delay_ms)
                page.locator(job.next_page).first.click()
                page.wait_for_load_state("domcontentloaded")
                _check_server(doc)
        finally:
            context.close()
            browser.close()
    result.seconds = round(time.monotonic() - start, 1)
    for r in result.rows:
        r["status"] = "done"
    cols = list(job.fields) + ["page"]
    result.output = write_rows(job.output or out_dir / "results.xlsx", cols, result.rows)
    result.report = write_report(result, cols)
    return result


def run_job(job: Job, out_root: Path, **kwargs) -> RunResult:
    out_dir = out_root / job.slug / datetime.now().strftime("%Y%m%d-%H%M%S")
    if job.type == "fill":
        return run_fill(job, out_dir, **kwargs)
    kwargs.pop("limit", None)
    kwargs.pop("fresh", None)
    return run_extract(job, out_dir, **kwargs)

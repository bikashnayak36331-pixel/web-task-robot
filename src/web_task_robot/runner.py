"""Run jobs in a real Chromium browser with Playwright."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .config import Job, JobError, Step, cell_text, check_columns, check_env, render, truthy
from .report import write_report
from .tables import read_rows, write_rows

Progress = Callable[[str], None]

NOT_RETRIED = " (not retried: the form may already have been sent; check the site)"


class RowFailed(Exception):
    """A row failed.

    `retryable` is False for problems a retry cannot fix (e.g. a validation error).
    `server` is True when the site itself answered with an error page (HTTP 5xx or 429): the request was refused,
    so it is safe to try again even after a form was sent.
    """

    def __init__(self, message: str, retryable: bool = True, server: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.server = server


@dataclass
class RunResult:
    job: Job
    out_dir: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    output: Path | None = None
    report: Path | None = None
    seconds: float = 0.0
    note: str = ""  # e.g. "Stopped with Ctrl+C after 4 of 10 rows"
    stopped: bool = False  # the user pressed Ctrl+C
    error: str = ""  # an extract job that could not finish

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.get("status") == status)

    @property
    def ok(self) -> bool:
        return self.count("failed") == 0 and not self.error and not self.stopped


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
        raise RowFailed(f"The site returned HTTP {doc.status} for {doc.url}", retryable=True, server=True)
    if doc.status == 429:
        raise RowFailed("The site says too many requests (HTTP 429)", retryable=True, server=True)


_INVALID_JS = """() => {
  const el = document.querySelector('form :invalid');
  return el ? (el.id ? '#' + el.id : el.name || el.tagName.toLowerCase()) + ': ' + el.validationMessage : null;
}"""


def _visible(selector: str) -> str:
    return f"{selector} >> visible=true"


def _check_error(page: Page, job: Job) -> None:
    """Stop early (no retries) when the site rejects the data: an error message or a browser validation error."""
    try:
        if job.error:
            loc = page.locator(_visible(job.error))
            if loc.count():
                raise RowFailed("Site said: " + " ".join(loc.first.inner_text().split()), retryable=False)
        invalid = page.evaluate(_INVALID_JS)
    except PlaywrightError:
        return  # the page is navigating or gone; the caller reports the original problem
    if invalid:
        raise RowFailed("Form not accepted by the browser: " + invalid, retryable=False)


# Playwright errors worth another try: the site was slow or unreachable, or the page crashed.
_TRANSIENT = (
    "Timeout",
    "net::ERR_",
    "NS_ERROR_",
    "has been closed",
    "crashed",
    "Navigation failed",
    "interrupted",
    "not attached",
    "detached",
    "Execution context was destroyed",
)


def _transient(message: str) -> bool:
    return any(t in message for t in _TRANSIENT)


_KEYS = {
    k.lower(): k
    for k in (
        "Enter Tab Escape Backspace Delete Insert Home End PageUp PageDown ArrowUp ArrowDown ArrowLeft ArrowRight "
        "Space Shift Control Alt Meta ControlOrMeta F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F12"
    ).split()
}
_KEYS.update(esc="Escape", ctrl="Control", cmd="Meta", up="ArrowUp", down="ArrowDown")
_KEYS["return"] = "Enter"


def _key(name: str) -> str:
    """'enter' or 'ENTER' -> 'Enter', 'ctrl+a' -> 'Control+a' (Playwright key names are case-sensitive)."""
    return "+".join(_KEYS.get(part.lower(), part) for part in name.split("+")) if name != "+" else name


def _upload_path(job: Job, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = job.path.parent / path
    if not path.is_file():
        raise RowFailed(f"File to upload not found: {path}", retryable=False)
    return path.resolve()


def run_steps(
    page: Page,
    steps: list[Step],
    row: dict[str, Any] | None,
    job: Job,
    doc: _DocStatus,
    shots_dir: Path | None = None,
    sent: list[bool] | None = None,
) -> dict[str, Any]:
    """Run browser steps for one row; returns any values captured with `extract`.

    `sent[0]` becomes True once a click or key press has been made: from then on the form may have reached the
    site, so the row is only retried if the site answers with an error page (see run_fill).
    """
    captured: dict[str, Any] = {}
    opts = job.options
    sent = sent if sent is not None else [False]
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
                    loc = page.locator(sel).first
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
                    if wanted:
                        page.check(sel)
                    elif page.locator(sel).first.get_attribute("type") != "radio":  # a radio can't be unticked
                        page.uncheck(sel)
                elif wanted:
                    page.uncheck(sel)
            elif step.kind in ("click", "press"):
                page.wait_for_selector(sel, state="visible")  # if this times out, nothing was clicked yet
                sent[0] = True
                if step.kind == "click":
                    page.click(sel)
                else:
                    page.press(sel, _key(render(a["key"], row)))
                page.wait_for_load_state("domcontentloaded")
                _check_server(doc)
                _check_error(page, job)
            elif step.kind == "upload":
                page.set_input_files(sel, str(_upload_path(job, render(a["file"], row))))
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
                page.screenshot(path=str(shots_dir / f"{_safe(render(a['name'], row))}.png"))
        except RowFailed:
            raise
        except PlaywrightError as e:
            _check_error(page, job)  # a visible site error explains the failure better than a timeout
            first = str(e).strip().splitlines()[0]
            raise RowFailed(f"{step.where} {step.kind}: {first}", retryable=_transient(first)) from None
    return captured


def _wait_for_success(page: Page, job: Job) -> None:
    """Wait for the success marker, or for the site's error message, whichever shows first."""
    target = page.locator(_visible(job.success))
    if job.error:
        target = target.or_(page.locator(_visible(job.error)))  # forms that save in the background
    try:
        target.first.wait_for(state="visible")
    except PlaywrightError as e:
        _check_error(page, job)
        first = str(e).strip().splitlines()[0]
        message = f"Success check: '{job.success}' did not appear ({first})"
        raise RowFailed(message, retryable=_transient(first)) from None
    _check_error(page, job)  # the error message appeared instead


def _login(page: Page, job: Job, doc: _DocStatus) -> None:
    if not job.login_url:
        return
    try:
        page.goto(render(job.login_url), wait_until="domcontentloaded")
        _check_server(doc)
        run_steps(page, job.login, None, job, doc)
    except RowFailed as e:
        raise JobError(f"Login failed: {e}") from None
    except PlaywrightError as e:
        raise JobError(f"Login failed: {str(e).strip().splitlines()[0]}") from None


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", name).strip(".")[:80] or "row"


class _Browser:
    """One browser, one context; opens a fresh tab if the current one crashed or was closed."""

    def __init__(self, p, job: Job, video_dir: Path | None):
        self.job = job
        # handle_sigint=False: Ctrl+C is handled by the robot, which closes the browser itself (see _CtrlC).
        self.browser = p.chromium.launch(
            headless=job.options.headless, slow_mo=job.options.slow_mo_ms, handle_sigint=False
        )
        kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        if video_dir:
            kwargs["record_video_dir"] = str(video_dir)
            kwargs["record_video_size"] = {"width": 1280, "height": 800}
        self.context = self.browser.new_context(**kwargs)
        self.dialogs: list[str] = []
        self.page, self.doc = self._new_page()

    def _new_page(self) -> tuple[Page, _DocStatus]:
        page = self.context.new_page()
        page.set_default_timeout(self.job.options.timeout_ms)
        page.on("dialog", self._on_dialog)
        return page, _DocStatus(page)

    def _on_dialog(self, dialog) -> None:
        self.dialogs.append(f"{dialog.type}: {dialog.message}")
        try:
            dialog.accept() if self.job.options.dialogs == "accept" else dialog.dismiss()
        except PlaywrightError:
            pass

    def ensure_page(self) -> None:
        if self.page.is_closed():
            self.page, self.doc = self._new_page()

    def close(self) -> None:
        for close in (self.context.close, self.browser.close):
            try:
                close()
            except PlaywrightError:
                pass


class _CtrlC:
    """First Ctrl+C: finish the current row, then stop and still write the results. Second Ctrl+C: save and quit now.

    While the browser starts, Ctrl+C is ignored, so Playwright's helper process inherits "ignore" and keeps running
    long enough for the robot to close the browser and save its files. Only possible in the main thread.
    """

    def __init__(self, progress: Progress):
        self.progress = progress
        self.requested = False
        self.active = threading.current_thread() is threading.main_thread()
        self.previous: Any = None
        self.on_quit: Callable[[], None] | None = None  # saves what is done so far

    def __enter__(self) -> _CtrlC:
        if self.active:
            self.previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self

    def arm(self) -> None:
        if self.active:
            signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum, frame) -> None:
        if not self.requested:
            self.requested = True
            self.progress("Ctrl+C: stopping after the current row and saving the results (Ctrl+C again to quit now)")
            return
        # Interrupting Playwright in the middle of a call can leave it hanging, so save and leave at once.
        # Closing our end of the pipe makes Playwright's helper process close the browser.
        try:
            if self.on_quit:
                self.on_quit()
        finally:
            sys.stdout.flush()
            os._exit(130)

    def __exit__(self, *exc) -> None:
        if self.active:
            signal.signal(signal.SIGINT, self.previous)


def _target(job: Job) -> dict[str, Any]:
    """What a checkpoint belongs to: the input file and the site(s) the job opens (scheme, host and port only)."""
    sites = set()
    for url in [job.login_url] + [s.args["url"] for s in job.steps if s.kind == "goto"]:
        try:
            parts = urlsplit(render(url))
        except JobError:
            continue
        if parts.scheme in ("http", "https") and parts.netloc and "{" not in parts.netloc:
            sites.add(f"{parts.scheme}://{parts.netloc}")
    return {"input": job.input.name if job.input else "", "sites": sorted(sites)}


def _load_checkpoint(path: Path, target: dict[str, Any]) -> dict[str, Any]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(saved, dict) and isinstance(saved.get("done"), dict) and "sites" in saved):
        return saved  # a 1.0.0 checkpoint: {key: values}
    if saved.get("sites") != target["sites"] or saved.get("input") != target["input"]:
        raise JobError(
            f"{path} is from an earlier run on {', '.join(saved.get('sites') or ['another site'])} "
            f"with {saved.get('input')}, not {', '.join(target['sites']) or 'this site'} with {target['input']}. "
            "To keep it, use another results folder (--out). To send every row again, use --fresh."
        )
    return saved["done"]


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
    target = _target(job)
    done: dict[str, Any] = {}
    if fresh and checkpoint_path.exists():
        checkpoint_path.unlink()
    elif checkpoint_path.exists():
        done = _load_checkpoint(checkpoint_path, target)

    result = RunResult(job, out_dir)
    extracted_cols: list[str] = []
    start = time.monotonic()

    def save() -> None:
        result.seconds = round(time.monotonic() - start, 1)
        cols = headers + extracted_cols + ["status", "message", "attempts", "seconds", "screenshot"]
        result.output = write_rows(job.output or out_dir / "results.xlsx", cols, list(result.rows))
        result.report = write_report(result, cols)

    def save_and_quit() -> None:
        result.stopped = True
        result.note = f"Stopped with Ctrl+C (twice) after {len(result.rows)} of {len(rows)} rows"
        save()
        progress(f"{result.note}\n  results: {result.output}\n  report:  {result.report}")

    with _CtrlC(progress) as ctrl_c, sync_playwright() as p:
        b = _Browser(p, job, video_dir)
        ctrl_c.on_quit = save_and_quit
        ctrl_c.arm()
        try:
            _login(b.page, job, b.doc)
            seen: dict[str, int] = {}
            for n, row in enumerate(rows, 1):
                if ctrl_c.requested:
                    result.stopped = True
                    result.note = f"Stopped with Ctrl+C after {n - 1} of {len(rows)} rows"
                    break
                key_value = cell_text(row[job.key]).strip() if job.key else ""
                key = key_value or f"row-{n}"  # a blank key cell can't identify a row
                label = f"[{n:>3}/{len(rows)}] {key}"
                if key_value and key in seen:
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
                    b.ensure_page()
                    sent = [False]
                    try:
                        captured = run_steps(b.page, job.steps, row, job, b.doc, shots, sent)
                        if job.success:
                            _wait_for_success(b.page, job)
                        status, message = "done", ""
                        break
                    except RowFailed as e:
                        message = str(e)
                        if e.retryable and sent[0] and not e.server:
                            message += NOT_RETRIED  # retrying could save the same record twice
                            break
                        if not e.retryable or attempts > job.options.retries or ctrl_c.requested:
                            break
                        progress(f"{label}: attempt {attempts} failed ({message}); retrying")
                        time.sleep(job.options.retry_delay_ms / 1000)
                shot = ""
                mode = job.options.screenshots
                if mode == "each" or (mode == "failure" and status == "failed"):
                    try:
                        shots.mkdir(parents=True, exist_ok=True)
                        shot_path = shots / f"{n:04d}-{_safe(key)}-{status}.png"
                        b.page.screenshot(path=str(shot_path), full_page=True)
                        shot = shot_path.relative_to(out_dir).as_posix()  # "/" works in report links on Windows
                    except PlaywrightError:
                        pass  # the page is gone; the message still says what happened
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
                    tmp.write_text(json.dumps({**target, "done": done}, indent=1), encoding="utf-8")
                    tmp.replace(checkpoint_path)  # atomic: a crash never leaves a half-written checkpoint
                progress(f"{label}: {status}" + (f" ({message})" if message else "") + f" in {secs}s")
                if job.options.delay_ms and n < len(rows) and not ctrl_c.requested:
                    time.sleep(job.options.delay_ms / 1000)
        finally:
            ctrl_c.on_quit = None
            b.close()
    save()
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


def _items_signature(page: Page, job: Job) -> str:
    try:
        return page.locator(job.item).evaluate_all("els => els.map(e => e.outerHTML).join('\\n')")
    except PlaywrightError:
        return ""


def _read_items(page: Page, job: Job, page_no: int) -> list[dict[str, Any]]:
    rows = []
    for item in page.locator(job.item).all():
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
        rows.append(row)
    return rows


def _next_page(page: Page, job: Job, doc: _DocStatus) -> bool:
    """Click the next-page link and wait until new items are shown. False if nothing changed."""
    before_url, before = page.url, _items_signature(page, job)
    page.locator(job.next_page).first.click()
    page.wait_for_load_state("domcontentloaded")
    _check_server(doc)
    # Pages that load the next items with JavaScript keep the old ones on screen for a moment.
    deadline = time.monotonic() + job.options.timeout_ms / 1000
    while time.monotonic() < deadline:
        now = _items_signature(page, job)
        if now and (now != before or page.url != before_url):
            return True
        page.wait_for_timeout(100)
    return False


def run_extract(job: Job, out_dir: Path, *, progress: Progress = print, video_dir: Path | None = None) -> RunResult:
    check_env(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(job, out_dir)
    start = time.monotonic()

    def save() -> None:
        result.seconds = round(time.monotonic() - start, 1)
        for r in result.rows:
            r["status"] = "done"
        cols = list(job.fields) + ["page"]
        result.output = write_rows(job.output or out_dir / "results.xlsx", cols, list(result.rows))
        result.report = write_report(result, cols)

    def save_and_quit() -> None:
        result.stopped = True
        result.note = f"Stopped with Ctrl+C (twice) after {len(result.rows)} items"
        save()
        progress(f"{result.note}\n  results: {result.output}\n  report:  {result.report}")

    with _CtrlC(progress) as ctrl_c, sync_playwright() as p:
        b = _Browser(p, job, video_dir)
        ctrl_c.on_quit = save_and_quit
        ctrl_c.arm()
        page, doc = b.page, b.doc
        try:
            _login(page, job, doc)
            url = render(job.start_url)
            try:
                page.goto(url, wait_until="domcontentloaded")
                _check_server(doc)
                page.wait_for_selector(job.item, state="attached")
            except (PlaywrightError, RowFailed) as e:
                first = str(e).strip().splitlines()[0]
                if isinstance(e, PlaywrightError) and "Timeout" in first:
                    first = f"no items matching '{job.item}' appeared within {job.options.timeout_ms / 1000:g}s"
                raise JobError(f"Could not read {url}: {first}") from None
            for page_no in range(1, job.max_pages + 1):
                try:
                    rows = _read_items(page, job, page_no)
                except PlaywrightError as e:
                    result.error = f"page {page_no}: {str(e).strip().splitlines()[0]}"
                    break
                result.rows.extend(rows)
                progress(f"page {page_no}: {len(rows)} items ({len(result.rows)} total)")
                if ctrl_c.requested:
                    result.stopped = True
                    result.note = f"Stopped with Ctrl+C after page {page_no}"
                    break
                if not job.next_page or page.locator(job.next_page).count() == 0:
                    break
                if page_no == job.max_pages:
                    progress(f"Stopped at max_pages={job.max_pages}")
                    break
                time.sleep(job.options.delay_ms / 1000)
                try:
                    if not _next_page(page, job, doc):
                        result.note = f"Stopped: the next-page link on page {page_no} did not show new items"
                        progress(result.note)
                        break
                except (PlaywrightError, RowFailed) as e:
                    result.error = f"page {page_no + 1}: {str(e).strip().splitlines()[0]}"
                    break
        finally:
            ctrl_c.on_quit = None
            b.close()
    if result.error:
        result.note = f"Stopped early at {result.error}. The rows collected before that are saved."
        progress(result.note)
    save()
    return result


def run_job(job: Job, out_root: Path, **kwargs) -> RunResult:
    out_dir = out_root / job.slug / datetime.now().strftime("%Y%m%d-%H%M%S")
    if job.type == "fill":
        return run_fill(job, out_dir, **kwargs)
    kwargs.pop("limit", None)
    kwargs.pop("fresh", None)
    return run_extract(job, out_dir, **kwargs)

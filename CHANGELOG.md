# Changelog

## 1.1.0 (2026-10-16)

Fixes for 26 problems found in a full end-to-end test: a clean install, every command and option, local test pages, and public practice websites.

**Reliability**
- **A form is never sent twice by a retry.** If a site took longer than `timeout_ms` to answer, 1.0.0 sent the same form up to 3 times and then reported "failed", even though the first one was saved. Now a row is only retried before anything is sent, or when the site answers with an error page (HTTP 5xx or 429). Otherwise it fails with "not retried: the form may already have been sent".
- **Forms that save in the background (JavaScript):** the site's error message is now recorded and the row is not retried. Before, it was retried and reported as "Success check: Timeout".
- **"Next" buttons that load items with JavaScript:** each page is read once. Before, 25 items came back as 50 rows with duplicates. If a next link changes nothing, the robot stops with a note.
- **Ctrl+C** finishes the current row, then writes the results and report and exits with code 130. Before, it hung and wrote nothing. Pressing Ctrl+C twice saves and quits at once.
- **Results are saved even when an extract job fails partway.** A page with no items gives a clear message instead of a crash.
- Errors that a retry can't fix (an unknown key name, a value a field refuses) are no longer retried.

**Files**
- Excel dates are typed as `2026-10-20` (1.0.0 typed `2026-10-20 00:00:00`, which date fields refuse).
- CSV files saved by Excel with semicolons, tabs or the older Windows encoding are read correctly.
- Results Excel: text from websites that starts with `=` is stored as text, not a formula, and control characters no longer crash the save (1.0.0 lost every row).
- The output file type is checked before the run starts, not after every row has been sent.
- Screenshot names that come from the data can't create folders or write outside the results folder.
- Rows with an empty key are no longer treated as duplicates of each other.
- **The checkpoint remembers which site and input file it belongs to.** In 1.0.0, running `webrobot demo` and then your own copy of the example skipped 8 rows as "done earlier" that had never been sent to your site. The same would happen after testing a job on a staging site and then running it on the live one. Now the robot stops and asks you to choose `--fresh` or another `--out` folder. `webrobot demo` also keeps its results in `out/demo`. Checkpoints written by 1.0.0 are still read.
- JSON results have the same columns as CSV and Excel.

**Job files and command line**
- New `upload` step (attach a file to a form) and `dialogs` option (accept or dismiss "Are you sure?" boxes).
- `"."` as a field selector reads the item itself.
- Key names are forgiving: `enter`, `ENTER` and `Enter` all work.
- Secrets that contain `{` and `}` are used as they are.
- Clearer checks: `max_pages`, `--limit`, `--slow-mo`, `--flaky`, `true`/`false` options, `wait_for` states, and numeric sheet names.
- `webrobot demo` always uses the practice site's own login, even if `PORTAL_USER` is set for a real portal.
- No crash on a console that can't show some characters (older Windows code pages).
- "Browser problem: …" with the install command instead of a traceback when Chromium is missing.
- README: the "Write your own job" steps now include starting the practice site and setting the variables.
- 55 tests (28 new). CI now also runs on Windows and macOS.

## 1.0.0 (2026-10-15)

First public release.

- `fill` jobs: run browser steps for every row of a CSV or Excel file (goto, fill, type, select, check, click, press, wait, extract, assert, screenshot).
- `extract` jobs: read repeated items into rows and follow "next page" links; output to CSV, Excel or JSON.
- Optional login, with secrets read from environment variables (`${VAR}`).
- Retries on timeouts and server errors; no retries when the site or the browser rejects the data, which is reported clearly.
- Resume from a checkpoint, duplicate-row detection, per-row screenshots, a colour-coded Excel file and an HTML run report.
- `webrobot demo`: a built-in practice website and two example jobs, all offline.
- 27 tests, including end-to-end runs in headless Chromium.

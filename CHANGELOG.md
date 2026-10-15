# Changelog

## 1.0.0 (2026-10-15)

First public release.

- `fill` jobs: run browser steps for every row of a CSV or Excel file (goto, fill, type, select, check, click, press, wait, extract, assert, screenshot).
- `extract` jobs: read repeated items into rows and follow "next page" links; output to CSV, Excel or JSON.
- Optional login, with secrets read from environment variables (`${VAR}`).
- Retries on timeouts and server errors; no retries when the site or the browser rejects the data, which is reported clearly.
- Resume from a checkpoint, duplicate-row detection, per-row screenshots, a colour-coded Excel file and an HTML run report.
- `webrobot demo`: a built-in practice website and two example jobs, all offline.
- 27 tests, including end-to-end runs in headless Chromium.

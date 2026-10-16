"""Command line: `webrobot run JOB.yaml`, `webrobot check JOB.yaml`, `webrobot demo-site`, `webrobot demo`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .config import JobError, check_columns, check_env, load_job
from .demo_site import DemoSite
from .tables import read_rows

EXAMPLES = Path(__file__).resolve().parent / "examples"


EXIT_STOPPED = 130  # the usual exit code after Ctrl+C


def _summary(res) -> str:
    note = f"\n  note:    {res.note}" if res.note else ""
    return (
        f"{res.job.name}: {res.count('done')} done, {res.count('failed')} failed, {res.count('skipped')} skipped "
        f"in {res.seconds}s{note}\n  results: {res.output}\n  report:  {res.report}"
    )


def _exit_code(res) -> int:
    if res.stopped:
        return EXIT_STOPPED
    return 0 if res.ok else 1


def cmd_run(args) -> int:
    from .runner import run_job  # imported late so `check` works without a browser installed

    job = load_job(args.job)
    if args.headed:
        job.options.headless = False
    if args.slow_mo is not None:
        job.options.slow_mo_ms = args.slow_mo
    res = run_job(
        job, Path(args.out), limit=args.limit, fresh=args.fresh, video_dir=Path(args.video) if args.video else None
    )
    print(_summary(res))
    return _exit_code(res)


def cmd_check(args) -> int:
    job = load_job(args.job)
    print(f"{job.path.name}: {job.type} job '{job.name}' is valid")
    if job.type == "fill":
        headers, rows = read_rows(job.input, job.sheet)
        check_columns(job, headers)
        print(f"  input: {job.input.name}, {len(rows)} rows, columns: {', '.join(headers)}")
        print(f"  steps per row: {len(job.steps)}" + (f", login steps: {len(job.login)}" if job.login else ""))
    check_env(job)
    print("  environment variables: OK")
    return 0


def cmd_examples(args) -> int:
    """Copy the example jobs and CSV into a folder so you can edit them."""
    import shutil

    dest = Path(args.folder)
    dest.mkdir(parents=True, exist_ok=True)
    for f in sorted(EXAMPLES.iterdir()):
        if f.suffix in (".yaml", ".csv"):
            target = dest / f.name
            if target.exists() and not args.force:
                print(f"  kept existing {target}")
                continue
            shutil.copy(f, target)
            print(f"  copied {target}")
    return 0


def cmd_demo_site(args) -> int:
    DemoSite(port=args.port, flaky=args.flaky).serve_forever()
    return 0


def cmd_demo(args) -> int:
    """Start the demo site and run both example jobs against it."""
    from .runner import run_job

    names = ("DEMO_URL", "PORTAL_USER", "PORTAL_PASSWORD")
    saved = {k: os.environ.get(k) for k in names}
    with DemoSite(flaky=args.flaky) as site:
        # Always the demo site's own login, even if PORTAL_USER/PORTAL_PASSWORD are set for a real portal.
        os.environ.update(DEMO_URL=site.url, PORTAL_USER=site.state.user, PORTAL_PASSWORD=site.state.password)
        try:
            print(f"Demo site: {site.url}")
            for name in ("add-customers.yaml", "collect-products.yaml"):
                job = load_job(EXAMPLES / name)
                if args.headed:
                    job.options.headless = False
                res = run_job(job, Path(args.out), fresh=True)
                print(_summary(res))
                if res.stopped:
                    return EXIT_STOPPED
            print(f"The demo site now has {len(site.state.customers)} customers.")
            print("(The 2 failed rows and the skipped duplicate are in the example data on purpose.)")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return 0


def _positive(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _not_negative(text: str) -> int:
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def _share(text: str) -> float:
    x = float(text)
    if not 0 <= x <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return x


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webrobot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a job file")
    run.add_argument("job")
    run.add_argument("--out", default="out", help="folder for results, reports and screenshots (default: out)")
    run.add_argument("--limit", type=_positive, help="only process the first N rows (good for a first test)")
    run.add_argument("--fresh", action="store_true", help="ignore the checkpoint and redo every row")
    run.add_argument("--headed", action="store_true", help="show the browser window")
    run.add_argument("--slow-mo", type=_not_negative, help="pause this many ms between browser actions")
    run.add_argument("--video", help="record a video of the run into this folder")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="validate a job file, its input columns and environment variables")
    check.add_argument("job")
    check.set_defaults(func=cmd_check)

    ex = sub.add_parser("examples", help="copy the example jobs into a folder to edit")
    ex.add_argument("folder", nargs="?", default="webrobot-jobs")
    ex.add_argument("--force", action="store_true", help="overwrite files that already exist")
    ex.set_defaults(func=cmd_examples)

    site = sub.add_parser("demo-site", help="start the practice website")
    site.add_argument("--port", type=int, default=8765)
    site.add_argument("--flaky", type=_share, default=None, help="share of form posts that fail with HTTP 503")
    site.set_defaults(func=cmd_demo_site)

    demo = sub.add_parser("demo", help="start the practice website and run the example jobs against it")
    demo.add_argument("--out", default="out")
    demo.add_argument("--headed", action="store_true")
    demo.add_argument("--flaky", type=_share, default=0.25, help="make some posts fail to show retries (default 0.25)")
    demo.set_defaults(func=cmd_demo)

    for stream in (sys.stdout, sys.stderr):  # never crash on a name the console can't show (old Windows code pages)
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except JobError as e:
        print(f"Job problem: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped (Ctrl+C).", file=sys.stderr)
        return EXIT_STOPPED
    except Exception as e:  # e.g. the browser is not installed or crashed: one clear line, not a traceback
        if type(e).__module__.startswith("playwright"):
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            print(f"Browser problem: {first}", file=sys.stderr)
            if "Executable doesn't exist" in str(e):
                print("Install the browser with:  python -m playwright install chromium", file=sys.stderr)
            return 1
        raise

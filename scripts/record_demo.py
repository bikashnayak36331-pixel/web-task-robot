"""Record the README pictures from a real run against the demo site.

  docs/images/demo.gif     the browser filling the form (from Playwright's video recording)
  docs/images/report.png   the HTML run report
  docs/demo-run.txt        the console output of the same run

Needs ffmpeg on PATH. Run: python scripts/record_demo.py
"""

import os
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from web_task_robot.config import load_job
from web_task_robot.demo_site import DemoSite
from web_task_robot.runner import run_fill

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"
EXAMPLES = ROOT / "src" / "web_task_robot" / "examples"


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, DemoSite(flaky=0.0) as site:
        tmp = Path(tmp)
        os.environ.update(DEMO_URL=site.url, PORTAL_USER=site.state.user, PORTAL_PASSWORD=site.state.password)
        job = load_job(EXAMPLES / "add-customers.yaml")
        job.options.typing_delay_ms = 18  # slow enough to watch
        job.options.slow_mo_ms = 60
        job.options.delay_ms = 400
        lines: list[str] = []
        res = run_fill(job, tmp / "out" / "run", limit=5, fresh=True, progress=lines.append, video_dir=tmp / "video")
        lines.append(f"{job.name}: {res.count('done')} done, {res.count('failed')} failed in {res.seconds}s")
        (ROOT / "docs" / "demo-run.txt").write_text("\n".join(lines) + "\n")
        video = next((tmp / "video").glob("*.webm"))
        palette = tmp / "palette.png"
        scale = "crop=1000:620:140:0,fps=8,scale=800:-1:flags=lanczos"  # the form area, larger text
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-vf",
                f"{scale},palettegen=max_colors=96",
                str(palette),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-i",
                str(palette),
                "-lavfi",
                f"{scale} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4",
                str(IMAGES / "demo.gif"),
            ],
            check=True,
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 600}, device_scale_factor=1)
            page.goto(res.report.as_uri())
            page.screenshot(path=str(IMAGES / "report.png"), full_page=True)
            browser.close()
    print("\n".join(lines))
    print(f"Saved docs/images/demo.gif ({(IMAGES / 'demo.gif').stat().st_size // 1024} KB), docs/images/report.png")


if __name__ == "__main__":
    main()

from pathlib import Path

import pytest

from web_task_robot.demo_site import DemoSite

EXAMPLES = Path(__file__).resolve().parent.parent / "src" / "web_task_robot" / "examples"


@pytest.fixture
def site(monkeypatch):
    with DemoSite(flaky=0.0) as s:
        monkeypatch.setenv("DEMO_URL", s.url)
        monkeypatch.setenv("PORTAL_USER", s.state.user)
        monkeypatch.setenv("PORTAL_PASSWORD", s.state.password)
        yield s


@pytest.fixture
def quiet():
    lines = []
    return lines.append, lines

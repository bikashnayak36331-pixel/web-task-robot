import pytest

from web_task_robot.config import JobError, check_columns, check_env, load_job, render, truthy

from .conftest import EXAMPLES


def write(tmp_path, text, name="job.yaml"):
    (tmp_path / "rows.csv").write_text("name,email\nAna,ana@x.example\n")
    p = tmp_path / name
    p.write_text(text)
    return p


def test_examples_are_valid():
    fill = load_job(EXAMPLES / "add-customers.yaml")
    assert fill.type == "fill" and fill.key == "email" and len(fill.steps) == 10 and len(fill.login) == 4
    assert fill.options.retries == 2 and fill.options.screenshots == "failure"
    ext = load_job(EXAMPLES / "collect-products.yaml")
    assert ext.type == "extract" and ext.fields["price"]["parse"] == "number" and ext.next_page == "a.next"


@pytest.mark.parametrize(
    "body, message",
    [
        ("type: fill\ninput: rows.csv\nsteps:\n  - fly: '#x'\n", "unknown action 'fly'"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - fill: {selector: '#x'}\n", "'fill' needs 'value'"),
        ("type: fill\ninput: rows.csv\nsteps: []\n", "at least one step"),
        ("type: fill\nsteps:\n  - click: '#x'\n", "needs 'input'"),
        ("type: fill\ninput: rows.txt\nsteps:\n  - click: '#x'\n", ".csv or .xlsx"),
        ("type: scrape\n", "type must be"),
        ("type: extract\nstart_url: http://x\nitem: .a\n", "needs 'fields'"),
        ("type: extract\nstart_url: http://x\nitem: .a\nfields: {p: {selector: .p, parse: money}}\n", "parse must be"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - click: '#x'\noptions: {retires: 3}\n", "options.retires"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - click: '#x'\nfoo: 1\n", "Unknown field(s)"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - click: '#x'\n    wait_for: '#y'\n", "exactly one action"),
        ("- just a list\n", "must contain a mapping"),
    ],
)
def test_helpful_errors(tmp_path, body, message):
    with pytest.raises(JobError, match=message.replace("(", r"\(").replace(")", r"\)").replace(".", r"\.")):
        load_job(write(tmp_path, body))


def test_templates_and_env():
    row = {"name": "Ana", "qty": 3.0, "blank": None}
    assert render("{name} x{qty}{blank}", row, env={}) == "Ana x3"
    assert render("${BASE}/new?n={name}", row, env={"BASE": "http://h"}) == "http://h/new?n=Ana"
    with pytest.raises(JobError, match="Environment variable SECRET is not set"):
        render("${SECRET}", row, env={})
    with pytest.raises(JobError, match="Column 'nope'"):
        render("{nope}", row, env={})
    assert truthy("Yes") and truthy("x") and not truthy("no") and not truthy("")


def test_column_and_env_checks(tmp_path, monkeypatch):
    job = load_job(
        write(
            tmp_path,
            "type: fill\ninput: rows.csv\nkey: email\nsteps:\n"
            "  - goto: ${BASE_URL}/x\n  - fill: {selector: '#p', value: '{phone}'}\n",
        )
    )
    with pytest.raises(JobError, match="no column\\(s\\): phone"):
        check_columns(job, ["name", "email"])
    monkeypatch.delenv("BASE_URL", raising=False)
    with pytest.raises(JobError, match="BASE_URL"):
        check_env(job)
    monkeypatch.setenv("BASE_URL", "http://h")
    check_env(job)

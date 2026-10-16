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
        ("type: fill\ninput: rows.csv\noutput: out.txt\nsteps:\n  - click: '#x'\n", "output must end in"),
        ("type: extract\nstart_url: http://x\nitem: .a\nfields: {a: .a}\nmax_pages: abc\n", "max_pages must be"),
        ("type: extract\nstart_url: http://x\nitem: .a\nfields: {a: .a}\nmax_pages: 0\n", "max_pages must be"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - click: '#x'\noptions: {retries: true}\n", "whole number"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - click: '#x'\noptions: {headless: maybe}\n", "true or false"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - click: '#x'\noptions: {dialogs: ignore}\n", "dismiss or accept"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - wait_for: {selector: '#x', state: gone}\n", "state must be"),
        ("type: fill\ninput: rows.csv\nsteps:\n  - upload: {selector: '#x'}\n", "'upload' needs 'file'"),
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
    # a secret that contains braces is used as it is (v1.0.0 read "{c}" in it as a column name)
    assert render("${TOKEN}-{name}", row, env={"TOKEN": "ab{c}d"}) == "ab{c}d-Ana"
    assert render("{name}", None, env={}) == "{name}"  # login steps have no row


def test_cell_text_is_what_a_person_would_type():
    from datetime import date, datetime, time

    from web_task_robot.config import cell_text

    assert cell_text(datetime(2026, 10, 20)) == "2026-10-20"
    assert cell_text(datetime(2026, 10, 20, 14, 30)) == "2026-10-20 14:30"
    assert cell_text(date(2026, 1, 2)) == "2026-01-02" and cell_text(time(9, 5)) == "09:05"
    assert cell_text(7.0) == "7" and cell_text(7.5) == "7.5" and cell_text(None) == "" and cell_text(True) == "True"


def test_options_and_names(tmp_path):
    job = load_job(
        write(
            tmp_path,
            "type: fill\ninput: rows.csv\nsheet: 2024\nkey: 7\nsteps:\n  - click: '#x'\n"
            "options: {headless: 'false', screenshots: Each, dialogs: ACCEPT}\n",
        )
    )
    assert job.options.headless is False and job.options.screenshots == "each" and job.options.dialogs == "accept"
    assert job.sheet == "2024" and job.key == "7"
    ext = load_job(write(tmp_path, "type: extract\nstart_url: http://x\nitem: li\nfields: {text: '.'}\n", "e.yaml"))
    assert ext.fields["text"] == {}  # "." reads the item itself


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

"""Job files: a small YAML format describing what the robot should do.

Two job types:
  fill     read rows from a CSV/Excel file and run the same browser steps for each row
  extract  open a page, read repeated items into rows, follow the "next page" link

Values can use {column} (from the current row) and ${ENV_VAR} (from the environment, for
passwords and other secrets, which should never be written in the job file).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STEP_TYPES = {
    "goto": "url",
    "fill": "selector+value",
    "type": "selector+value",
    "select": "selector+value",
    "check": "selector",
    "uncheck": "selector",
    "click": "selector",
    "press": "selector+key",
    "wait_for": "selector",
    "wait_ms": "number",
    "extract": "selector+as",
    "assert_text": "selector+contains",
    "screenshot": "name",
}

_COLUMN = re.compile(r"(?<!\$)\{([^{}]+)\}")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class JobError(ValueError):
    """The job file has a problem; the message says where."""


@dataclass
class Step:
    kind: str
    args: dict[str, Any]
    where: str  # e.g. "steps[3]" for error messages


@dataclass
class Options:
    retries: int = 2
    retry_delay_ms: int = 1000
    delay_ms: int = 250
    timeout_ms: int = 15000
    screenshots: str = "failure"  # failure | each | never
    headless: bool = True
    typing_delay_ms: int = 0
    slow_mo_ms: int = 0


@dataclass
class Job:
    name: str
    type: str
    path: Path
    options: Options
    login: list[Step] = field(default_factory=list)
    login_url: str = ""
    # fill
    input: Path | None = None
    sheet: str | None = None
    key: str | None = None
    steps: list[Step] = field(default_factory=list)
    success: str | None = None
    error: str | None = None
    # extract
    start_url: str = ""
    item: str = ""
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_page: str | None = None
    max_pages: int = 50
    output: Path | None = None

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "job"


def columns_used(text: str) -> set[str]:
    return set(_COLUMN.findall(text or ""))


def env_used(text: str) -> set[str]:
    return set(_ENV.findall(text or ""))


def render(template: Any, row: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> str:
    """Fill {column} from the row and ${VAR} from the environment."""
    text = "" if template is None else str(template)
    env = os.environ if env is None else env

    def env_sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in env:
            raise JobError(f"Environment variable {name} is not set")
        return env[name]

    text = _ENV.sub(env_sub, text)
    if row is not None:
        text = _COLUMN.sub(lambda m: _cell(row, m.group(1)), text)
    return text


def _cell(row: dict[str, Any], name: str) -> str:
    if name not in row:
        raise JobError(f"Column '{name}' is not in the input file")
    value = row[name]
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "x", "on", "checked")


def _parse_steps(raw: Any, where: str) -> list[Step]:
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise JobError(f"{where} must be a list of steps")
    steps = []
    for i, item in enumerate(raw):
        loc = f"{where}[{i}]"
        if not isinstance(item, dict) or len(item) != 1:
            raise JobError(f"{loc} must have exactly one action, e.g. '- click: \"#save\"'")
        kind, value = next(iter(item.items()))
        if kind not in STEP_TYPES:
            raise JobError(f"{loc}: unknown action '{kind}'. Use one of: {', '.join(STEP_TYPES)}")
        need = STEP_TYPES[kind]
        if need == "url":
            args = {"url": value}
        elif need == "number":
            if not isinstance(value, (int, float)) or value < 0:
                raise JobError(f"{loc}: wait_ms needs a number of milliseconds")
            args = {"ms": int(value)}
        elif need == "name":
            args = {"name": str(value)}
        elif isinstance(value, str):
            args = {"selector": value}
        elif isinstance(value, dict):
            args = dict(value)
        else:
            raise JobError(f"{loc}: '{kind}' needs a selector or a mapping")
        for part in need.split("+"):
            if part in ("url", "number", "name"):
                continue
            if part not in args or args[part] in (None, ""):
                raise JobError(f"{loc}: '{kind}' needs '{part}'")
        steps.append(Step(kind, args, loc))
    return steps


def _options(raw: Any) -> Options:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise JobError("options must be a mapping")
    opts = Options()
    for key, value in raw.items():
        if not hasattr(opts, key):
            raise JobError(f"options.{key} is not a known option")
        expected = type(getattr(opts, key))
        if expected is bool:
            value = bool(value)
        elif expected is int:
            if not isinstance(value, int) or value < 0:
                raise JobError(f"options.{key} must be a whole number of 0 or more")
        setattr(opts, key, value)
    if opts.screenshots not in ("failure", "each", "never"):
        raise JobError("options.screenshots must be failure, each or never")
    return opts


def load_job(path: str | Path) -> Job:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JobError(f"Job file not found: {path}") from None
    except yaml.YAMLError as e:
        raise JobError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise JobError(f"{path} must contain a mapping (name, type, ...)")
    known = {
        "name",
        "type",
        "options",
        "login",
        "input",
        "sheet",
        "key",
        "steps",
        "success",
        "error",
        "start_url",
        "item",
        "fields",
        "next_page",
        "max_pages",
        "output",
    }
    unknown = set(raw) - known
    if unknown:
        raise JobError(f"Unknown field(s) in {path.name}: {', '.join(sorted(unknown))}")
    jtype = raw.get("type")
    if jtype not in ("fill", "extract"):
        raise JobError("type must be 'fill' or 'extract'")
    base = path.parent
    job = Job(name=str(raw.get("name") or path.stem), type=jtype, path=path, options=_options(raw.get("options")))

    login = raw.get("login")
    if login:
        if not isinstance(login, dict) or "url" not in login:
            raise JobError("login needs a url and steps")
        job.login_url = str(login["url"])
        job.login = _parse_steps(login.get("steps"), "login.steps")

    if raw.get("output"):
        job.output = (base / str(raw["output"])).resolve()

    if jtype == "fill":
        if not raw.get("input"):
            raise JobError("A fill job needs 'input': a CSV or Excel file")
        job.input = (base / str(raw["input"])).resolve()
        if job.input.suffix.lower() not in (".csv", ".xlsx"):
            raise JobError("input must be a .csv or .xlsx file")
        job.sheet = raw.get("sheet")
        job.key = raw.get("key")
        job.steps = _parse_steps(raw.get("steps"), "steps")
        if not job.steps:
            raise JobError("A fill job needs at least one step")
        job.success = raw.get("success")
        job.error = raw.get("error")
    else:
        for req in ("start_url", "item", "fields"):
            if not raw.get(req):
                raise JobError(f"An extract job needs '{req}'")
        job.start_url = str(raw["start_url"])
        job.item = str(raw["item"])
        if not isinstance(raw["fields"], dict):
            raise JobError("fields must map column names to selectors")
        for name, spec in raw["fields"].items():
            spec = {"selector": spec} if isinstance(spec, str) else dict(spec or {})
            if "selector" not in spec and "attr" not in spec:
                raise JobError(f"fields.{name} needs a selector (or attr to read from the item itself)")
            if spec.get("parse", "text") not in ("text", "number", "int"):
                raise JobError(f"fields.{name}.parse must be text, number or int")
            job.fields[str(name)] = spec
        job.next_page = raw.get("next_page")
        job.max_pages = int(raw.get("max_pages", 50))
    return job


def check_columns(job: Job, headers: list[str]) -> None:
    """Fail early if a step refers to a column the input file does not have."""
    missing = []
    for step in job.steps:
        for value in step.args.values():
            missing += [c for c in columns_used(str(value)) if c not in headers]
    if job.key and job.key not in headers:
        missing.append(job.key)
    if missing:
        raise JobError(
            f"The input file has no column(s): {', '.join(sorted(set(missing)))}. Columns found: {', '.join(headers)}"
        )


def check_env(job: Job) -> None:
    """Fail early if a ${VAR} used by the job is not set."""
    needed = set()
    for step in job.login + job.steps:
        for value in step.args.values():
            needed |= env_used(str(value))
    for text in (job.login_url, job.start_url):
        needed |= env_used(text)
    missing = sorted(v for v in needed if v not in os.environ)
    if missing:
        raise JobError("Set these environment variables first: " + ", ".join(missing))

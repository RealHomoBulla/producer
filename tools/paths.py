"""The one place that knows where things live in this project, on whichever machine.

Every tool imports this instead of writing a path literal or re-reading ``producer.toml``.
Identity comes from ``producer.toml`` at the repository root; the toolchain layout
(``work/``, ``tools/``, ``.runtime/``) is fixed and derived from this file's own location,
so it is correct anywhere by construction.

Nothing here is specific to any one project: the same skeleton is dropped into a new
project directory, ``producer.toml`` is edited, and every tool follows.

    import paths
    paths.AGENT_STATE / "HANDOVER.md"
    paths.project_name()
    paths.keys_file()

``paths.PROJECT`` is the repository root (the directory holding ``producer.toml``).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from datetime import timedelta, timezone
from pathlib import Path

try:  # Python 3.11+ ships tomllib in the stdlib.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.12 is the stated floor.
    tomllib = None  # type: ignore[assignment]

# The repository root, derived from this file: tools/ -> root.  Correct anywhere by construction.
PROJECT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------ fixed toolchain layout
TOOLS = PROJECT / "tools"
RUNTIME = PROJECT / ".runtime"

# The owner's inbox: undated pages he opens himself, in his language (see PAGE_NAMES below).
WORK = PROJECT / "work"

# The agents' workbench.
AGENTS = WORK / "agents"
AGENT_STATE = AGENTS / "state"
AGENT_REGISTERS = AGENTS / "registers"
AGENT_ORCA = AGENTS / "orca"
AGENT_KNOWLEDGE = AGENTS / "knowledge"
AGENT_REPORTS = AGENTS / "reports"
SYSTEMS = WORK / "systems"

# Tool state files (one per tool that keeps durable state).
DIGEST_STATE = AGENT_STATE / "DIGEST_STATE.json"
UNANSWERED_STATE = AGENT_STATE / "UNANSWERED_STATE.json"
COMMIT_REVIEW_STATE = AGENT_STATE / "COMMIT_REVIEW_STATE.json"
COMMIT_JOURNAL = AGENT_STATE / "COMMIT_JOURNAL.md"
POLISHING_TODO = AGENT_REGISTERS / "POLISHING_TODO.md"
REPORTS_REGISTER = AGENT_REGISTERS / "REPORTS.md"
REVIEWS_DIR = AGENT_REPORTS / "reviews"
CONTEXT_INDEX = RUNTIME / "_context_index.json"
CHECKPOINT = RUNTIME / "producer-checkpoint.json"
SUPERVISOR = RUNTIME / "producer-supervisor.json"

DEFAULT_KEYS_FILE = "~/.config/producer/keys.env"

DEFAULTS = {
    "name": "producer",
    "timezone": "UTC",
    "owner_language": "ru",
    "keys_file": "",
    "archive_dir": "",
    "run": "",
    "product_globs": (),
}


def _load_toml() -> dict:
    """Read ``producer.toml``; a missing or malformed file yields the defaults."""
    path = PROJECT / "producer.toml"
    if tomllib is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(name: str) -> dict:
    value = _load_toml().get(name)
    return value if isinstance(value, dict) else {}


def project_name() -> str:
    return str(_section("project").get("name") or DEFAULTS["name"])


def timezone_name() -> str:
    return str(_section("project").get("timezone") or DEFAULTS["timezone"])


def owner_language() -> str:
    """"ru" or "en": the owner's language from producer.toml (anything not Russian reads as English)."""
    raw = str(_section("project").get("owner_language") or DEFAULTS["owner_language"]).strip().lower()
    return "ru" if raw.startswith("ru") else "en"


# Owner-page file names depend on the owner's language. Docs name the Russian file; the English
# owner's copy is the same page under the English name (setup.py --lang en renames the seeds).
PAGE_NAMES = {
    "ru": {"digest": "ДАЙДЖЕСТ.md", "unanswered": "БЕЗ_ОТВЕТА.md", "checklist": "ЧЕКЛИСТ.md",
           "brief": "БРИФ.md"},
    "en": {"digest": "DIGEST.md", "unanswered": "UNANSWERED.md", "checklist": "CHECKLIST.md",
           "brief": "BRIEF.md"},
}


def owner_page(kind: str, language: str | None = None) -> Path:
    """The owner's page of `kind` (digest/unanswered/checklist/brief) in `language` (default: configured)."""
    return WORK / PAGE_NAMES[language or owner_language()][kind]


DIGEST_PAGE = owner_page("digest")
UNANSWERED_PAGE = owner_page("unanswered")
CHECKLIST_PAGE = owner_page("checklist")
BRIEF_PAGE = owner_page("brief")


def owner_tzinfo():
    """(tzinfo, warning) for `[project] timezone`.

    Accepts an IANA name (needs tzdata on Windows: `pip install tzdata`) or a fixed offset such as
    `UTC+02:00`, `+0530` or `GMT-5`, which is what setup.py writes when the OS gives no IANA name.
    An unknown value falls back to UTC with a warning that the caller shows - never to a
    hard-coded zone.
    """
    name = timezone_name().strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), None
    except Exception:  # noqa: BLE001 - not found, bad key, or no tzdata: try the offset forms
        pass
    if name.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc, None
    match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", name, re.IGNORECASE)
    if match:
        delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        return timezone(-delta if match.group(1) == "-" else delta, name), None
    return timezone.utc, (f"timezone {name!r} is not an IANA name or a UTC offset (on Windows the "
                          "IANA names need `pip install tzdata`); using UTC")


def console_safe() -> None:
    """Never let an unencodable character crash a tool on a legacy console code page.

    Tool output contains arrows, dashes and Cyrillic; a Windows console in cp866/cp1252 raises
    UnicodeEncodeError on them. Keep the console's own encoding, replace what it cannot show.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def keys_file() -> Path:
    """The owner's keys file, expanded; empty setting falls back to the default path."""
    raw = str(_section("paths").get("keys_file") or "")
    if not raw:
        raw = DEFAULT_KEYS_FILE
    return Path(os.path.expanduser(raw))


def archive_dir() -> Path:
    """Where cleared owner pages are archived: `[paths] archive_dir`, else `.runtime/archive`.

    Inside the repository's gitignored runtime folder by default, so nothing is written outside
    the project unless the owner points it elsewhere.
    """
    raw = str(_section("paths").get("archive_dir") or "")
    return Path(os.path.expanduser(raw)) if raw else RUNTIME / "archive"


def web_dir() -> str:
    """`[web] dir`: the static product folder tools/serve.py serves (repo-relative; default `site`)."""
    return str(_section("web").get("dir") or "site")


def web_port() -> int:
    """`[web] port`: the ONE fixed dev-server port (default 8080); a stateful web app keeps one origin."""
    try:
        return int(_section("web").get("port") or 8080)
    except (TypeError, ValueError):
        return 8080


def review_reviewers() -> tuple[str, ...]:
    """`[review] reviewers`: the model families this project can actually use as commit reviewers,
    best first. Empty = the toolchain's built-in preference list (commit_review.REVIEWER_PREFERENCE).
    setup.py fills it from the agent CLIs it finds, so a stranger is never routed to an account
    they do not have."""
    value = _section("review").get("reviewers")
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def configured_run() -> str:
    """The Run id from ``producer.toml [orca] run``, or "" when unbound."""
    return str(_section("orca").get("run") or "")


def product_globs() -> tuple[str, ...]:
    """The product globs the knowledge gate watches; empty means the gate is off."""
    value = _section("knowledge").get("product_globs")
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def matches_product(path: str) -> bool:
    """Whether a repo-relative POSIX path is a watched product path."""
    globs = product_globs()
    if not globs:
        return False
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, glob) for glob in globs)


# ------------------------------------------------------------------ the one Run resolver
def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid_run(value: object) -> str:
    return value if isinstance(value, str) and value.startswith("run_") else ""


def run_sources() -> dict[str, str]:
    """Every place a Run id is recorded, in priority order (empty string = unset)."""
    checkpoint = (_read_json(CHECKPOINT).get("activeRun") or {})
    return {
        "bind": _valid_run(_read_json(SUPERVISOR).get("activeRunId")),
        "producer.toml": _valid_run(configured_run()),
        "checkpoint": _valid_run(checkpoint.get("id") if isinstance(checkpoint, dict) else ""),
    }


def resolve_run(explicit: str | None = None) -> str:
    """The ONE answer to "which Run is this project on?" for every tool.

    Order: `explicit` (a --run flag), the binding written by `producer.py bind-run` (the
    supervisor file's activeRunId), `producer.toml [orca] run`, the last checkpoint. "" = unbound;
    a tool then says so — it never guesses a Run.
    """
    if explicit:
        return explicit
    for run in run_sources().values():
        if run:
            return run
    return ""


def run_conflicts() -> list[str]:
    """Human-readable lines when the recorded Runs disagree (empty = consistent or unbound)."""
    found = {name: run for name, run in run_sources().items() if run}
    if len(set(found.values())) <= 1:
        return []
    return [f"{name}={run}" for name, run in found.items()]


# Every tool imports this module first, so a legacy console code page can never crash a tool's output
# (not even `--help`, which prints before main() runs).
console_safe()

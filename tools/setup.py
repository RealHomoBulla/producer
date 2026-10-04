#!/usr/bin/env python3
"""setup.py — first-run setup for the shareable «producer» skeleton.

A friend clones the skeleton, runs ``python tools/setup.py`` once, and gets a project that
knows its name, its language, which agent CLIs are installed, a roster the Guardian can keep
filled, a keys file outside git, and the next two commands to run. Everything is stdlib only
and works on Windows and Linux.

    python tools/setup.py
    python tools/setup.py --non-interactive --name mysite --lang en --yes
    python tools/setup.py --preset solo-claude --unattended    # one Claude subscription, overnight

What it does:
1. asks the project name, owner language (ru/en) and timezone (default from the OS);
2. detects the agent CLIs on PATH and prints found/missing with one install hint each.
   ``orca`` and ``claude`` are required: when one is missing the wizard still writes the config but
   exits 1 and does NOT print launch instructions (``--allow-missing`` for a deliberate dry run).
   «On PATH» means installed, not logged in or entitled to a model; ``--probe`` runs each found
   CLI's ``--version`` (free) so you know it at least starts;
3. ensures the keys file exists (``[paths] keys_file`` in ``producer.toml``, default
   ``~/.config/producer/keys.env``; chmod 600; commented variable NAMES only);
4. picks a PRESET from ``tools/presets.toml`` (``--list-presets``) and writes the Producer routes and
   ``[[roster]]`` (the single-command fallback ``[guardian] producer_command`` is kept in step):
   * ``solo-claude`` — the first-class path: ONE Claude subscription. Producer = Claude Sonnet
     (medium); Workers = one Sonnet + one Haiku; no other route. All three tabs share one 5-hour
     window. This is the default when ``claude`` is the only agent CLI found;
   * ``claude-max-100`` / ``claude-plus-go`` / ``budget-free`` … — the other presets of presets.toml
     (cost and what each needs are shown; ``auto`` picks the fullest one this machine can run);
   * ``full`` — Producer = Sonnet plus a worker seat for every extra CLI found (OpenCode, Codex…);
   ``--unattended`` adds Claude's permission-bypass flag to every Claude command (overnight runs
   need it; it is never added by default). Re-running keeps your commands and timers: they are
   rewritten only with ``--preset`` or ``--reset-guardian``;
5. writes ``[review] reviewers`` from the CLIs found and installs the commit-msg hook (knowledge gate,
   which is off until product globs are set, plus the agent-authorship check);
6. in English mode renames/rewrites the untouched Russian seed pages (brief, digest, …) to English;
7. ``git init`` if needed; if ``origin`` points at the template repository, renames it to
   ``template`` (push disabled) and prints how to create the user's own repo;
8. prints the next two commands (``guardian.py start`` then ``guardian.py autonomy on``).

``producer.toml`` is only ever changed by :func:`write_config`: it replaces ``[project]`` and
``[setup]`` and — only when the preset is (re)applied — ``[guardian]`` and every ``[[roster]]``
block; every other section (``[paths]``, ``[orca]``, ``[knowledge]`` when the gate stays off, and
any section another seat appended) stays untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

PROJECT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT / "tools" / "templates"

# ---------------------------------------------------------------- constants
PERMISSION_FLAG = "--dangerously-skip-permissions"
PRODUCER_COMMAND = "claude --model claude-sonnet-5-5 --effort medium"
SONNET_COMMAND = "claude --model claude-sonnet-5-5 --effort medium"
HAIKU_COMMAND = "claude --model claude-haiku-4-5-20251001"
BOOTSTRAP_PROMPT = (
    "You are the Producer. Read work/agents/orca/START_PROMPT.md in full and resume. "
    "Autonomy is ON."
)
GUARDIAN_DEFAULTS = {
    "interval_seconds": 60,
    "idle_nudge_minutes": 10,
    "worker_idle_minutes": 15,
    "seat_max_hours": 3,
    "producer_title": "Producer",
}
PRESETS_FILE = PROJECT / "tools" / "presets.toml"
BUILTIN_PRESETS = ("solo-claude", "full")  # always available, with or without presets.toml

# claude, codex, opencode, agy, cmd, orca, git, gh  (name is what PATH is probed for)
CLI_HINTS: tuple[tuple[str, str], ...] = (
    ("claude", "Claude Code — the Producer (required); npm i -g @anthropic-ai/claude-code"),
    ("codex", "Codex CLI — extra workers; npm i -g @openai/codex"),
    ("opencode", "OpenCode — free/Go workers; npm i -g opencode-ai"),
    ("agy", "AGY/Gemini — fast lookups; install from the AGY provider"),
    ("cmd", "Command Code — extra worker budget; install from its provider (Windows cmd.exe is NOT it)"),
    ("orca", "Orca IDE — hosts the Producer/Worker tabs (required); install the Orca desktop app"),
    ("git", "Git — version control; https://git-scm.com/downloads"),
    ("gh", "GitHub CLI — optional PRs; https://cli.github.com"),
)
REQUIRED_CLIS = ("orca", "claude")
WORKER_CLIS = ("codex", "opencode", "agy", "cmd")  # an agent CLI beyond Claude makes the setup «full»

KEY_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "COMMAND_CODE_API_KEY",
    "COMMANDCODE_API_KEY",
)

GITIGNORE_ENTRIES = (
    ".runtime/",
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    "*.env",
    "keys.env",
    "*.tmp",
    "*.lock",
)

DEFAULT_HEADER = (
    "# producer.toml — settings for the generic Producer toolchain.\n"
    "#\n"
    "# This file is the ONE place the tools read project identity from. Every path a tool\n"
    "# needs is derived here and resolved by tools/paths.py; no other file hard-codes one.\n"
)

ANSI_WARN = "\a" if os.name == "nt" else ""  # a Windows bell makes a warning audible

# Owner pages that setup localises: (Russian path, English path). Both are the same file for the
# pages that do not change name. A page is replaced only while it is byte-for-byte the shipped
# Russian seed (SEED_SHA256 below; a test fails when a seed is edited without updating its hash).
SEED_PAGES: tuple[tuple[str, str], ...] = (
    ("work/БРИФ.md", "work/BRIEF.md"),
    ("work/ROADMAP.md", "work/ROADMAP.md"),
    ("work/ЧЕКЛИСТ.md", "work/CHECKLIST.md"),
    ("work/ДАЙДЖЕСТ.md", "work/DIGEST.md"),
    ("work/БЕЗ_ОТВЕТА.md", "work/UNANSWERED.md"),
    ("work/systems/PAGE_TEMPLATE.md", "work/systems/PAGE_TEMPLATE.md"),
    ("work/agents/registers/BLITZ_READY_TEMPLATE.md", "work/agents/registers/BLITZ_READY_TEMPLATE.md"),
)
SEED_SHA256: dict[str, str] = {
    "work/БРИФ.md":
        "6424100ba76b168ebca402c0d979945c16655eaa2b1d266c8a67f5930c3a9ae7",
    "work/ROADMAP.md":
        "bfc54c5cab36ad2636f6029f0bebb99c220d29fe84708d556a3e08138afe936a",
    "work/ЧЕКЛИСТ.md":
        "ae786ffea9febecb5f7bc75ad7483065004530e193294d162e12870fb653bbd5",
    "work/ДАЙДЖЕСТ.md":
        "1fea1414fd63a0d398221e3b635fd10fae5137bf9fe029c389d9c4441cbb9701",
    "work/БЕЗ_ОТВЕТА.md":
        "746574d0add478c8dad48605e444482d1b747d83317a22a6aa11d783721a0715",
    "work/systems/PAGE_TEMPLATE.md":
        "0e6b4320ac51126a1c50c696a46265bdbc565ee90b94c02c49f082b154138f3a",
    "work/agents/registers/BLITZ_READY_TEMPLATE.md":
        "17d241a0472f70d3d761b61cfea0d020431fa0813ca134121cb27670164d7717",
}


def seed_digest(text: str) -> str:
    """Hash of a page with line endings and trailing blank space normalised away."""
    normalised = text.replace("\r\n", "\n").rstrip() + "\n"
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- model
@dataclass
class Seat:
    name: str
    title_prefix: str
    count: int = 1
    command: str = ""


@dataclass
class Plan:
    name: str
    lang: str
    timezone: str
    producer_command: str = PRODUCER_COMMAND
    roster: list[Seat] = field(default_factory=list)
    product_globs: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()
    preset: str = "full"
    unattended: bool = False
    apply_guardian: bool = True  # False = leave the existing [guardian], routes and roster exactly as they are
    routes: list[dict] = field(default_factory=list)  # Producer routes the Guardian walks, best first


# ---------------------------------------------------------------- detection
def _is_windows_shell(path: str | None) -> bool:
    """On Windows `cmd` resolves to the system shell (cmd.exe), which is NOT the Command Code CLI."""
    return bool(path) and path.replace("/", "\\").lower().endswith("\\system32\\cmd.exe")


def detect_clis(which: Callable[[str], str | None] = shutil.which) -> dict[str, str | None]:
    """Map each known CLI to the path PATH resolves it to, or None (the Windows shell is not `cmd`)."""
    found = {name: which(name) for name, _hint in CLI_HINTS}
    if _is_windows_shell(found.get("cmd")):
        found["cmd"] = None
    return found


def render_cli_table(found: dict[str, str | None]) -> list[str]:
    """One line per CLI: [x]/[ ] and path or install hint; a header marks missing required ones."""
    lines: list[str] = []
    for name, hint in CLI_HINTS:
        path = found.get(name)
        required = name in REQUIRED_CLIS
        mark = "x" if path else " "
        label = name + (" (required)" if required else "")
        lines.append(f"  [{mark}] {label:<20} {path or 'MISSING — ' + hint}")
    return lines


def missing_required(found: dict[str, str | None]) -> list[str]:
    return [name for name in REQUIRED_CLIS if not found.get(name)]


def probe_clis(found: dict[str, str | None], runner=subprocess.run) -> dict[str, str]:
    """`<cli> --version` for every found CLI: proves it starts. Free, no model request.

    Says nothing about login or model access; that needs a real request (README, «connect
    accounts») which can cost quota, so it is never run implicitly.
    """
    result: dict[str, str] = {}
    for name, path in found.items():
        if not path or name in ("cmd",):  # `cmd` may be Windows' own shell: never run it blindly
            continue
        try:
            done = runner([path, "--version"], capture_output=True, text=True, timeout=20,
                          encoding="utf-8", errors="replace")
            first = ((done.stdout or done.stderr or "").strip().splitlines() or [""])[0][:80]
            result[name] = f"starts (rc {done.returncode}) {first}".strip()
        except (OSError, subprocess.SubprocessError) as exc:
            result[name] = f"does NOT start: {exc}"
    return result


# ---------------------------------------------------------------- presets and roster
def with_permissions(command: str, unattended: bool) -> str:
    """Add Claude's permission-bypass flag to a `claude ...` command, only when asked for."""
    if unattended and command.split()[:1] == ["claude"] and PERMISSION_FLAG not in command:
        return f"{command} {PERMISSION_FLAG}"
    return command


def load_presets(path: Path | None = None) -> list[dict]:
    """The presets of `tools/presets.toml`: name, label, who, cost, notes, routes, roster (file order).

    A missing or unreadable file gives [] - setup then falls back to its two built-in presets.
    """
    try:
        import tomllib
        data = tomllib.loads(Path(path or PRESETS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return []
    presets: list[dict] = []
    for row in data.get("preset", []) if isinstance(data, dict) else []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        presets.append({
            "name": str(row["name"]), "label": str(row.get("label") or row["name"]),
            "who": str(row.get("who") or ""), "cost": str(row.get("cost") or ""),
            "notes": str(row.get("notes") or ""),
            "routes": [r for r in row.get("producer_routes", []) if isinstance(r, dict)],
            "roster": [r for r in row.get("roster", []) if isinstance(r, dict)],
        })
    return presets


def preset_clis(preset: dict) -> set[str]:
    """The agent CLIs a preset needs: every route's `agent` and the CLI each roster command starts."""
    known = {name for name, _hint in CLI_HINTS}
    needed = {str(r["agent"]) for r in preset.get("routes", []) if r.get("agent")}
    for seat in preset.get("roster", []):
        words = str(seat.get("command") or "").split()
        needed.update(w for w in words[:1] if w in known)
    return needed & known


def preset_missing(preset: dict, found: dict[str, str | None]) -> list[str]:
    return sorted(cli for cli in preset_clis(preset) if not found.get(cli))


def choose_preset(requested: str, found: dict[str, str | None], presets: list[dict] | None = None) -> str:
    """The preset to apply. A named one is taken as given; `auto` means:

    only `claude` on PATH (no Codex/OpenCode/AGY/Command Code) -> solo-claude; otherwise the preset
    of presets.toml that this machine can run completely and that uses the most different CLIs, else
    `full` (a seat per CLI found).
    """
    presets = presets or []
    names = {p["name"] for p in presets} | set(BUILTIN_PRESETS)
    if requested != "auto":
        if requested not in names:
            raise ValueError(f"unknown preset {requested!r}; available: {', '.join(sorted(names))}")
        return requested
    if not any(found.get(name) for name in WORKER_CLIS):
        return "solo-claude"
    runnable = [p for p in presets if not preset_missing(p, found) and p["name"] != "solo-claude"]
    if runnable:
        return max(runnable, key=lambda p: len(preset_clis(p)))["name"]
    return "full"


def apply_unattended(command: str, unattended: bool) -> str:
    """Permission-bypass flags are the owner's explicit choice: stripped unless `unattended`, and
    only a Claude command gets the Claude flag added."""
    tokens = [t for t in command.split() if t != PERMISSION_FLAG]
    out = " ".join(tokens)
    return with_permissions(out, unattended)


def preset_seats(preset: dict, unattended: bool) -> list[Seat]:
    return [Seat(str(r.get("name") or "seat"), str(r.get("title_prefix") or r.get("name") or "Seat"),
                 int(r.get("count") or 1), apply_unattended(str(r.get("command") or ""), unattended))
            for r in preset.get("roster", []) if r.get("command")]


def preset_routes(preset: dict, unattended: bool) -> list[dict]:
    """The preset's Producer routes, verbatim except `args_unattended` (kept only when unattended)."""
    routes = []
    for row in preset.get("routes", []):
        route = {k: v for k, v in row.items() if k != "args_unattended"}
        if unattended and row.get("args_unattended"):
            route["args_unattended"] = row["args_unattended"]
        routes.append(route)
    return routes


def render_preset_table(presets: list[dict], found: dict[str, str | None]) -> list[str]:
    lines = []
    for p in presets:
        missing = preset_missing(p, found)
        mark = "ready" if not missing else "needs " + ", ".join(missing)
        lines.append(f"  {p['name']:<16} {p['label']} · {p['cost']} [{mark}]")
        if p["who"]:
            lines.append(f"  {'':<16} {p['who']}")
    return lines


def review_families(found: dict[str, str | None], preset: str = "") -> tuple[str, ...]:
    """Commit-review families this machine can actually run, other vendors first.

    The tenth-commit review needs a family that did not write the batch; listing only what is
    installed means a stranger is never routed to an account they do not have.
    """
    families: list[str] = []
    if preset == "solo-claude":
        # One subscription: Opus and Sonnet review each other (different models, separate families);
        # when a batch was written by both, commit_review falls back to a fresh same-family review.
        return ("claude-opus", "claude-sonnet")
    if found.get("codex"):
        families += ["codex-luna", "codex-sol"]
    if found.get("agy"):
        families.append("gemini")
    if found.get("opencode"):
        families.append("deepseek")
    if found.get("claude"):
        families += ["claude-opus", "claude-sonnet"]
    return tuple(families)


def solo_claude_roster(unattended: bool = False) -> list[Seat]:
    """One subscription: a Sonnet Worker and a Haiku Worker, both Claude, nothing else."""
    return [
        Seat("sonnet", "Sonnet", 1, with_permissions(SONNET_COMMAND, unattended)),
        Seat("haiku", "Haiku", 1, with_permissions(HAIKU_COMMAND, unattended)),
    ]


def default_roster(
    found: dict[str, str | None], environ: dict[str, str] | None = None, unattended: bool = False
) -> list[Seat]:
    """The «full» roster from what is installed: claude -> Sonnet; opencode -> Go/free; codex -> Luna."""
    env = os.environ if environ is None else environ
    roster: list[Seat] = []
    if found.get("claude"):
        roster.append(Seat("sonnet", "Sonnet", 1, with_permissions(SONNET_COMMAND, unattended)))
    if found.get("opencode"):
        if env.get("OPENCODE_API_KEY"):
            roster.append(Seat("deepseek", "DS", 2,
                               "opencode --model opencode-go/deepseek-v4.1-flash"))
        else:
            roster.append(Seat("space-bunny", "Bunny", 1,
                               "opencode --model opencode/space-bunny-free"))
    if found.get("codex"):
        roster.append(Seat("luna", "Luna", 1,
                           "codex -m gpt-6-luna -c model_reasoning_effort=max"))
    return roster


# ---------------------------------------------------------------- keys file
def keys_path_from_config(root: Path) -> Path:
    """`[paths] keys_file` of this project's producer.toml (expanded), else the default location."""
    default = os.path.expanduser("~/.config/producer/keys.env")
    config = Path(root) / "producer.toml"
    try:
        import tomllib
        data = tomllib.loads(config.read_text(encoding="utf-8", errors="replace"))
        raw = str((data.get("paths") or {}).get("keys_file") or "")
    except (OSError, ValueError, ImportError):
        raw = ""
    return Path(os.path.expanduser(raw or default))


def ensure_keys_file(path: Path) -> bool:
    """Create the keys file with commented NAMES only if missing; return whether it was created."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Producer model keys — fill in the values, never commit this file.",
        f"# Sourced by the worker launch commands:  . {path}",
        "",
    ]
    lines += [f"# {name}=" for name in KEY_NAMES]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: chmod is best-effort; the file is outside git anyway
    return True


# ---------------------------------------------------------------- .gitignore
def ensure_gitignore(root: Path) -> bool:
    """Append any missing standard entries; return whether the file changed."""
    path = Path(root) / ".gitignore"
    existing = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    present = {line.strip() for line in existing}
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return False
    if existing and existing[-1].strip():
        existing.append("")
    existing.append("# Producer toolchain runtime and keys")
    existing.extend(missing)
    path.write_text("\n".join(existing).rstrip("\n") + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------- producer.toml writer
HEADER_RE = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-]+)\s*\]\]?")
ARRAY_SECTIONS = {"roster"}


def _header_key(line: str) -> str | None:
    match = HEADER_RE.match(line)
    return match.group(1) if match else None


def _blocks(text: str) -> list[dict]:
    """Split into blocks, each starting at a table header (key=None for the preamble)."""
    result: list[dict] = []
    current: dict = {"key": None, "lines": []}
    for raw in text.splitlines():
        key = _header_key(raw)
        if key is not None:
            result.append(current)
            current = {"key": key, "lines": [raw]}
        else:
            current["lines"].append(raw)
    result.append(current)
    return result


def update_toml(text: str, generated: dict[str, list[str]], arrays: set[str]) -> str:
    """Replace managed sections in ``text`` with ``generated``, keeping every other line.

    A key in ``generated`` replaces the FIRST block with that header; later duplicate single
    sections are dropped; every block of an array section (e.g. ``[[roster]]``) is replaced by
    one generated sequence at the first occurrence. Generated sections absent from the file are
    appended at the end. Unmanaged sections (including an array section that is not in
    ``generated``) and the preamble survive verbatim.
    """
    out: list[str] = []
    emitted: set[str] = set()
    for block in _blocks(text):
        key = block["key"]
        if key is None:
            out.extend(block["lines"])
            continue
        if key in generated:
            if key not in emitted:
                out.extend(generated[key])
                emitted.add(key)
            continue  # a later duplicate / further array element of a managed section is dropped
        out.extend(block["lines"])
    for key, lines in generated.items():
        if key not in emitted:
            out.append("")
            out.extend(lines)
    return "\n".join(out).rstrip("\n") + "\n"


def _toml_quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render_project(plan: Plan) -> list[str]:
    return [
        "[project]",
        f"name = {_toml_quote(plan.name)}",
        f"timezone = {_toml_quote(plan.timezone)}",
        f"owner_language = {_toml_quote(plan.lang)}",
        "",
    ]


def render_setup(plan: Plan) -> list[str]:
    return [
        "[setup]",
        "# Written by tools/setup.py. Re-running keeps the [guardian] section and the roster seats as they are; they are",
        "# rewritten only with --preset or --reset-guardian.",
        f"preset = {_toml_quote(plan.preset)}",
        f"unattended = {'true' if plan.unattended else 'false'}",
        "",
    ]


def render_guardian(plan: Plan) -> list[str]:
    return [
        "[guardian]",
        "# The small generic Guardian (tools/guardian.py). Any agent CLI works: producer_command",
        "# and each roster command are free text; the Guardian only counts and reminds.",
        f"interval_seconds = {GUARDIAN_DEFAULTS['interval_seconds']}",
        f"idle_nudge_minutes = {GUARDIAN_DEFAULTS['idle_nudge_minutes']}        # Producer screen unchanged this long while autonomy ON -> nudge",
        f"worker_idle_minutes = {GUARDIAN_DEFAULTS['worker_idle_minutes']}       # a Worker screen unchanged this long -> tell the Producer",
        f"seat_max_hours = {GUARDIAN_DEFAULTS['seat_max_hours']}             # Producer seat age -> type ROTATE NOW",
        f"producer_title = {_toml_quote(GUARDIAN_DEFAULTS['producer_title'])}",
        f"producer_command = {_toml_quote(plan.producer_command)}",
        f"bootstrap_prompt = {_toml_quote(BOOTSTRAP_PROMPT)}",
        "",
    ]


CLAUDE_LIMITS = ["usage limit", "\\d-hour limit", "weekly limit", "rate limit", "out of credits", "\\b429\\b"]
CODEX_LIMITS = ["usage limit", "try again at", "rate limit", "out of credits", "\\b429\\b"]


def producer_routes(found: dict[str, str | None], preset: str, unattended: bool) -> list[dict]:
    """The Producer routes the Guardian walks, best first - only routes this machine can run.

    solo-claude = exactly one: Claude Sonnet (medium). full = Claude, then Codex when installed.
    The permission-bypass flag goes in `args_unattended`, which the Guardian appends only while
    autonomy is ON, and only when the user asked for it.
    """
    routes: list[dict] = []
    if found.get("claude"):
        routes.append({"name": "claude-sonnet", "title": "Producer Claude Sonnet", "agent": "claude",
                       "command": PRODUCER_COMMAND, "limit_patterns": CLAUDE_LIMITS, "probe": "claude --version",
                       "args_unattended": PERMISSION_FLAG if unattended else ""})
    if preset != "solo-claude" and found.get("codex"):
        routes.append({"name": "codex-luna", "title": "Producer Codex Luna", "agent": "codex",
                       "command": "codex -m gpt-6-luna -c model_reasoning_effort=max",
                       "limit_patterns": CODEX_LIMITS, "probe": "codex --version", "args_unattended": ""})
    return routes


ROUTE_KEY_ORDER = ("name", "title", "agent", "keys", "shell_windows", "command", "args_unattended",
                   "limit_patterns", "probe", "probe_timeout", "enabled")


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_quote(value)


def render_routes(routes: list[dict]) -> list[str]:
    """`[[producer_routes]]` blocks: the keys the Guardian reads, in a stable order, any extra key last."""
    lines: list[str] = []
    for route in routes:
        lines.append("[[producer_routes]]")
        keys = [k for k in ROUTE_KEY_ORDER if k in route] + [k for k in route if k not in ROUTE_KEY_ORDER]
        for key in keys:
            if route[key] in ("", None):
                continue
            lines.append(f"{key} = {_toml_value(route[key])}")
        lines.append("")
    return lines


def _set_guardian_command(text: str, command: str) -> str:
    """Change only `producer_command` inside an existing [guardian] section; touch nothing else."""
    out: list[str] = []
    in_guardian = done = False
    for line in text.split("\n"):
        key = _header_key(line)
        if key is not None:
            if in_guardian and not done:
                out.append(f"producer_command = {_toml_quote(command)}")
                done = True
            in_guardian = key == "guardian"
        elif in_guardian and re.match(r"^\s*producer_command\s*=", line) and not done:
            out.append(f"producer_command = {_toml_quote(command)}")
            done = True
            continue
        out.append(line)
    return "\n".join(out)


def render_roster(roster: list[Seat], preset: str = "full") -> list[str]:
    if not roster:
        return []
    lines = ["# The wanted worker seats; the Producer launches them, the Guardian only counts and reminds."]
    if preset == "solo-claude":
        lines.append("# solo-claude: ONE Claude subscription. The Producer and these Workers all draw from the "
                     "same 5-hour window.")
    lines.append("# Seats are installed-on-PATH only: login and model access are not verified by setup.")
    for seat in roster:
        lines += [
            "[[roster]]",
            f"name = {_toml_quote(seat.name)}",
            f"title_prefix = {_toml_quote(seat.title_prefix)}",
            f"count = {int(seat.count)}",
            f"command = {_toml_quote(seat.command)}",
        ]
    return lines


def render_review(families: tuple[str, ...]) -> list[str]:
    joined = ", ".join(_toml_quote(f) for f in families)
    return [
        "[review]",
        "# Model families that can review commits (best first). The reviewer must not be a family that",
        "# wrote the batch; with one vendor only, `commit_review.py open --accept-same-family` records a",
        "# weaker same-family review knowingly.",
        f"reviewers = [{joined}]",
        "",
    ]


def render_knowledge(globs: tuple[str, ...]) -> list[str]:
    joined = ", ".join(_toml_quote(glob) for glob in globs)
    return [
        "[knowledge]",
        "# Product globs whose change requires a knowledge write-back. Empty = the gate is off.",
        "# A glob is matched against a repo-relative POSIX path (fnmatch).",
        f"product_globs = [{joined}]",
        "",
    ]


def write_config(path: Path, plan: Plan, *, existing: str | None = None) -> str:
    """The wizard's ONLY producer.toml writer; returns the new text (and leaves writing to the caller)."""
    path = Path(path)
    if existing is None:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if not existing.strip():
        existing = DEFAULT_HEADER
    generated: dict[str, list[str]] = {
        "project": render_project(plan),
        "setup": render_setup(plan),
    }
    if plan.apply_guardian:
        if any(block["key"] == "guardian" for block in _blocks(existing)):
            # Never regenerate an existing [guardian]: it carries tuning (ignore_titles, recover
            # minutes, ...) this wizard does not know. Only the single-command fallback is set.
            existing = _set_guardian_command(existing, plan.producer_command)
        else:
            generated["guardian"] = render_guardian(plan)
        generated["roster"] = render_roster(plan.roster, plan.preset)
        if plan.routes:
            generated["producer_routes"] = render_routes(plan.routes)
        if plan.reviewers:
            generated["review"] = render_review(plan.reviewers)
    if plan.product_globs:
        generated["knowledge"] = render_knowledge(plan.product_globs)
    return update_toml(existing, generated, ARRAY_SECTIONS)


def _has_setup_section(text: str) -> bool:
    return any(block["key"] == "setup" for block in _blocks(text))


# ---------------------------------------------------------------- owner-language seed pages
def localize_owner_pages(root: Path, lang: str) -> list[str]:
    """English mode: replace the still-untouched Russian seed pages with their English versions.

    A page you have edited (it no longer matches the shipped seed's hash) is left alone, as is any
    page whose English file already exists. Returns one human line per change.
    Russian is the shipped language, so `ru` changes nothing.
    """
    root = Path(root)
    notes: list[str] = []
    if lang != "en":
        return notes
    for ru_rel, en_rel in SEED_PAGES:
        template = TEMPLATES / "en" / Path(en_rel).name
        ru_path, en_path = root / ru_rel, root / en_rel
        if not template.is_file():
            continue
        if ru_path != en_path and en_path.exists():
            notes.append(f"kept {en_rel} (already exists)")
            continue
        if ru_path.exists():
            current = ru_path.read_text(encoding="utf-8", errors="replace")
            if seed_digest(current) != SEED_SHA256.get(ru_rel):
                notes.append(f"kept {ru_rel} (you have edited it)")
                continue
            if ru_path != en_path:
                ru_path.unlink()
        elif ru_path != en_path:
            pass  # no Russian seed: simply create the English page below
        en_path.parent.mkdir(parents=True, exist_ok=True)
        en_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        notes.append(f"{ru_rel} -> {en_rel}" if ru_path != en_path else f"{en_rel} (English)")
    return notes


# ---------------------------------------------------------------- external steps
def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def ensure_git_repo(root: Path) -> bool:
    """``git init`` when the directory is not already a repository; return whether we ran it."""
    root = Path(root)
    if (root / ".git").exists():
        return False
    _run_git(root, "init", "-q")
    return True


TEMPLATE_REPO = "RealHomoBulla/producer"
_TEMPLATE_URL_RE = re.compile(r"github\.com[:/]+" + re.escape(TEMPLATE_REPO) + r"(?:\.git)?/?$", re.IGNORECASE)


def fix_template_remote(root: Path, project_name: str = "my-project") -> list[str]:
    """Keep a user's project from ever pushing to the template.

    If `origin` points at the template repository, rename it to `template`, disable pushing to it
    and return the lines that tell the user how to create their OWN repository. Any other origin
    (the user's own repo) is left alone. Returns [] when there is nothing to say.
    """
    root = Path(root)
    if not (root / ".git").exists():
        return []
    done = _run_git(root, "remote", "get-url", "origin")
    url = (done.stdout or "").strip() if done.returncode == 0 else ""
    if not url or not _TEMPLATE_URL_RE.search(url):
        return []
    if _run_git(root, "remote", "get-url", "template").returncode == 0:
        return [f"WARNING: origin still points at the template ({url}) and a 'template' remote "
                "already exists; run `git remote remove origin` yourself, then add your own origin."]
    renamed = _run_git(root, "remote", "rename", "origin", "template")
    if renamed.returncode != 0:
        return [f"WARNING: origin points at the template ({url}) but renaming it failed: "
                f"{(renamed.stderr or renamed.stdout).strip()[:200]}"]
    _run_git(root, "remote", "set-url", "--push", "template", "DISABLED-never-push-to-the-template")
    return [
        f"Remote 'origin' pointed at the template ({url}); renamed it to 'template' and disabled "
        "pushing to it (pull template updates with `git fetch template`).",
        "Create YOUR OWN repository so your work has somewhere to go:",
        f"  gh repo create {project_name} --private --source . --remote origin --push",
        "  or on github.com: New repository (empty) -> then",
        "    git remote add origin <your-repo-url> && git push -u origin main",
    ]


def set_review_baseline(root: Path, runner=subprocess.run) -> str:
    """Start the tenth-commit review AFTER the template's own history.

    A fresh clone carries the skeleton's agent-written commits; counting them would make a new
    project's first review "due" for work the user never did. Returns a one-line note ("" = nothing done).
    """
    root = Path(root)
    state = root / "work" / "agents" / "state" / "COMMIT_REVIEW_STATE.json"
    tool = root / "tools" / "commit_review.py"
    if state.exists() or not tool.is_file() or not (root / ".git").exists():
        return ""
    head = _run_git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return ""  # no commits yet: nothing to exclude
    done = runner([sys.executable, str(tool), "baseline", "--at", "HEAD", "--why",
                   "the template's own history: the review rule starts with this project's commits"],
                  cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return ("Commit review starts after the template's history." if done.returncode == 0
            else f"could not set the commit-review baseline: {(done.stderr or done.stdout).strip()[:120]}")


def install_knowledge_gate(root: Path) -> tuple[bool, str]:
    """Install the copy's own commit-msg hook (only called when product globs are set)."""
    tool = Path(root) / "tools" / "knowledge_gate.py"
    if not tool.is_file():
        return False, f"{tool} not found"
    proc = subprocess.run(
        [sys.executable, str(tool), "install"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()


# ---------------------------------------------------------------- interactive helpers
def _ask(prompt: str, default: str, input_fn: Callable[[str], str]) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input_fn(f"{prompt}{suffix}: ").strip()
    return raw or default


def default_timezone() -> str:
    """The OS timezone as an IANA name when available, else a plain UTC offset (never a locale name)."""
    try:
        info = datetime.now().astimezone().tzinfo
        key = getattr(info, "key", None)
        if key:
            return str(key)
        offset = datetime.now().astimezone().utcoffset()
        if offset is not None:
            total = int(offset.total_seconds())
            sign = "+" if total >= 0 else "-"
            hours, minutes = divmod(abs(total) // 60, 60)
            return f"UTC{sign}{hours:02d}:{minutes:02d}"
    except Exception:  # noqa: BLE001 - a bad clock must not block setup
        pass
    return "UTC"


def _parse_globs(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# ---------------------------------------------------------------- the wizard
def run_setup(
    root: Path | str | None = None,
    argv: list[str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    environ: dict[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    keys_path: Path | None = None,
    git_init: bool = True,
    gate_installer: Callable[[Path], tuple[bool, str]] | None = None,
    probe_runner=subprocess.run,
) -> int:
    root = Path(root) if root else PROJECT
    env = os.environ if environ is None else environ
    raw_out = out

    def out(text: str) -> None:  # noqa: F811 - plain ASCII punctuation: the console may not show dashes
        raw_out(text.replace("\u2014", "-").replace("\u00b7", "-"))

    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    interactive = not args.non_interactive
    gate_installer = gate_installer or install_knowledge_gate
    if keys_path is None:
        keys_path = keys_path_from_config(root)
    config_path = root / "producer.toml"
    existing_text = config_path.read_text(encoding="utf-8", errors="replace") if config_path.is_file() else ""

    # 1. identity
    if interactive:
        existing_name = _existing_project_name(root) or "producer"
        name = _ask("Project name", args.name or existing_name, input_fn)
        lang = _ask("Owner language (ru/en)", args.lang, input_fn).lower()
        timezone = _ask("Timezone", args.timezone or default_timezone(), input_fn)
    else:
        name = args.name or "producer"
        lang = args.lang
        timezone = args.timezone or default_timezone()
    lang = "en" if lang.startswith("en") else "ru"

    # 2. CLI detection
    found = detect_clis(which)
    out("Agent CLIs on PATH (installed — not necessarily logged in):")
    for line in render_cli_table(found):
        out(line)
    missing = missing_required(found)
    for item in missing:
        out(f"{ANSI_WARN}WARNING: required CLI '{item}' is MISSING — install it before starting the Guardian.")
    if args.probe:
        out("Probe (`--version`; login and model access are NOT checked):")
        for cli, verdict in probe_clis(found, probe_runner).items():
            out(f"  {cli:<10} {verdict}")

    # 3. keys file
    created = ensure_keys_file(keys_path)
    if created:
        out(f"Created {keys_path} (mode 600) with commented variable NAMES only.")
        out(f"  Edit it and fill the values you have — nothing is asked for on screen: {keys_path}")
    else:
        out(f"Keys file already exists: {keys_path}")

    # 4. preset and roster
    first_run = not _has_setup_section(existing_text)
    explicit_preset = args.preset != "auto"
    apply_guardian = first_run or explicit_preset or args.reset_guardian
    presets = load_presets()
    if args.list_presets:
        out("Presets (tools/presets.toml) — `python tools/setup.py --preset <name>`:")
        for line in render_preset_table(presets, found):
            out(line)
        out(f"  {'full':<16} one seat per agent CLI found on this machine [built in]")
        return 0
    try:
        preset = choose_preset(args.preset, found, presets)
    except ValueError as exc:
        out(f"ERROR: {exc}")
        return 2
    unattended = args.unattended
    product_globs: tuple[str, ...]
    if interactive:
        if apply_guardian:
            out("Presets:")
            for line in render_preset_table(presets, found):
                out(line)
            out(f"  {'full':<16} one seat per agent CLI found on this machine")
            answer = _ask("Preset (name)", preset, input_fn).lower()
            try:
                preset = choose_preset(answer, found, presets)
            except ValueError:
                out(f"unknown preset {answer!r}; using {preset}")
            if not unattended:
                answer = _ask("Unattended overnight run — skip Claude's permission prompts? (y/n)", "n", input_fn)
                unattended = answer.lower().startswith("y")
        globs_raw = _ask("Knowledge-gate product globs (comma separated, empty = gate off)", "", input_fn)
        product_globs = _parse_globs(globs_raw)
    else:
        product_globs = _parse_globs(args.product_globs)
    roster: list[Seat] = []
    if apply_guardian:
        chosen = next((p for p in presets if p["name"] == preset), None)
        if chosen is not None:
            roster = preset_seats(chosen, unattended)
        elif preset == "solo-claude":
            roster = solo_claude_roster(unattended)
        else:
            roster = default_roster(found, env, unattended)
        if interactive:
            out(f"Preset {preset}. Proposed roster:")
            for seat in roster:
                out(f"  {seat.name:<12} x{seat.count}  {seat.command}")
            if preset == "solo-claude":
                out("  (the Producer and these Workers share ONE 5-hour Claude window)")
            if roster and _ask("Accept this roster? (y/n)", "y", input_fn).lower().startswith("n"):
                roster = _edit_counts(roster, input_fn)
        else:
            out(f"Preset {preset}: {len(roster)} roster seat(s).")
    else:
        out("Kept the existing [guardian] and [[roster]] (re-run with --preset or --reset-guardian to rewrite).")

    plan = Plan(
        name=name, lang=lang, timezone=timezone,
        producer_command=with_permissions(PRODUCER_COMMAND, unattended),
        roster=roster, product_globs=product_globs, reviewers=review_families(found, preset),
        routes=(preset_routes(chosen, unattended) if apply_guardian and chosen is not None
                else producer_routes(found, preset, unattended) if apply_guardian else []),
        preset=preset if apply_guardian else
        _existing_preset(existing_text) or preset, unattended=unattended,
        apply_guardian=apply_guardian,
    )

    # 5. write, ignore, localise, git, gate
    config_path.write_text(write_config(config_path, plan, existing=existing_text or None),
                           encoding="utf-8")
    out(f"Wrote {config_path} ({len(roster)} roster seat(s)).")
    if ensure_gitignore(root):
        out("Updated .gitignore with the standard runtime/key entries.")
    for note in localize_owner_pages(root, lang):
        out(f"Owner page: {note}")
    if git_init and ensure_git_repo(root):
        out("Initialized a git repository (git init).")
    for line in fix_template_remote(root, re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "my-project"):
        out(line)
    if (root / ".git").exists():
        note = set_review_baseline(root)
        if note:
            out(note)
        ok, detail = gate_installer(root)
        out(f"Commit hooks: {'installed' if ok else 'NOT installed'} — {detail}")
    if product_globs:
        out("Knowledge gate is ON for the product globs you gave.")
    else:
        out("Knowledge gate stays off (no product globs set); the commit hook still checks agent "
            "authorship trailers.")

    # 6. next commands — only when the machine can actually launch
    out("")
    if missing and not args.allow_missing:
        out(f"NOT READY: install {', '.join(missing)} and re-run `python tools/setup.py` "
            "(config was written; nothing is lost). Use --allow-missing only for a dry run.")
        return 1
    if unattended:
        out("Unattended mode is ON: Claude tabs run without permission prompts. Only use it on a "
            "project folder you are happy for the agents to edit freely.")
    out("Next:")
    out("  python tools/guardian.py start")
    out('  python tools/guardian.py autonomy on --objective "..."')
    return 0


def _existing_preset(text: str) -> str:
    try:
        import tomllib
        value = (tomllib.loads(text).get("setup") or {}).get("preset")
    except (ValueError, ImportError):
        return ""
    return value if value in ("solo-claude", "full") else ""


def _existing_project_name(root: Path) -> str | None:
    path = Path(root) / "producer.toml"
    if not path.is_file():
        return None
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, ImportError):
        return None
    project = data.get("project") if isinstance(data, dict) else None
    if isinstance(project, dict) and str(project.get("name") or "").strip():
        return str(project["name"]).strip()
    return None


def _edit_counts(roster: list[Seat], input_fn: Callable[[str], str]) -> list[Seat]:
    edited: list[Seat] = []
    for seat in roster:
        raw = _ask(f"Count for {seat.name}", str(seat.count), input_fn)
        try:
            count = max(0, int(raw))
        except ValueError:
            count = seat.count
        if count:
            edited.append(Seat(seat.name, seat.title_prefix, count, seat.command))
    return edited


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="First-run setup for the producer skeleton.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="no prompts; use --name/--lang and the chosen preset")
    parser.add_argument("--name", default="", help="project name")
    parser.add_argument("--lang", default="ru", choices=["ru", "en"], help="owner language")
    parser.add_argument("--timezone", default="", help="timezone (default: the OS)")
    parser.add_argument("--yes", action="store_true", help="accept the proposed roster")
    parser.add_argument("--preset", default="auto",
                        help="a name from tools/presets.toml (see --list-presets), `full` = a seat per CLI "
                             "found, or `auto` (default): solo-claude when claude is the only agent CLI found")
    parser.add_argument("--list-presets", action="store_true",
                        help="print the presets with cost and what each needs, then exit")
    parser.add_argument("--unattended", action="store_true",
                        help="add Claude's permission-bypass flag to every Claude command (overnight "
                             "runs); never on by default")
    parser.add_argument("--reset-guardian", action="store_true",
                        help="rewrite [guardian] and [[roster]] even though setup already ran")
    parser.add_argument("--probe", action="store_true",
                        help="run `<cli> --version` for every found CLI (free; no model request)")
    parser.add_argument("--allow-missing", action="store_true",
                        help="exit 0 even when a required CLI (orca, claude) is missing")
    parser.add_argument("--product-globs", default="",
                        help="comma-separated knowledge-gate globs (empty = gate off)")
    return parser


def main(argv: list[str] | None = None) -> int:
    import paths
    paths.console_safe()
    return run_setup(PROJECT, argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""guardian.py — a small, generic watchdog for the «producer» skeleton.

One long-lived process that keeps an autonomous Producer awake and honest, for ANY project
and ANY agent CLI: it is configured from ``producer.toml`` (``[guardian]``, ``[[roster]]``,
``[[producer_routes]]``), it speaks only to Orca through :func:`_run_orca`, and it is
stdlib-only (Windows + Linux).

    python tools/guardian.py autonomy on  --objective "build the site, work all night"
    python tools/guardian.py start          # detached supervise loop, single instance
    python tools/guardian.py status         # alive? pid, last tick, route, sleeping seats, incidents
    python tools/guardian.py routes         # Producer routes + probe status
    python tools/guardian.py route --primary codex-luna
    python tools/guardian.py stop

Safety model (why it can run a night unattended):

* **Ownership.** It only ever acts on tabs of THIS project's worktree (``worktreePath``), and
  on the one Producer pane it bound (handle + incarnation, persisted). A foreign Producer,
  the owner's Blitz tab or another project's tab is never adopted, typed into or closed.
* **Ambiguity is not death.** A failed or malformed terminal list is «unverifiable»: nothing is
  launched, typed or closed. A bound Producer must be absent in ``absent_confirm_ticks``
  consecutive good reads before it counts as dead.
* **Durable succession.** A Producer is never closed before HANDOVER is committed and its
  successor is up and has received the bootstrap brief (receipt checked on a fresh screen).
* **Routes.** ``[[producer_routes]]`` is an ordered list of Producer launch lines. A limit
  message on the screen, or a dead Producer whose probe fails, hands the seat to the next live
  route; a recovered higher route is returned to at the next rotation.
* **Alarm.** A limit message on the Producer or any Worker tab is parsed for its reset time;
  the seat is marked *sleeping* and woken at reset + 2 min (relaunched if the tab died). With
  one subscription and no other route, this alarm IS the fallback.
* **One instance.** An OS lock held for the process lifetime; a token handshake on start;
  a bounded log; state persisted in ``.runtime/guardian-state.json`` so a restart resumes.

The tool is deliberately project-agnostic — the source project's 24k-line Guardian was NOT
ported; this is a new, small tool with the same job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

try:  # Python 3.11+ ships tomllib; the skeleton targets 3.12.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on an older interpreter
    tomllib = None  # type: ignore[assignment]

try:  # tzdata may be missing on Windows; the parser then falls back to local time.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

import orca_cli
import paths

# ---------------------------------------------------------------- markers and constants
BUSY_RE = re.compile(r"esc(?: to)? interrupt|\bworking\b\s*[(\u2026.]", re.I)
PASTED_MARKERS = ("[pasted content", "[pasted text", "[pasted")
CONNECTION_LOST_RE = re.compile(r"connection lost|connection error|reconnecting", re.I)
MODEL_SWITCH_RE = re.compile(r"select a model|switch model|model selector|choose a model", re.I)
MODEL_KEEP_RE = re.compile(r"keep|existing|current|stay|don.t|do not|not now|skip|\bno\b", re.I)
OPTION_RE = re.compile(r"^\W*(\d)[.)]\s+(.+)$", re.M)
FINISHED_RE = re.compile(r"delivery-fallback:|session ended|task complete", re.I)
# Spinner glyphs and digits are stripped before hashing a screen, so a ticking timer or a
# repainted spinner does not read as «the screen changed».
HASH_STRIP_RE = re.compile(r"[0-9\u2800-\u28ff\u25d0-\u25d3\u2722-\u2733\u00b7*|/\\-]+")
ROTATE_TEXT = "ROTATE NOW: write HANDOVER, checkpoint, end your turn."
WAKE_TEXT = "continue — the limit reset; re-read HANDOVER and resume"
WORKER_WAKE_TEXT = "continue — the limit reset; re-read your brief and resume"
KICKOFF_NOTE = (
    "work/БРИФ.md is empty — this is a first run: do the kickoff in START_PROMPT first "
    "(§0a connect the owner's accounts, then §0 the brief interview) before any other work."
)
ACTION_KEEP = 50
INCIDENT_KEEP = 10
RETIRED_KEEP = 20
BOOTSTRAP_WAIT_MS = 60_000
ORCA_TIMEOUT = 45
MIN_INTERVAL = 5.0
MAX_RESET_AHEAD = 8 * 86400.0  # a reset further away than this is not a reset time
STATE_FILE = "guardian-state.json"
RC_COMMAND = "/remote-control"
RC_STATUS_MARK = "/rc"             # Claude Code's status bar ends with this while Remote Control is on
RC_SETTLE_SECONDS = 10.0           # launch path: wait this long for the status bar to show it before the brief is typed
RC_VERIFY_SECONDS = 120.0          # typed but never confirmed this long -> tell the owner, do not type again
BUSY_NUDGE = "Machine busy ({reasons}) — close finished tabs, do not open new."

DEFAULTS = {
    "interval_seconds": 60.0,
    "idle_nudge_minutes": 10.0,
    "worker_idle_minutes": 15.0,
    "seat_max_hours": 3.0,
    "producer_title": "Producer",
    "producer_command": "",
    "bootstrap_prompt": "",
    "recover_minutes": 30.0,
    "probe_ttl_seconds": 300.0,
    "probe_timeout_seconds": 30.0,
    "limit_confirm_ticks": 2,
    "absent_confirm_ticks": 2,
    "rotate_ask_minutes": 15.0,
    "rotate_max_asks": 3,
    "busy_stuck_minutes": 30.0,
    "startup_grace_seconds": 120.0,
    "bootstrap_receipt_seconds": 45.0,
    "bootstrap_max_attempts": 3,
    "log_max_bytes": 1_000_000,
    "wake_delay_seconds": 120.0,
    "sleep_retry_minutes": 30.0,
    "wake_grace_seconds": 120.0,
}

# A small default so a Worker tab's limit message is recognised even when no route names it.
GENERIC_LIMIT_PATTERNS = (
    r"usage limit", r"\d-hour limit", r"weekly limit", r"rate limit", r"out of credits",
    r"try again (at|in)", r"limit (reached|hit)",
)


# ---------------------------------------------------------------- tiny helpers
def runtime_dir() -> Path:
    """The runtime directory; ``GUARDIAN_RUNTIME`` overrides it (used by tests and the smoke)."""
    override = os.environ.get("GUARDIAN_RUNTIME")
    return Path(override) if override else paths.RUNTIME


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="seconds")


def _whoami() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "owner"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _norm_path(value: object) -> str:
    text = str(value or "").replace("\\", "/").rstrip("/")
    return text.casefold() if os.name == "nt" else text


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a liveness probe on Windows; OpenProcess is.
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_orca(*args: str, timeout: int = ORCA_TIMEOUT, allow_error: bool = False) -> dict:
    """The ONE Orca seam: list args, no shell, timeout, parsed JSON.

    Appends ``--json`` (the CLI's machine mode). A failure is raised unless ``allow_error``,
    in which case an empty dict is returned so a flaky read never kills the tick.
    """
    argv = list(args)
    if "--json" not in argv:
        argv.append("--json")
    try:
        return orca_cli.run_json(*argv, timeout=timeout)
    except orca_cli.OrcaCliError:
        if allow_error:
            return {}
        raise


def _unwrap(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _terminals(payload: object) -> list[dict] | None:
    """The terminal rows, or ``None`` when the payload is not a readable inventory."""
    unwrapped = _unwrap(payload)
    rows = unwrapped.get("terminals")
    if not isinstance(rows, list) and isinstance(payload, dict):
        rows = payload.get("terminals")
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def _screen_parts(payload: object) -> tuple[str, str]:
    """(rendered tail text, composer draft) from a ``terminal read`` payload."""
    unwrapped = _unwrap(payload)
    terminal = unwrapped.get("terminal")
    if not isinstance(terminal, dict) and isinstance(payload, dict):
        terminal = payload.get("terminal")
    if not isinstance(terminal, dict):
        return "", ""
    tail = terminal.get("tail")
    lines = [str(line) for line in tail] if isinstance(tail, list) else []
    draft = terminal.get("draft")
    return "\n".join(lines), draft if isinstance(draft, str) else ""


def _find_key(value: object, key: str) -> str | None:
    if isinstance(value, dict):
        direct = value.get(key)
        if isinstance(direct, str) and direct:
            return direct
        for child in value.values():
            found = _find_key(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found:
                return found
    return None


def _first_handle(value: object) -> str | None:
    if isinstance(value, dict):
        direct = value.get("handle")
        if isinstance(direct, str) and direct.startswith("term_"):
            return direct
        for child in value.values():
            found = _first_handle(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_handle(child)
            if found:
                return found
    return None


def _tail(screen: str, lines: int) -> str:
    rows = [row for row in (screen or "").splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def _is_busy(screen: str) -> bool:
    return bool(BUSY_RE.search(_tail(screen, 8)))


def _is_finished(screen: str) -> bool:
    return bool(FINISHED_RE.search(_tail(screen, 6)))


def _has_pasted(screen: str) -> bool:
    low = (screen or "").casefold()
    return any(marker in low for marker in PASTED_MARKERS)


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", (text or "").casefold())


def _screen_digest(screen: str) -> str:
    stable = re.sub(r"\s+", " ", HASH_STRIP_RE.sub("", screen or "")).strip()
    return hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------- reset-time parsing
_ANCHOR = r"(?:will\s+)?(?:reset|try\s+again|retry|available\s+again)\w*\s*(?:at|on|in|after|:)?\s*"
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"
_CLOCK24 = r"(\d{1,2}):(\d{2})"
_MONTHS = {
    name: index + 1
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
_DURATION_PART = re.compile(r"(\d+)\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)\b", re.I)
_REL_RE = re.compile(_ANCHOR + r"((?:\d+\s*[a-z]+[\s,]*(?:and\s+)?)+)", re.I)
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*(Z|[+-]\d{2}:?\d{2})?")
_DATE_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?,?\s*(?:at\s+)?"
    r"(?:" + _CLOCK + r"|" + _CLOCK24 + r")",
    re.I,
)
_TIME_ONLY_RE = re.compile(_ANCHOR + r"(?:" + _CLOCK + r"|" + _CLOCK24 + r")", re.I)
_ZONE_RE = re.compile(r"\(\s*([A-Za-z]+(?:/[A-Za-z_+\-0-9]+)*|UTC|GMT)\s*\)")


def _tzinfo(name: str, now: float):
    """A tzinfo for an IANA name; the machine's local zone when the name is unknown or empty."""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - missing tzdata / bad name -> local time
            pass
    if name.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    try:
        return datetime.fromtimestamp(now).astimezone().tzinfo
    except (OSError, OverflowError, ValueError):
        return timezone.utc


def _hm(match: re.Match, offset: int) -> tuple[int, int] | None:
    """Hour/minute from a match whose groups start at ``offset``: [h, m, a/p] or [h, m] (24 h)."""
    groups = match.groups()
    hour, minute, meridiem = groups[offset], groups[offset + 1], groups[offset + 2]
    if hour is not None:
        h, m = int(hour), int(minute or 0)
        if not 1 <= h <= 12 or m > 59:
            return None
        if meridiem.lower() == "p" and h != 12:
            h += 12
        if meridiem.lower() == "a" and h == 12:
            h = 0
        return h, m
    hour, minute = groups[offset + 3], groups[offset + 4]
    if hour is None:
        return None
    h, m = int(hour), int(minute)
    return (h, m) if h < 24 and m < 60 else None


def parse_reset_time(text: str, now: float, tz_name: str = "") -> float | None:
    """See :func:`_parse_reset_time`; a parser must never raise into the tick."""
    try:
        return _parse_reset_time(text, now, tz_name)
    except (OSError, OverflowError, ValueError, KeyError):
        return None


def _parse_reset_time(text: str, now: float, tz_name: str = "") -> float | None:
    """The epoch a limit message says the limit resets at, or ``None`` when it says nothing usable.

    Understood (examples):

    * clock time — ``resets 7pm``, ``resets at 7:30 PM (Europe/Berlin)``, ``try again at 19:00``
      (the next such time after *now*, in the named or configured zone);
    * date + time — ``try again at Oct 7th, 2026 4:03 PM``, ``resets Oct 9 at 7pm``, ISO ``2026-10-05T07:00:00Z``;
    * relative — ``resets in 2h 15m``, ``try again in 45 minutes``, ``in 1 day 3 hours``.

    Anything else, or a time more than 8 days away, is garbage: the caller retries on a fixed interval.
    """
    flat = re.sub(r"\s+", " ", text or "")
    if not flat.strip():
        return None
    zone_match = _ZONE_RE.search(flat)
    tz = _tzinfo(zone_match.group(1) if zone_match else tz_name, now)

    result: float | None = None
    relative = _REL_RE.search(flat)
    iso = _ISO_RE.search(flat)
    dated = next(
        (m for m in _DATE_RE.finditer(flat) if m.group(1)[:3].lower() in _MONTHS and _hm(m, 3)), None
    )
    clock = _TIME_ONLY_RE.search(flat)
    total = 0.0
    if relative:  # «resets 7pm» also looks like «number + letters»; only a real duration counts
        for amount, unit in _DURATION_PART.findall(relative.group(1)):
            total += int(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit[0].lower()]
    if total > 0:
        result = now + total
    elif iso:
        year, month, day, hour, minute = (int(iso.group(i)) for i in range(1, 6))
        second = int(iso.group(6) or 0)
        zone = iso.group(7)
        try:
            if zone:
                offset = timezone.utc if zone == "Z" else timezone(
                    (1 if zone[0] == "+" else -1)
                    * timedelta(hours=int(zone[1:3]), minutes=int(zone[-2:]))
                )
                result = datetime(year, month, day, hour, minute, second, tzinfo=offset).timestamp()
            else:
                result = datetime(year, month, day, hour, minute, second, tzinfo=tz).timestamp()
        except ValueError:
            result = None
    elif dated:
        h, m = _hm(dated, 3)  # type: ignore[misc]
        local_now = datetime.fromtimestamp(now, tz)
        year = int(dated.group(3)) if dated.group(3) else local_now.year
        try:
            candidate = datetime(year, _MONTHS[dated.group(1)[:3].lower()], int(dated.group(2)), h, m, tzinfo=tz)
            if not dated.group(3) and candidate.timestamp() < now - 3600:
                candidate = candidate.replace(year=year + 1)
            result = candidate.timestamp()
        except ValueError:
            result = None
    elif clock and _hm(clock, 0):
        h, m = _hm(clock, 0)  # type: ignore[misc]
        local_now = datetime.fromtimestamp(now, tz)
        candidate = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        result = candidate.timestamp()
    if result is None or result > now + MAX_RESET_AHEAD:
        return None
    return result


# ---------------------------------------------------------------- configuration
@dataclass
class Seat:
    """One wanted Worker seat: a title prefix and how many live tabs it should have."""

    name: str
    title_prefix: str
    count: int = 1
    command: str = ""
    keys: bool = False            # source [paths] keys_file into the launched shell (tools/worker.py)
    shell_windows: str = ""       # `--shell` for the tab on Windows only


@dataclass
class Route:
    """One way to run the Producer: a launch line, how to spot its limit, how to probe it.

    ``command`` is the exact launch shape; ``args_unattended`` is appended only while autonomy is ON
    (put ``--dangerously-skip-permissions`` there so the choice is one visible, removable line);
    ``keys = true`` sources ``[paths] keys_file`` into the launched shell (never printed);
    ``shell_windows`` is passed as ``--shell`` on Windows only (env-prefixed lines need a POSIX shell).
    """

    name: str
    command: str
    limit_patterns: list[str] = field(default_factory=list)
    probe: str = ""
    probe_timeout: float = DEFAULTS["probe_timeout_seconds"]
    title: str = ""
    agent: str = ""
    keys: bool = False
    shell_windows: str = ""
    args_unattended: str = ""
    enabled: bool = True

    def limit_regexes(self) -> list[re.Pattern]:
        compiled = []
        for pattern in self.limit_patterns:
            try:
                compiled.append(re.compile(pattern, re.I))
            except re.error:
                continue
        return compiled


@dataclass
class Limits:
    """``[limits]``: how many Worker tabs and how much machine load the Producer may add. 0 = that limit is off."""

    max_workers: int = 0
    min_free_ram_gb: float = 0.0
    max_load: float = 0.0          # CPU %, whole machine

    @property
    def measured(self) -> bool:
        return self.min_free_ram_gb > 0 or self.max_load > 0


@dataclass
class RemoteControlConfig:
    """``[remote_control]``: opt-in ``/remote-control`` for the owner's Claude Code tabs (off by default)."""

    enabled: bool = False
    titles: list[str] = field(default_factory=lambda: ["Producer", "Blitz"])
    settle_seconds: float = RC_SETTLE_SECONDS
    verify_seconds: float = RC_VERIFY_SECONDS


@dataclass
class GuardianConfig:
    """Everything the Guardian reads from ``producer.toml``; defaults match the brief."""

    interval_seconds: float = DEFAULTS["interval_seconds"]
    idle_nudge_minutes: float = DEFAULTS["idle_nudge_minutes"]
    worker_idle_minutes: float = DEFAULTS["worker_idle_minutes"]
    seat_max_hours: float = DEFAULTS["seat_max_hours"]
    producer_title: str = DEFAULTS["producer_title"]
    producer_command: str = DEFAULTS["producer_command"]
    bootstrap_prompt: str = DEFAULTS["bootstrap_prompt"]
    roster: list[Seat] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    ignore_titles: list[str] = field(default_factory=lambda: ["Blitz"])
    model_switch_choice: str = ""
    keys_file_raw: str = "~/.config/producer/keys.env"
    timezone_name: str = ""
    recover_minutes: float = DEFAULTS["recover_minutes"]
    probe_ttl_seconds: float = DEFAULTS["probe_ttl_seconds"]
    limit_confirm_ticks: int = DEFAULTS["limit_confirm_ticks"]
    absent_confirm_ticks: int = DEFAULTS["absent_confirm_ticks"]
    rotate_ask_minutes: float = DEFAULTS["rotate_ask_minutes"]
    rotate_max_asks: int = DEFAULTS["rotate_max_asks"]
    busy_stuck_minutes: float = DEFAULTS["busy_stuck_minutes"]
    startup_grace_seconds: float = DEFAULTS["startup_grace_seconds"]
    bootstrap_receipt_seconds: float = DEFAULTS["bootstrap_receipt_seconds"]
    bootstrap_max_attempts: int = DEFAULTS["bootstrap_max_attempts"]
    log_max_bytes: int = DEFAULTS["log_max_bytes"]
    wake_delay_seconds: float = DEFAULTS["wake_delay_seconds"]
    sleep_retry_minutes: float = DEFAULTS["sleep_retry_minutes"]
    wake_grace_seconds: float = DEFAULTS["wake_grace_seconds"]
    require_checkpoint: bool = True
    limits: Limits = field(default_factory=Limits)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)
    problems: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Direct construction (tests, callers) gets the same sanity as a parsed file.
        self.interval_seconds = max(MIN_INTERVAL, float(self.interval_seconds))

    @property
    def idle_nudge_seconds(self) -> float:
        return self.idle_nudge_minutes * 60.0

    @property
    def worker_idle_seconds(self) -> float:
        return self.worker_idle_minutes * 60.0

    @property
    def seat_max_seconds(self) -> float:
        return self.seat_max_hours * 3600.0

    @property
    def recover_seconds(self) -> float:
        return self.recover_minutes * 60.0

    @property
    def busy_stuck_seconds(self) -> float:
        return self.busy_stuck_minutes * 60.0

    @property
    def sleep_retry_seconds(self) -> float:
        return self.sleep_retry_minutes * 60.0

    def effective_routes(self) -> list[Route]:
        """Enabled routes in order; the legacy ``producer_command`` becomes one route when none are set."""
        routes = [route for route in self.routes if route.enabled and route.command.strip()]
        if not routes and not self.routes and self.producer_command.strip():
            routes = [Route(name="default", command=self.producer_command, title=self.producer_title)]
        return routes


class ConfigError(ValueError):
    """``producer.toml`` cannot be parsed (strict mode only)."""


def _as_float(section: dict, key: str, default: float, problems: list[str], minimum: float = 0.0,
              table: str = "guardian") -> float:
    raw = section.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        problems.append(f"[{table}] {key} = {raw!r} is not a number; using {default}")
        return float(default)
    if value < minimum:
        problems.append(f"[{table}] {key} = {value} is below {minimum}; using {max(minimum, default if minimum == 0 else minimum)}")
        return max(minimum, float(default) if minimum == 0 else minimum)
    return value


def _as_int(section: dict, key: str, default: int, problems: list[str], minimum: int = 0) -> int:
    raw = section.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        problems.append(f"[guardian] {key} = {raw!r} is not an integer; using {default}")
        return int(default)
    return max(minimum, value)


def _str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _parse_route(row: dict, problems: list[str]) -> Route | None:
    name = str(row.get("name") or "").strip()
    command = str(row.get("command") or "").strip()
    if not name or not command:
        problems.append(f"[[producer_routes]] needs name and command: {row!r}"[:160])
        return None
    patterns = _str_list(row.get("limit_patterns"))
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"route {name}: bad limit pattern {pattern!r}: {exc}")
    try:
        probe_timeout = float(row.get("probe_timeout") or DEFAULTS["probe_timeout_seconds"])
    except (TypeError, ValueError):
        probe_timeout = float(DEFAULTS["probe_timeout_seconds"])
    return Route(
        name=name,
        command=command,
        limit_patterns=patterns,
        probe=str(row.get("probe") or "").strip(),
        probe_timeout=probe_timeout,
        title=str(row.get("title") or "").strip(),
        agent=str(row.get("agent") or "").strip().casefold(),
        keys=bool(row.get("keys", False)),
        shell_windows=str(row.get("shell_windows") or "").strip(),
        args_unattended=str(row.get("args_unattended") or "").strip(),
        enabled=bool(row.get("enabled", True)),
    )


def _number(section: dict, key: str, table: str, problems: list[str], *, whole: bool = False) -> float:
    raw = section.get(key, 0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        problems.append(f"[{table}] {key} = {raw!r} is not a number; limit switched off")
        return 0.0
    if value < 0 or (whole and value != int(value)):
        problems.append(f"[{table}] {key} = {raw!r} must be {'a whole ' if whole else 'a '}number >= 0; limit switched off")
        return 0.0
    return value


def _parse_limits(raw: object, problems: list[str]) -> Limits:
    section = raw if isinstance(raw, dict) else {}
    max_load = _number(section, "max_load", "limits", problems)
    if max_load > 100:
        problems.append(f"[limits] max_load = {max_load} is a CPU percentage (0-100); limit switched off")
        max_load = 0.0
    return Limits(
        max_workers=int(_number(section, "max_workers", "limits", problems, whole=True)),
        min_free_ram_gb=_number(section, "min_free_ram_gb", "limits", problems),
        max_load=max_load,
    )


def _parse_remote_control(raw: object, problems: list[str]) -> RemoteControlConfig:
    section = raw if isinstance(raw, dict) else {}
    titles = _str_list(section.get("titles")) if "titles" in section else ["Producer", "Blitz"]
    return RemoteControlConfig(
        enabled=section.get("enabled", False) is True,
        titles=titles,
        settle_seconds=_as_float(section, "settle_seconds", RC_SETTLE_SECONDS, problems, table="remote_control"),
        verify_seconds=_as_float(section, "verify_seconds", RC_VERIFY_SECONDS, problems, table="remote_control"),
    )


def load_config(project: str | os.PathLike | None = None, *, strict: bool = False) -> GuardianConfig:
    """Read ``[guardian]`` / ``[[roster]]`` / ``[[producer_routes]]`` from ``producer.toml``.

    A bad file yields defaults plus a ``problems`` entry — unless ``strict``, which raises
    :class:`ConfigError` so a running Guardian keeps its last good configuration instead.
    """
    root = Path(project) if project else paths.PROJECT
    path = root / "producer.toml"
    problems: list[str] = []
    data: dict = {}
    if tomllib is None:
        problems.append("tomllib is unavailable (Python < 3.11); using defaults")
    elif path.is_file():
        try:
            loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError) as exc:
            if strict:
                raise ConfigError(f"producer.toml: {exc}") from exc
            problems.append(f"producer.toml cannot be read ({exc}); using defaults")
    section = data.get("guardian")
    section = section if isinstance(section, dict) else {}
    roster: list[Seat] = []
    rows = data.get("roster")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            prefix = str(row.get("title_prefix") or name).strip()
            if not prefix:
                continue
            try:
                # count = 0 means «this seat is switched off»; only a missing count defaults to 1.
                count = 1 if row.get("count") is None else int(row.get("count"))
            except (TypeError, ValueError):
                count = 1
            roster.append(
                Seat(name=name or prefix, title_prefix=prefix, count=max(0, count), command=str(row.get("command") or ""),
                     keys=bool(row.get("keys", False)), shell_windows=str(row.get("shell_windows") or "").strip())
            )
    routes: list[Route] = []
    route_rows = data.get("producer_routes")
    if isinstance(route_rows, list):
        for row in route_rows:
            if isinstance(row, dict):
                route = _parse_route(row, problems)
                if route:
                    routes.append(route)
    names = [route.name for route in routes]
    for duplicate in sorted({name for name in names if names.count(name) > 1}):
        problems.append(f"duplicate route name {duplicate!r}")
    project_section = data.get("project")
    project_section = project_section if isinstance(project_section, dict) else {}
    paths_section = data.get("paths")
    paths_section = paths_section if isinstance(paths_section, dict) else {}
    interval = _as_float(section, "interval_seconds", DEFAULTS["interval_seconds"], problems, MIN_INTERVAL)
    config = GuardianConfig(
        interval_seconds=interval,
        idle_nudge_minutes=_as_float(section, "idle_nudge_minutes", DEFAULTS["idle_nudge_minutes"], problems),
        worker_idle_minutes=_as_float(section, "worker_idle_minutes", DEFAULTS["worker_idle_minutes"], problems),
        seat_max_hours=_as_float(section, "seat_max_hours", DEFAULTS["seat_max_hours"], problems),
        producer_title=str(section.get("producer_title") or DEFAULTS["producer_title"]),
        producer_command=str(section.get("producer_command") or DEFAULTS["producer_command"]),
        bootstrap_prompt=str(section.get("bootstrap_prompt") or DEFAULTS["bootstrap_prompt"]),
        roster=roster,
        routes=routes,
        ignore_titles=_str_list(section.get("ignore_titles")) if "ignore_titles" in section else ["Blitz"],
        model_switch_choice=str(section.get("model_switch_choice") or ""),
        keys_file_raw=str(paths_section.get("keys_file") or "~/.config/producer/keys.env"),
        timezone_name=str(section.get("timezone") or project_section.get("timezone") or ""),
        recover_minutes=_as_float(section, "recover_minutes", DEFAULTS["recover_minutes"], problems),
        probe_ttl_seconds=_as_float(section, "probe_ttl_seconds", DEFAULTS["probe_ttl_seconds"], problems),
        limit_confirm_ticks=_as_int(section, "limit_confirm_ticks", DEFAULTS["limit_confirm_ticks"], problems, 1),
        absent_confirm_ticks=_as_int(section, "absent_confirm_ticks", DEFAULTS["absent_confirm_ticks"], problems, 1),
        rotate_ask_minutes=_as_float(section, "rotate_ask_minutes", DEFAULTS["rotate_ask_minutes"], problems),
        rotate_max_asks=_as_int(section, "rotate_max_asks", DEFAULTS["rotate_max_asks"], problems, 1),
        busy_stuck_minutes=_as_float(section, "busy_stuck_minutes", DEFAULTS["busy_stuck_minutes"], problems),
        startup_grace_seconds=_as_float(section, "startup_grace_seconds", DEFAULTS["startup_grace_seconds"], problems),
        bootstrap_receipt_seconds=_as_float(
            section, "bootstrap_receipt_seconds", DEFAULTS["bootstrap_receipt_seconds"], problems),
        bootstrap_max_attempts=_as_int(section, "bootstrap_max_attempts", DEFAULTS["bootstrap_max_attempts"], problems, 1),
        log_max_bytes=_as_int(section, "log_max_bytes", DEFAULTS["log_max_bytes"], problems, 10_000),
        wake_delay_seconds=_as_float(section, "wake_delay_seconds", DEFAULTS["wake_delay_seconds"], problems),
        sleep_retry_minutes=_as_float(section, "sleep_retry_minutes", DEFAULTS["sleep_retry_minutes"], problems, 1.0),
        wake_grace_seconds=_as_float(section, "wake_grace_seconds", DEFAULTS["wake_grace_seconds"], problems),
        require_checkpoint=bool(section.get("require_checkpoint", True)),
        limits=_parse_limits(data.get("limits"), problems),
        remote_control=_parse_remote_control(data.get("remote_control"), problems),
        problems=problems,
    )
    if not config.effective_routes():
        problems.append("no Producer route: set [[producer_routes]] (or [guardian] producer_command)")
    return config


def read_autonomy(runtime: Path | None = None) -> dict:
    state = _read_json((runtime or runtime_dir()) / "autonomy.json")
    return state if state else {"enabled": False}


# ---------------------------------------------------------------- keys + probes
def load_keys_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (optional ``export``, quotes) into a dict. Never printed or logged."""
    env: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key.strip():
            env[key.strip()] = value
    return env


def _bash() -> str | None:
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
        return None  # System32\bash.exe is WSL; never pick it by accident
    return shutil.which("bash")


def default_probe_runner(command: str, env: dict[str, str], timeout: float) -> int:
    """Run a probe command; exit 0 = alive. Keys are passed through the environment only."""
    merged = {**os.environ, **env}
    bash = _bash()
    argv: list[str] | str = [bash, "-c", command] if bash else command
    try:
        done = subprocess.run(
            argv, shell=not bash, env=merged, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return 127
    return done.returncode


def default_git_runner(project: Path, args: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            ["git", "-C", str(project), "-c", "core.fsmonitor=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


# ---------------------------------------------------------------- machine load and limits
@dataclass
class Machine:
    """One reading of the machine: free RAM (GB) and whole-machine CPU load (%). ``None`` = not measurable."""

    free_ram_gb: float | None = None
    cpu_percent: float | None = None
    error: str = ""


@dataclass
class LoadVerdict:
    """What the limits say now. ``block`` = reasons new Worker tabs are refused; ``pressure`` = RAM/CPU over the line."""

    block: list[str] = field(default_factory=list)
    pressure: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.block)

    def text(self) -> str:
        return BUSY_NUDGE.format(reasons="; ".join(self.block))


_WIN_MACHINE_SCRIPT = (
    "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory; "
    "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"
)


def default_command_runner(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout


def _win_available_gb() -> float | None:
    """Available physical RAM (free + standby cache, what a new process can really get) via kernel32."""
    try:
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return round(status.ullAvailPhys / 1024.0 ** 3, 2)
    except Exception:  # noqa: BLE001 - the CIM reading below is the fallback
        return None


def _read_machine_windows(runner: Callable[[list[str], float], tuple[int, str]] = default_command_runner,
                          ram_reader: Callable[[], float | None] = _win_available_gb) -> Machine:
    """CPU load (and a fallback for RAM) from CIM through PowerShell - ``wmic`` is gone from current Windows 11.

    Free RAM prefers the kernel's *available* figure: CIM ``FreePhysicalMemory`` leaves out the standby cache and so
    reads far too low on a machine that has simply been running for a while.
    """
    code, out = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_MACHINE_SCRIPT], 25.0)
    numbers = re.findall(r"\d+(?:[.,]\d+)?", out or "")
    if code != 0 or len(numbers) < 2:
        available = ram_reader()
        if available is None:
            return Machine(error=f"CIM read failed (exit {code}): {(out or '').strip()[:80]}")
        return Machine(free_ram_gb=available, error="CPU load unreadable (CIM failed); RAM only")
    free_kb, load = (float(item.replace(",", ".")) for item in numbers[:2])
    available = ram_reader()
    free_gb = available if available is not None else round(free_kb / 1024.0 / 1024.0, 2)
    return Machine(free_ram_gb=free_gb, cpu_percent=round(load, 1))


def _cpu_times(proc: Path) -> tuple[float, float] | None:
    try:
        first = (proc / "stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [float(item) for item in first[1:9]]
    except (OSError, IndexError, ValueError):
        return None
    if len(values) < 5:
        return None
    return sum(values), values[3] + values[4]  # total, idle + iowait


def _read_machine_linux(proc: str | os.PathLike = "/proc", sleep: Callable[[float], None] = time.sleep) -> Machine:
    root = Path(proc)
    free = None
    try:
        for line in (root / "meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                free = round(float(line.split()[1]) / 1024.0 / 1024.0, 2)
                break
    except (OSError, IndexError, ValueError):
        pass
    cpu = None
    before = _cpu_times(root)
    if before:
        sleep(0.5)
        after = _cpu_times(root)
        if after and after[0] > before[0]:
            cpu = round(100.0 * (1.0 - (after[1] - before[1]) / (after[0] - before[0])), 1)
    if free is None and cpu is None:
        return Machine(error="/proc/meminfo and /proc/stat are unreadable")
    return Machine(free_ram_gb=free, cpu_percent=cpu)


def read_machine() -> Machine:
    """Free RAM and CPU load of this machine; any failure is returned in ``error``, never raised."""
    try:
        return _read_machine_windows() if os.name == "nt" else _read_machine_linux()
    except Exception as exc:  # noqa: BLE001 - a flaky read must not stop a launch or a tick
        return Machine(error=f"{type(exc).__name__}: {exc}"[:120])


def assess_load(limits: Limits, machine: Machine | None, live_workers: int) -> LoadVerdict:
    """Compare the limits with a reading. An unmeasured signal is a note, never a block (a limit is a brake, not a gate)."""
    verdict = LoadVerdict()
    if limits.max_workers and live_workers >= limits.max_workers:
        verdict.block.append(f"workers {live_workers}/{limits.max_workers}")
        verdict.pressure = live_workers > limits.max_workers
    if limits.measured:
        if machine is None or (machine.free_ram_gb is None and machine.cpu_percent is None):
            verdict.notes.append("machine load not measurable" + (f": {machine.error}" if machine and machine.error else ""))
        else:
            if limits.min_free_ram_gb and machine.free_ram_gb is not None and machine.free_ram_gb < limits.min_free_ram_gb:
                verdict.block.append(f"free RAM {machine.free_ram_gb:g} GB < {limits.min_free_ram_gb:g} GB")
                verdict.pressure = True
            if limits.max_load and machine.cpu_percent is not None and machine.cpu_percent > limits.max_load:
                verdict.block.append(f"CPU {machine.cpu_percent:g}% > {limits.max_load:g}%")
                verdict.pressure = True
    return verdict


def compose_command(command: str, keys: bool, keys_file_raw: str) -> str:
    """The shell line for a tab: with ``keys`` the keys file is sourced first (never printed)."""
    command = command.strip()
    if keys:
        raw = keys_file_raw.replace("\\", "/")
        if raw.startswith("~"):
            raw = "$HOME" + raw[1:]
        command = f'set -a; . "{raw}"; set +a; ' + command
    return command


# ---------------------------------------------------------------- Claude Code Remote Control
def claude_composer(screen: str) -> bool:
    """True when a rendered screen shows Claude Code's composer (``❯`` line plus its status bar)."""
    lines = [line.rstrip() for line in str(screen or "").splitlines() if line.strip()]
    has_prompt = any(line.lstrip().startswith("❯") for line in lines[-8:])
    has_status = any("bypass permissions" in line or "for shortcuts" in line or "⏵⏵" in line
                     for line in lines[-6:])
    return has_prompt and has_status


def _status_bar(screen: str) -> str:
    lines = [line.rstrip() for line in str(screen or "").splitlines() if line.strip()]
    for line in reversed(lines[-8:]):
        if "bypass permissions" in line or "⏵⏵" in line or "for shortcuts" in line:
            return line
    return ""


def rc_is_on(screen: str) -> bool:
    """Remote Control is on: the status bar ends with ``/rc``, or its connected-session menu is open."""
    if "Disconnect this session" in screen and "Remote Control" in screen:
        return True
    return _status_bar(screen).endswith(RC_STATUS_MARK)


def rc_is_masked(screen: str) -> bool:
    """Background work (``1 shell``, ``2 monitors``) replaces ``/rc`` in the status bar: unknown, not off.

    Typing ``/remote-control`` on a connected pane opens the Disconnect menu over the owner's chat, so a masked bar
    is never answered with a keypress.
    """
    return bool(re.search(r"(?:^|[ ,])\d+ (?:shell|monitor|agent|background)", _status_bar(screen)))


# ---------------------------------------------------------------- the Guardian
class Guardian:
    """One watchdog instance. All side effects go through ``orca``, ``clock``, ``sleep``, git and probes."""

    def __init__(
        self,
        config: GuardianConfig | None = None,
        *,
        orca: Callable[..., dict] = _run_orca,
        clock: Callable[[], float] = time.time,
        runtime: str | os.PathLike | None = None,
        project: str | os.PathLike | None = None,
        sleep: Callable[[float], None] = time.sleep,
        probe_runner: Callable[[str, dict, float], int] = default_probe_runner,
        git_runner: Callable[[Path, list[str]], tuple[int, str, str]] = default_git_runner,
        config_loader: Callable[[], GuardianConfig] | None = None,
        token: str = "",
        machine: Callable[[], Machine] = read_machine,
    ) -> None:
        self.config = config or GuardianConfig()
        self.machine = machine
        self.orca = orca
        self.clock = clock
        self.sleep = sleep
        self.probe_runner = probe_runner
        self.git_runner = git_runner
        self.config_loader = config_loader
        self.token = token
        self.project = Path(project) if project else paths.PROJECT
        self.project_posix = Path(self.project).resolve().as_posix()
        self.runtime = Path(runtime) if runtime else runtime_dir()
        self.log_path = self.runtime / "guardian.log"
        self.heartbeat_path = self.runtime / "guardian.json"
        self.pid_path = self.runtime / "guardian.pid"
        self.state_path = self.runtime / STATE_FILE
        self.handover_path = self.project / "work" / "agents" / "state" / "HANDOVER.md"
        self.checkpoint_path = self.runtime / "producer-checkpoint.json"

        self.actions: list[dict] = []
        self.screens: dict[str, str] = {}        # handle -> last stable screen hash
        self.changed_at: dict[str, float] = {}   # handle -> epoch of last stable-screen change
        self.nudged_at: dict[str, float] = {}    # handle -> epoch of last nudge to that tab
        self.dialog: dict[str, dict] = {}        # handle -> {attempts, at} for dialog fixes
        self.limit_hits: dict[str, int] = {}     # handle -> consecutive ticks showing a limit message
        self.incident_at: dict[str, float] = {}  # incident key -> epoch last logged (rate limit)
        self.consecutive_errors = 0
        self.last_error = ""
        self._cfg_mtime: float | None = None
        self._view_cache: dict[str, tuple[str, str]] = {}
        self._probe_cache: dict[str, tuple[float, bool]] = {}
        self._verdict: LoadVerdict | None = None   # this tick's reading of the machine limits
        self._machine: Machine | None = None
        self._inventory_state = "unknown"
        self.state = self._load_state()
        if self.config_loader:
            self._cfg_mtime = self._config_mtime()

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        data = _read_json(self.state_path)
        state = {
            "binding": data.get("binding") if isinstance(data.get("binding"), dict) else None,
            "rotation": data.get("rotation") if isinstance(data.get("rotation"), dict) else None,
            "routes": data.get("routes") if isinstance(data.get("routes"), dict) else {},
            "retired": [str(h) for h in data.get("retired", [])] if isinstance(data.get("retired"), list) else [],
            "pendingClose": data.get("pendingClose") if isinstance(data.get("pendingClose"), list) else [],
            "sleeping": data.get("sleeping") if isinstance(data.get("sleeping"), dict) else {},
            "incidents": data.get("incidents") if isinstance(data.get("incidents"), list) else [],
            "absentTicks": int(data.get("absentTicks") or 0),
            "launchRetryAt": float(data.get("launchRetryAt") or 0.0),
            "launchFailures": int(data.get("launchFailures") or 0),
            "remoteControl": data.get("remoteControl") if isinstance(data.get("remoteControl"), dict) else {},
        }
        return state

    def _save_state(self) -> None:
        try:
            _write_json(self.state_path, self.state)
        except OSError:
            pass  # state is a recovery aid; the tick must survive a read-only runtime

    @property
    def binding(self) -> dict | None:
        return self.state["binding"]

    @property
    def producer_handle(self) -> str | None:
        binding = self.binding
        return str(binding["handle"]) if binding and binding.get("handle") else None

    @property
    def producer_started_at(self) -> float | None:
        binding = self.binding
        return float(binding["startedAt"]) if binding and binding.get("startedAt") is not None else None

    # ------------------------------------------------------------ logging / heartbeat
    def _write_log(self, row: dict) -> None:
        try:
            self.runtime.mkdir(parents=True, exist_ok=True)
            try:
                if self.log_path.stat().st_size > self.config.log_max_bytes:
                    for index in (2, 1):
                        older = self.log_path.with_name(f"guardian.log.{index}")
                        newer = self.log_path.with_name(f"guardian.log.{index - 1}") if index > 1 else self.log_path
                        if newer.exists():
                            os.replace(newer, older)
            except OSError:
                pass
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"{row.get('at', '?')} {row.get('kind', '?')} "
                    f"{row.get('handle') or '-'} {row.get('detail', '')}\n"
                )
        except OSError:
            pass  # a logging aid must never kill the tick

    def _action(self, kind: str, *, handle: str | None = None, detail: str = "") -> dict:
        row = {"at": _iso(self.clock()), "kind": kind, "handle": handle, "detail": detail}
        self.actions.append(row)
        del self.actions[:-ACTION_KEEP]
        self._write_log(row)
        return row

    def _incident(self, kind: str, detail: str, *, now: float, key: str = "", every: float = 600.0) -> None:
        """A visible, rate-limited problem the Guardian cannot fix itself (shown by ``status``)."""
        key = key or kind
        last = self.incident_at.get(key)
        if last is not None and now - last < every:
            return
        self.incident_at[key] = now
        self._action(f"incident-{kind}", detail=detail)
        incidents = self.state["incidents"]
        incidents.append({"at": _iso(now), "kind": kind, "detail": detail[:200]})
        del incidents[:-INCIDENT_KEEP]
        self._save_state()

    def _write_heartbeat(self, autonomy: bool, last_tick: float) -> None:
        binding = self.binding or {}
        sleeping = [
            {"handle": handle, **{k: v for k, v in entry.items() if k != "text"}}
            for handle, entry in self.state["sleeping"].items()
        ]
        rotation = self.state.get("rotation") or {}
        _write_json(
            self.heartbeat_path,
            {
                "pid": os.getpid(),
                "token": self.token,
                "lastTick": _iso(last_tick),
                "lastTickEpoch": last_tick,
                "intervalSeconds": self.config.interval_seconds,
                "autonomy": autonomy,
                "inventory": self._inventory_state,
                "producerHandle": binding.get("handle"),
                "route": binding.get("route"),
                "generation": binding.get("generation"),
                "bootstrapped": binding.get("bootstrapped"),
                "rotation": rotation.get("phase"),
                "sleeping": sleeping,
                "degraded": bool(self.consecutive_errors),
                "consecutiveErrors": self.consecutive_errors,
                "lastError": self.last_error,
                "machine": self._machine_row(),
                "remoteControl": {h: r.get("state") for h, r in self.state["remoteControl"].items()},
                "incidents": self.state["incidents"][-5:],
                "actions": self.actions[-5:],
            },
        )

    def _machine_row(self) -> dict | None:
        verdict, machine = self._verdict, self._machine
        if verdict is None:
            return None
        return {"freeRamGb": machine.free_ram_gb if machine else None, "cpuPercent": machine.cpu_percent if machine else None,
                "busy": verdict.busy, "reasons": verdict.block}

    # ------------------------------------------------------------ config reload
    def _config_mtime(self) -> float | None:
        try:
            return (self.project / "producer.toml").stat().st_mtime
        except OSError:
            return None

    def _maybe_reload(self, now: float) -> None:
        if not self.config_loader:
            return
        mtime = self._config_mtime()
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            self.config = self.config_loader()
        except ConfigError as exc:
            self._incident("config-invalid", f"{exc}; keeping the previous configuration", now=now)
            return
        self._action("config-reloaded", detail=f"{len(self.config.effective_routes())} route(s)")

    # ------------------------------------------------------------ orca wrappers
    def _view(self, handle: str, *, fresh: bool = False) -> tuple[str, str]:
        if not fresh and handle in self._view_cache:
            return self._view_cache[handle]
        payload = self.orca("terminal", "read", "--terminal", handle, "--screen", allow_error=True)
        text, draft = _screen_parts(payload)
        if not text and not draft:
            payload = self.orca("terminal", "read", "--terminal", handle, allow_error=True)
            text, draft = _screen_parts(payload)
        self._view_cache[handle] = (text, draft)
        return text, draft

    def _read_screen(self, handle: str, *, fresh: bool = False) -> str:
        text, draft = self._view(handle, fresh=fresh)
        return text + ("\n" + draft if draft else "")

    def _composer_free(self, handle: str) -> bool:
        text, draft = self._view(handle)
        return not draft.strip() and not _has_pasted(text)

    def _type(self, handle: str, text: str) -> None:
        self.orca("terminal", "send", "--terminal", handle, "--text", text, timeout=60)
        self.orca("terminal", "send", "--terminal", handle, "--enter", timeout=60)
        self._view_cache.pop(handle, None)

    def _close(self, handle: str) -> bool:
        try:
            self.orca("terminal", "close", "--terminal", handle, "--tab", timeout=90)
        except Exception:  # noqa: BLE001 - reported to the caller, never swallowed silently
            return False
        return True

    # ------------------------------------------------------------ inventory and ownership
    def _inventory(self) -> list[dict] | None:
        """This project's connected-or-not terminal rows, or ``None`` when the list is unverifiable."""
        try:
            payload = self.orca("terminal", "list", "--worktree", f"path:{self.project_posix}", "--limit", "500")
        except Exception as exc:  # noqa: BLE001 - any failure to read = unverifiable, never «empty»
            self._inventory_error = f"{type(exc).__name__}: {exc}"[:160]
            return None
        rows = _terminals(payload)
        if rows is None:
            self._inventory_error = "terminal list returned no 'terminals' array"
            return None
        mine = _norm_path(self.project_posix)
        return [row for row in rows if _norm_path(row.get("worktreePath")) == mine]

    def _is_agent(self, row: dict) -> bool:
        """A pane running an agent CLI. Hosts that do not report ``agentIdentity`` are given the benefit of the doubt."""
        if "agentIdentity" not in row:
            return True
        return bool(row.get("agentIdentity"))

    def _ignored(self, row: dict) -> bool:
        title = str(row.get("title", "")).casefold()
        return any(token.casefold() in title for token in self.config.ignore_titles if token)

    def _others(self, inventory: list[dict], producer_handle: str | None) -> list[dict]:
        """Connected agent panes of this project that are neither the Producer, retired, nor the owner's."""
        retired = set(self.state["retired"])
        return [
            row for row in inventory
            if row.get("connected") and str(row.get("handle")) != producer_handle
            and str(row.get("handle")) not in retired and self._is_agent(row) and not self._ignored(row)
        ]

    def _prune(self, inventory: list[dict]) -> None:
        live = {str(row.get("handle")) for row in inventory}
        for table in (self.screens, self.changed_at, self.nudged_at, self.dialog, self.limit_hits):
            for handle in [h for h in table if h not in live]:
                del table[handle]
        retired = [h for h in self.state["retired"] if h in live]
        if retired != self.state["retired"]:
            self.state["retired"] = retired
        rc = self.state["remoteControl"]
        for handle in [h for h in rc if h not in live]:
            del rc[handle]

    # ------------------------------------------------------------ routes
    def _route(self, name: str | None) -> Route | None:
        for route in self.config.effective_routes():
            if route.name == name:
                return route
        return None

    def _route_state(self, name: str) -> dict:
        return self.state["routes"].setdefault(name, {})

    def _keys_env(self) -> dict[str, str]:
        return load_keys_env(Path(os.path.expanduser(self.config.keys_file_raw)))

    def probe(self, route: Route, now: float, *, force: bool = False) -> bool | None:
        """Probe a route (cached ``probe_ttl_seconds``). ``None`` = the route declares no probe."""
        if not route.probe:
            return None
        cached = self._probe_cache.get(route.name)
        if cached and not force and now - cached[0] < self.config.probe_ttl_seconds:
            return cached[1]
        code = self.probe_runner(route.probe, self._keys_env(), route.probe_timeout)
        ok = code == 0
        self._probe_cache[route.name] = (now, ok)
        status = self._route_state(route.name)
        status["probeAt"], status["probeOk"] = now, ok
        return ok

    def _route_available(self, route: Route, now: float) -> bool:
        if not route.enabled:
            return False
        status = self._route_state(route.name)
        limited_at = status.get("limitedAt")
        if limited_at is not None:
            until = status.get("until")
            ready = float(until) if until is not None else float(limited_at) + self.config.recover_seconds
            if now < ready:
                return False
            if route.probe and not self.probe(route, now):
                return False
            status.pop("limitedAt", None)
            status.pop("until", None)
            self._action("route-recovered", detail=route.name)
            self._save_state()
            return True
        return self.probe(route, now) is not False

    def pick_route(self, reason: str, now: float, current: str | None = None) -> Route | None:
        """The route to launch on: ``rotation``/``start`` take the best available (so a recovered higher route
        is returned to); ``death`` tries the current route first; ``limit`` skips the one that just limited."""
        ordered = list(self.config.effective_routes())
        if reason == "death" and current:
            ordered.sort(key=lambda route: route.name != current)
        for route in ordered:
            if self._route_available(route, now):
                return route
        return None

    def mark_limited(self, route_name: str, now: float, until: float | None) -> None:
        status = self._route_state(route_name)
        status["limitedAt"] = now
        if until is not None:
            status["until"] = until
        else:
            status.pop("until", None)
        self._save_state()

    def _launch_command(self, route: Route) -> str:
        command = route.command.strip()
        autonomy = bool(read_autonomy(self.runtime).get("enabled"))
        if autonomy and route.args_unattended:
            command += " " + route.args_unattended
        return compose_command(command, route.keys, self.config.keys_file_raw)

    # ------------------------------------------------------------ discovery
    def _resolve_producer(self, inventory: list[dict], now: float) -> tuple[str, dict | None]:
        """``found`` / ``absent`` / ``ambiguous`` for the bound Producer pane."""
        binding = self.binding
        if binding and binding.get("handle"):
            for row in inventory:
                if str(row.get("handle")) != binding["handle"]:
                    continue
                incarnation = binding.get("incarnationId")
                if incarnation and row.get("incarnationId") and row["incarnationId"] != incarnation:
                    break  # same handle, new process: not the seat we launched
                if not row.get("connected"):
                    break
                if not incarnation and row.get("incarnationId"):
                    binding["incarnationId"] = row["incarnationId"]  # learn it once
                    self._save_state()
                return "found", row
            return "absent", None
        # Never bound (first run, or state lost): adopt a single, unambiguous, project-owned titled pane.
        want = self.config.producer_title.casefold()
        retired = set(self.state["retired"])
        candidates = [
            row for row in inventory
            if row.get("connected") and want in str(row.get("title", "")).casefold()
            and str(row.get("handle")) not in retired and self._is_agent(row) and not self._ignored(row)
        ]
        if len(candidates) > 1:
            return "ambiguous", None
        if not candidates:
            return "absent", None
        row = candidates[0]
        routes = self.config.effective_routes()
        self.state["binding"] = {
            "handle": str(row.get("handle")),
            "incarnationId": row.get("incarnationId") or "",
            "title": str(row.get("title", "")),
            "route": routes[0].name if routes else "",
            "startedAt": now,
            "generation": 1,
            "bootstrapped": True,
            "bootstrapAttempts": 0,
            "adopted": True,
        }
        self._save_state()
        self._action("adopt", handle=str(row.get("handle")), detail="single titled pane in this project")
        return "found", row

    def _seat_status(self, others: list[dict]) -> list[tuple[Seat, int]]:
        status: list[tuple[Seat, int]] = []
        for seat in self.config.roster:
            prefix = seat.title_prefix.casefold()
            live = sum(1 for row in others if prefix in str(row.get("title", "")).casefold())
            status.append((seat, live))
        return status

    def _short_seats(self, seat_status: list[tuple[Seat, int]]) -> list[tuple[Seat, int]]:
        return [(seat, live) for seat, live in seat_status if live < seat.count]

    def _surplus_seats(self, seat_status: list[tuple[Seat, int]]) -> list[tuple[Seat, int]]:
        return [(seat, live) for seat, live in seat_status if live > seat.count]

    def _note_change(self, handle: str, screen: str, now: float) -> bool:
        """Return whether the (spinner-normalised) screen changed since last seen; update the change clock."""
        digest = _screen_digest(screen)
        previous = self.screens.get(handle)
        if previous == digest:
            self.changed_at.setdefault(handle, now)
            return False
        self.screens[handle] = digest
        self.changed_at[handle] = now
        return True

    def _can_nudge(self, handle: str, now: float, window: float) -> bool:
        last = self.nudged_at.get(handle)
        return last is None or now - last >= window

    def _is_idle(self, handle: str, now: float, window: float) -> bool:
        return handle in self.changed_at and now - self.changed_at[handle] >= window

    # ------------------------------------------------------------ handover proof
    def handover_proof(self, since: float) -> tuple[bool, str]:
        """Has HANDOVER been rewritten since ``since`` AND committed (and the checkpoint refreshed)?"""
        try:
            mtime = self.handover_path.stat().st_mtime
        except OSError:
            return False, "HANDOVER.md not found"
        if mtime < since:
            return False, "HANDOVER not rewritten since the request"
        if self.config.require_checkpoint:
            try:
                if self.checkpoint_path.stat().st_mtime < since:
                    return False, "checkpoint not refreshed since the request"
            except OSError:
                return False, "checkpoint missing"
        relative = self.handover_path.relative_to(self.project).as_posix()
        code, out, err = self.git_runner(self.project, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or out.strip() != "true":
            if "not a git repository" in (err or "").lower():
                return True, "no git repository; HANDOVER mtime only"
            return False, f"git unavailable: {(err or out).strip()[:80]}"
        code, out, err = self.git_runner(self.project, ["status", "--porcelain", "--", relative])
        if code != 0:
            return False, f"git status failed: {err.strip()[:80]}"
        if out.strip():
            return False, "HANDOVER has uncommitted changes"
        return True, "HANDOVER committed"

    # ------------------------------------------------------------ launch + bootstrap
    def _brief_path(self) -> Path:
        """The project's brief page, named for the owner's language (`paths.PAGE_NAMES`), never a hard-coded name."""
        language = "ru"
        try:
            import tomllib
            data = tomllib.loads((self.project / "producer.toml").read_text(encoding="utf-8"))
            language = str((data.get("project") or {}).get("owner_language") or "ru").strip().lower()
        except (OSError, ValueError):
            pass
        preferred = "en" if language.startswith("en") else "ru"
        names = [paths.PAGE_NAMES[preferred]["brief"]]
        names += [table["brief"] for table in paths.PAGE_NAMES.values() if table["brief"] not in names]
        candidates = [self.project / "work" / name for name in names]
        return next((c for c in candidates if c.exists()), candidates[0])

    def brief_is_empty(self) -> bool:
        """True only when the brief is missing or its STATUS LINE still says «not filled in».

        Resolves the page by the owner's language and tests the status line alone, so a filled brief
        that keeps the phrase somewhere else does not keep re-triggering the kickoff note.
        """
        try:
            text = self._brief_path().read_text(encoding="utf-8")
        except OSError:
            return True
        if not text.strip():
            return True
        status = ""
        for line in text.splitlines():
            stripped = line.lstrip("_*# ").strip().casefold()
            if stripped.startswith(("статус", "status")):
                status = stripped
                break
        return "не заполнен" in status or "not filled" in status

    def _bootstrap_text(self, binding: dict) -> str:
        prompt = self.config.bootstrap_prompt.strip()
        note = str(binding.get("note") or "").strip()
        if self.brief_is_empty():
            note = (KICKOFF_NOTE + " " + note).strip()
        return (prompt + (" " + note if note else "")).strip()

    def _reason_note(self, reason: str, route: Route, proof: tuple[bool, str] | None) -> str:
        parts = {
            "limit": "Previous seat hit its limit; resume from HANDOVER.",
            "death": "Previous seat died; resume from HANDOVER.",
            "rotation": "Previous seat rotated at its age limit; HANDOVER is committed; resume from it.",
            "wake": "Previous seat hit its limit and the limit has reset; resume from HANDOVER.",
            "start": "",
        }.get(reason, "")
        if reason in ("death", "limit", "wake") and proof is not None and not proof[0]:
            parts += " No fresh committed HANDOVER was found; reconcile from the Run and git log."
        if route.name:
            parts += f" You are running on route {route.name}."
        return parts.strip()

    def _launch(self, route: Route, reason: str, now: float, old: dict | None = None,
                need_proof_since: float | None = None) -> str | None:
        previous = self.binding or {}
        proof = None
        if reason in ("death", "limit", "wake"):
            since = previous.get("startedAt") if previous.get("startedAt") is not None else 0.0
            proof = self.handover_proof(float(since))
        args = [
            "terminal", "create", "--worktree", f"path:{self.project_posix}",
            "--title", route.title or self.config.producer_title,
            "--command", self._launch_command(route),
        ]
        if os.name == "nt" and route.shell_windows:
            args += ["--shell", route.shell_windows]
        payload = self.orca(*args, allow_error=True)
        handle = _first_handle(_unwrap(payload)) or _first_handle(payload)
        if not handle:
            failures = int(self.state.get("launchFailures", 0)) + 1
            self.state["launchFailures"] = failures
            self.state["launchRetryAt"] = now + min(60.0 * 2 ** (failures - 1), 900.0)
            self._save_state()
            self._action("launch-failed", detail=f"route={route.name} orca returned no handle")
            return None
        self.state["launchFailures"] = 0
        self.state["launchRetryAt"] = 0.0
        if previous.get("handle") and previous["handle"] != handle:
            retired = self.state["retired"]
            retired.append(str(previous["handle"]))
            del retired[:-RETIRED_KEEP]
        self.state["binding"] = {
            "handle": handle,
            "incarnationId": _find_key(_unwrap(payload), "incarnationId") or "",
            "title": route.title or self.config.producer_title,
            "route": route.name,
            "startedAt": now,
            "generation": int(previous.get("generation") or 0) + 1,
            "bootstrapped": False,
            "bootstrapAttempts": 0,
            "reason": reason,
            "note": self._reason_note(reason, route, proof),
            "run": paths.configured_run() if hasattr(paths, "configured_run") else "",
        }
        self.state["rotation"] = None
        self.state["absentTicks"] = 0
        if old is not None:
            self.state["pendingClose"].append({
                "handle": str(old.get("handle")),
                "needProof": need_proof_since is not None,
                "since": need_proof_since,
            })
        self._save_state()
        self.screens.pop(handle, None)
        self.changed_at.pop(handle, None)
        self._action("launch", handle=handle, detail=f"route={route.name} reason={reason}")
        self._ensure_bootstrapped(now)
        return handle

    def _wait_ready(self, handle: str) -> None:
        """Wait for the TUI to be idle; if the host cannot, poll the screen for a prompt for up to the same time."""
        try:
            self.orca(
                "terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                "--timeout-ms", str(BOOTSTRAP_WAIT_MS),
            )
            return
        except Exception:  # noqa: BLE001 - unsupported/failed wait -> poll instead
            pass
        waited = 0.0
        while waited < BOOTSTRAP_WAIT_MS / 1000.0:
            text, _draft = self._view(handle, fresh=True)
            if text.strip() and not _is_busy(text):
                return
            self.sleep(3.0)
            waited += 3.0

    def _ensure_bootstrapped(self, now: float) -> bool:
        """Type the bootstrap brief and wait for a receipt on a FRESH screen. A send is not a launch."""
        binding = self.binding
        if not binding or binding.get("bootstrapped"):
            return True
        handle = str(binding["handle"])
        prompt = self._bootstrap_text(binding)
        if not prompt:
            binding["bootstrapped"] = True
            self._save_state()
            return True
        if int(binding.get("bootstrapAttempts", 0)) >= self.config.bootstrap_max_attempts:
            self._incident("bootstrap-failed", f"{handle}: brief not confirmed after "
                           f"{self.config.bootstrap_max_attempts} attempts", now=now, key=f"bootstrap:{handle}", every=1800.0)
            return False
        binding["bootstrapAttempts"] = int(binding.get("bootstrapAttempts", 0)) + 1
        self._wait_ready(handle)
        self._remote_control_launch(handle, now)
        self._type(handle, prompt)
        snippet = _alnum(prompt)[:30]
        deadline = self.config.bootstrap_receipt_seconds
        waited = 0.0
        pressed_enter = False
        while True:
            text, draft = self._view(handle, fresh=True)
            pasted = _has_pasted(text) or bool(draft.strip())
            seen = snippet and snippet in _alnum(text)
            if not pasted and (seen or _is_busy(text)):
                binding["bootstrapped"] = True
                self._save_state()
                self._action("bootstrap-verified", handle=handle, detail=f"attempt {binding['bootstrapAttempts']}")
                return True
            if pasted and not pressed_enter:
                # The paste is still in the composer: submit it once, then keep watching.
                self.orca("terminal", "send", "--terminal", handle, "--enter", timeout=60)
                self._view_cache.pop(handle, None)
                pressed_enter = True
            if waited >= deadline:
                break
            self.sleep(3.0)
            waited += 3.0
        self._save_state()
        self._action("bootstrap-unverified", handle=handle, detail=f"attempt {binding['bootstrapAttempts']}")
        return False

    def _process_pending_close(self, inventory: list[dict], now: float) -> None:
        """Close an old seat only once its successor is bootstrapped and (if required) HANDOVER is committed."""
        binding = self.binding
        if not self.state["pendingClose"]:
            return
        live = {str(row.get("handle")): row for row in inventory}
        remaining = []
        for item in self.state["pendingClose"]:
            handle = str(item.get("handle"))
            if handle not in live:
                continue  # gone: confirmed
            if not binding or not binding.get("bootstrapped"):
                remaining.append(item)
                continue
            if item.get("closeSentAt") and now - float(item["closeSentAt"]) < 120.0:
                remaining.append(item)
                continue
            if item.get("needProof"):
                ok, why = self.handover_proof(float(item.get("since") or 0.0))
                if not ok:
                    self._incident("close-deferred", f"{handle} left open: {why}", now=now, key=f"close:{handle}", every=1800.0)
                    remaining.append(item)
                    continue
            if self._close(handle):
                item["closeSentAt"] = now
                self._action("rotate-close", handle=handle, detail="successor is up")
            else:
                self._incident("close-failed", f"could not close {handle}", now=now, key=f"closefail:{handle}")
            remaining.append(item)
        self.state["pendingClose"] = remaining
        self._save_state()

    # ------------------------------------------------------------ dialogs
    def _fix_dialogs(self, inventory: list[dict], producer_handle: str | None, now: float) -> None:
        """Fix the CURRENT provider dialog on agent panes; bounded retries with backoff, never on a shell."""
        retired = set(self.state["retired"])
        for row in inventory:
            if not row.get("connected") or not self._is_agent(row) or self._ignored(row):
                continue
            handle = str(row.get("handle"))
            if not handle or handle in retired:
                continue
            text, draft = self._view(handle)
            tail = _tail(text, 14)
            lost = CONNECTION_LOST_RE.search(_tail(text, 8))
            switch = None if lost else MODEL_SWITCH_RE.search(tail)
            state = self.dialog.setdefault(handle, {"attempts": 0, "at": None})
            if not lost and not switch:
                state["attempts"], state["at"] = 0, None
                continue
            attempts = int(state["attempts"])
            if attempts >= 6:
                self._incident("dialog-persistent", f"{handle}: dialog still showing after {attempts} fixes",
                               now=now, key=f"dialog:{handle}", every=1800.0)
                continue
            delay = min(60.0 * 2 ** attempts, 900.0)
            if state["at"] is not None and now - float(state["at"]) < delay:
                continue
            if draft.strip():
                continue  # somebody is typing; do not collide with them
            if lost:
                self._type(handle, "continue")
                self._action("dialog-continue", handle=handle, detail=f"connection lost (try {attempts + 1})")
            else:
                identity = str(row.get("agentIdentity") or "").casefold()
                if identity and identity != "codex":
                    continue  # the model-switch fix is Codex's; never inject a digit elsewhere
                options = dict(OPTION_RE.findall(tail))
                choice = self.config.model_switch_choice if self.config.model_switch_choice in options else None
                if choice is None:
                    keep = [digit for digit, label in options.items() if MODEL_KEEP_RE.search(label)]
                    choice = keep[0] if keep else None
                if choice is None:
                    self._incident("dialog-unrecognised", f"{handle}: model dialog without a recognisable keep option",
                                   now=now, key=f"dialogu:{handle}")
                    continue
                self._type(handle, choice)
                self._action("dialog-model-switch", handle=handle, detail=f"chose option {choice}")
            state["attempts"], state["at"] = attempts + 1, now

    # ------------------------------------------------------------ limits and the wake-up alarm
    def _limit_patterns(self, route: Route | None) -> list[re.Pattern]:
        if route and route.limit_patterns:
            return route.limit_regexes()
        return [re.compile(p, re.I) for p in GENERIC_LIMIT_PATTERNS]

    def _all_limit_patterns(self) -> list[re.Pattern]:
        compiled = [pattern for route in self.config.effective_routes() for pattern in route.limit_regexes()]
        return compiled + [re.compile(p, re.I) for p in GENERIC_LIMIT_PATTERNS]

    @staticmethod
    def _limit_text(screen: str, patterns: list[re.Pattern]) -> str:
        tail = _tail(screen, 12)
        return tail if any(pattern.search(tail) for pattern in patterns) else ""

    def _sleep_seat(self, handle: str, kind: str, text: str, route: str, now: float) -> dict:
        """Mark a seat sleeping until the parsed reset time + wake delay (or a fixed retry when unparsable)."""
        reset = parse_reset_time(text, now, self.config.timezone_name)
        parsed = reset is not None
        wake_at = (reset + self.config.wake_delay_seconds) if parsed else now + self.config.sleep_retry_seconds
        entry = {
            "kind": kind, "route": route, "since": now, "wakeAt": wake_at,
            "wakeAtIso": _iso(wake_at), "parsed": parsed,
        }
        self.state["sleeping"][handle] = entry
        self._save_state()
        self._action(
            "sleep", handle=handle,
            detail=f"{kind} limit; wake {_iso(wake_at)}" + ("" if parsed else " (reset time unreadable; retry interval)"),
        )
        return entry

    def _wake_due(self, handle: str, now: float) -> bool:
        entry = self.state["sleeping"].get(handle)
        return bool(entry) and now >= float(entry["wakeAt"])

    def _wake(self, handle: str, kind: str, now: float) -> bool:
        """Type the wake line into a sleeping seat once its composer is free; returns whether it was sent."""
        if not self._composer_free(handle):
            return False
        text = WAKE_TEXT if kind == "producer" else WORKER_WAKE_TEXT
        self._type(handle, text)
        self.state["sleeping"].pop(handle, None)
        self.limit_hits.pop(handle, None)
        self.dialog.pop(handle, None)
        self.state.setdefault("wokeAt", {})[handle] = now
        self._save_state()
        self._action("wake", handle=handle, detail=kind)
        return True

    def _recently_woken(self, handle: str, now: float) -> bool:
        woke = self.state.get("wokeAt", {}).get(handle)
        return woke is not None and now - float(woke) < self.config.wake_grace_seconds

    def _check_workers_for_limits(self, others: list[dict], now: float) -> None:
        """Sleep Workers that show a limit message; wake sleeping ones that are due."""
        patterns = self._all_limit_patterns()
        live = {str(row.get("handle")) for row in others}
        for handle in [h for h, e in self.state["sleeping"].items() if e.get("kind") == "worker" and h not in live]:
            self.state["sleeping"].pop(handle, None)  # tab gone; the Producer owns relaunching Workers
        for row in others:
            handle = str(row.get("handle"))
            sleeping = self.state["sleeping"].get(handle)
            if sleeping:
                if self._wake_due(handle, now):
                    self._wake(handle, "worker", now)
                continue
            if self._recently_woken(handle, now):
                continue
            screen = self._read_screen(handle)
            if _is_busy(screen):
                self.limit_hits.pop(handle, None)
                continue
            text = self._limit_text(screen, patterns)
            if not text:
                self.limit_hits.pop(handle, None)
                continue
            self.limit_hits[handle] = self.limit_hits.get(handle, 0) + 1
            if self.limit_hits[handle] >= self.config.limit_confirm_ticks:
                self._sleep_seat(handle, "worker", text, "", now)

    def _check_producer_limit(self, row: dict, now: float) -> str:
        """Returns ``handled`` when the seat is limited/sleeping (the tick must not nudge or rotate it)."""
        handle = str(row.get("handle"))
        binding = self.binding or {}
        route = self._route(binding.get("route"))
        sleeping = self.state["sleeping"].get(handle)
        if sleeping:
            if self._wake_due(handle, now):
                if self._wake(handle, "producer", now):
                    if route:
                        status = self._route_state(route.name)
                        status.pop("limitedAt", None)
                        status.pop("until", None)
                        self._save_state()
                return "handled" if handle in self.state["sleeping"] else "woke"
            return "handled"
        if self._recently_woken(handle, now):
            return "clear"
        screen = self._read_screen(handle)
        text = self._limit_text(screen, self._limit_patterns(route))
        if not text or _is_busy(screen):
            self.limit_hits.pop(handle, None)
            return "clear"
        self.limit_hits[handle] = self.limit_hits.get(handle, 0) + 1
        if self.limit_hits[handle] < self.config.limit_confirm_ticks:
            return "handled"
        reset = parse_reset_time(text, now, self.config.timezone_name)
        wake_at = (reset + self.config.wake_delay_seconds) if reset is not None else None
        if route:
            self.mark_limited(route.name, now, wake_at)
        self._action("limit", handle=handle, detail=f"route={route.name if route else '?'} reset={_iso(reset) if reset else 'unknown'}")
        successor = self.pick_route("limit", now, binding.get("route"))
        if successor is not None and (route is None or successor.name != route.name):
            self._launch(successor, "limit", now, old=row, need_proof_since=float(binding.get("startedAt") or 0.0))
            return "handled"
        # No other route: the alarm IS the fallback — sleep this seat and wake it at reset (never give up the night).
        self._sleep_seat(handle, "producer", text, route.name if route else "", now)
        return "handled"

    # ------------------------------------------------------------ nudges
    def _nudge_text(self, seat_status: list[tuple[Seat, int]], idle_workers: list[tuple[str, int]],
                    sleeping: list[str] | None = None, verdict: LoadVerdict | None = None) -> str:
        roster_line = ", ".join(f"{seat.name} {live}/{seat.count}" for seat, live in seat_status if seat.count) or "none"
        base = (
            "GUARDIAN: autonomy ON and you look idle. Check the mailbox, the roster "
            f"({roster_line}), the TODO top block; dispatch or accept now."
        )
        extras: list[str] = []
        busy = verdict is not None and verdict.busy
        short = [] if busy else self._short_seats(seat_status)  # a busy machine launches nothing, so it is not "short"
        if busy:
            extras.append(verdict.text())
        if short:
            named = ", ".join(f"{seat.name} {live}/{seat.count}" for seat, live in short)
            extras.append(f"Roster short: {named} — launch the missing seat.")
        surplus = self._surplus_seats(seat_status)
        if surplus:
            named = ", ".join(f"{seat.name} {live}/{seat.count}" for seat, live in surplus)
            extras.append(f"Roster surplus: {named}.")
        if idle_workers:
            named = ", ".join(f"{handle} {minutes}m" for handle, minutes in idle_workers)
            extras.append(f"Worker idle: {named} — wake or close it.")
        if sleeping:
            extras.append(f"Sleeping on a limit (the Guardian wakes them at reset): {', '.join(sleeping)}.")
        return base + (" " + " ".join(extras) if extras else "")

    def _idle_workers(self, others: list[dict], now: float) -> list[tuple[str, int]]:
        idle: list[tuple[str, int]] = []
        for row in others:
            handle = str(row.get("handle"))
            if not handle or handle in self.state["sleeping"]:
                continue
            screen = self._read_screen(handle)
            changed = self._note_change(handle, screen, now)
            if changed or _is_busy(screen) or _is_finished(screen):
                continue
            if self._is_idle(handle, now, self.config.worker_idle_seconds):
                idle.append((handle, int((now - self.changed_at[handle]) / 60.0)))
        return idle

    # ------------------------------------------------------------ rotation
    def _rotation_step(self, row: dict, screen: str, now: float) -> bool:
        """Two-phase succession. Returns True when this tick's seat handling is finished."""
        binding = self.binding or {}
        handle = str(row.get("handle"))
        rotation = self.state.get("rotation")
        started = float(binding.get("startedAt") or now)
        busy = _is_busy(screen)
        if not rotation:
            if now - started < self.config.seat_max_seconds:
                return False
            if busy and now - started < self.config.seat_max_seconds + 900.0:
                return False  # let the turn finish; ask at the first quiet moment (or after 15 min regardless)
            if not self._composer_free(handle):
                return False
            self._type(handle, ROTATE_TEXT)
            self.state["rotation"] = {"phase": "asked", "askedAt": now, "asks": 1, "lastAskAt": now, "handle": handle}
            self._save_state()
            self._action("rotate-now", handle=handle, detail=f"seat age {int(now - started)}s")
            return True
        if rotation.get("handle") != handle:
            self.state["rotation"] = None  # a different pane now: the old request is void
            self._save_state()
            return False
        asked_at = float(rotation["askedAt"])
        ok, why = self.handover_proof(asked_at)
        if ok and not busy:
            route = self.pick_route("rotation", now, binding.get("route"))
            if route is None:
                self._incident("no-live-route", "rotation due but no route is available", now=now)
                return True
            self._launch(route, "rotation", now, old=row, need_proof_since=asked_at)
            return True
        if now - float(rotation.get("lastAskAt") or asked_at) >= self.config.rotate_ask_minutes * 60.0:
            if int(rotation.get("asks", 1)) < self.config.rotate_max_asks:
                if self._composer_free(handle):
                    self._type(handle, ROTATE_TEXT)
                    rotation["asks"] = int(rotation.get("asks", 1)) + 1
                    rotation["lastAskAt"] = now
                    self._save_state()
                    self._action("rotate-now", handle=handle, detail=f"ask {rotation['asks']}: {why}")
            else:
                self._incident("rotation-stalled", f"{handle} has not committed HANDOVER ({why}); it stays open "
                               "— nothing is closed without a committed HANDOVER", now=now, every=1800.0)
        return True

    # ------------------------------------------------------------ one tick
    # ------------------------------------------------------------ machine limits
    def load_verdict(self, live_workers: int) -> LoadVerdict:
        """The limits against this tick's reading (read once per tick, and only when a RAM/CPU limit is set)."""
        if self._verdict is None:
            limits = self.config.limits
            if limits.measured and self._machine is None:
                self._machine = self.machine()
            self._verdict = assess_load(limits, self._machine, live_workers)
            if self._verdict.notes:
                self._incident("machine-unmeasured", "; ".join(self._verdict.notes),
                               now=self.clock(), key="machine", every=3600.0)
        return self._verdict

    # ------------------------------------------------------------ Remote Control (opt-in)
    def _is_claude_tab(self, row: dict, text: str) -> bool:
        identity = str(row.get("agentIdentity") or "").casefold()
        return identity == "claude" if identity else claude_composer(text)

    def _rc_role(self, row: dict) -> str | None:
        """Which owner tab this is: the bound Producer, or a title on [remote_control] titles; else None."""
        handle = str(row.get("handle"))
        if handle and handle == self.producer_handle:
            return "producer"
        title = str(row.get("title") or "").casefold().strip()
        for word in self.config.remote_control.titles:
            word = word.casefold().strip()
            if word and (title == word or title.startswith((word + " ", word + "#"))):
                return word
        return None

    def _rc_record(self, row: dict, role: str, state: str, now: float) -> dict:
        record = {"incarnation": str(row.get("incarnationId") or ""), "role": role, "state": state,
                  "title": str(row.get("title") or ""), "at": now}
        self.state["remoteControl"][str(row.get("handle"))] = record
        self._save_state()
        return record

    def _rc_api_route(self, handle: str) -> bool:
        """The bound Producer runs on a key/proxy route: Remote Control needs the owner's claude.ai login."""
        binding = self.binding or {}
        route = self._route(binding.get("route")) if str(binding.get("handle")) == handle else None
        return bool(route and route.keys)

    def _rc_type(self, handle: str, row: dict, role: str, now: float) -> None:
        self._type(handle, RC_COMMAND)
        self._rc_record(row, role, "typed", now)
        self._action("remote-control-typed", handle=handle, detail=f"role={role}")

    def _remote_control_launch(self, handle: str, now: float) -> None:
        """Right after a Producer tab is ready and BEFORE the brief: a busy Producer is never at a free prompt later."""
        cfg = self.config.remote_control
        if not cfg.enabled or str(handle) in self.state["remoteControl"]:
            return
        row = next((r for r in (self._inventory() or []) if str(r.get("handle")) == handle), None)
        if row is None:
            return
        text, draft = self._view(handle, fresh=True)
        if not self._is_claude_tab(row, text):
            return
        if self._rc_api_route(handle):
            self._rc_record(row, "producer", "skipped-api-route", now)
            return
        if draft.strip() or _is_busy(text) or not claude_composer(text) or rc_is_on(text) or rc_is_masked(text):
            return  # the per-tick step catches it at the next free prompt
        self._rc_type(handle, row, "producer", now)
        waited = 0.0
        while waited < cfg.settle_seconds:
            self.sleep(2.0)
            waited += 2.0
            if rc_is_on(self._view(handle, fresh=True)[0]):
                self.state["remoteControl"][handle]["state"] = "on"
                self._save_state()
                break

    def _remote_control_step(self, inventory: list[dict], now: float) -> None:
        """Type /remote-control ONCE per opted-in Claude Code tab, then only watch.

        Never retypes: on a connected pane a second /remote-control opens the Disconnect menu over the owner's chat.
        A tab that never shows /rc is reported (actionable), not retried.
        """
        cfg = self.config.remote_control
        if not cfg.enabled:
            return
        records = self.state["remoteControl"]
        retired = set(self.state["retired"])
        for row in inventory:
            handle = str(row.get("handle"))
            if not row.get("connected") or not handle or handle in retired:
                continue
            role = self._rc_role(row)
            if role is None:
                continue
            record = records.get(handle)
            incarnation = str(row.get("incarnationId") or "")
            if record and record.get("incarnation") == incarnation:
                if record.get("state") == "typed":
                    text, _draft = self._view(handle)
                    if rc_is_on(text):
                        record["state"] = "on"
                        self._save_state()
                        self._action("remote-control-on", handle=handle, detail=f"role={role}")
                    elif now - float(record.get("at") or now) >= cfg.verify_seconds:
                        record["state"] = "unverified"
                        self._save_state()
                        self._incident(
                            "remote-control-unverified",
                            f"{handle} ({row.get('title')}): '{RC_COMMAND}' was typed but '{RC_STATUS_MARK}' never showed "
                            f"in the status bar - open the tab and type {RC_COMMAND} yourself (the Guardian does not retype it)",
                            now=now, key=f"rc:{handle}", every=3600.0)
                continue
            text, draft = self._view(handle)
            if not self._is_claude_tab(row, text):
                continue
            if self._rc_api_route(handle):
                self._rc_record(row, role, "skipped-api-route", now)
                continue
            if rc_is_on(text):
                self._rc_record(row, role, "on", now)
                continue
            if rc_is_masked(text) or _is_busy(text) or draft.strip() or not claude_composer(text):
                continue  # unknown or not a free prompt: look again next tick
            self._rc_type(handle, row, role, now)

    def tick(self, now: float | None = None) -> list[dict]:
        """One supervise iteration. Returns the actions taken this tick."""
        now = self.clock() if now is None else float(now)
        before = len(self.actions)
        self._view_cache = {}
        self._verdict = self._machine = None
        self._maybe_reload(now)
        autonomy = bool(read_autonomy(self.runtime).get("enabled"))

        inventory = self._inventory()
        if inventory is None:
            self._inventory_state = "unverifiable"
            self._incident("inventory-unreadable", getattr(self, "_inventory_error", "terminal list failed")
                           + " — no tab is touched until it reads again", now=now, every=900.0)
            self._write_heartbeat(autonomy, now)
            return self.actions[before:]
        self._inventory_state = "observed"
        self._prune(inventory)
        self._remote_control_step(inventory, now)

        if not autonomy:
            self._tick_autonomy_off(inventory, now)
            self._write_heartbeat(False, now)
            return self.actions[before:]

        status, row = self._resolve_producer(inventory, now)
        if status == "ambiguous":
            self._incident("ambiguous-producer", "several titled panes in this project; none adopted", now=now)
            self._write_heartbeat(True, now)
            return self.actions[before:]

        # An agent that left its pane (shell survives, agent process gone) is a dead Producer too.
        if status == "found" and row is not None:
            route = self._route((self.binding or {}).get("route"))
            started = float((self.binding or {}).get("startedAt") or now)
            if (route and route.agent and "agentIdentity" in row and now - started >= self.config.startup_grace_seconds
                    and str(row.get("agentIdentity") or "").casefold() != route.agent):
                self.state["absentTicks"] = int(self.state["absentTicks"]) + 1
                if self.state["absentTicks"] < self.config.absent_confirm_ticks:
                    self._save_state()
                    self._write_heartbeat(True, now)
                    return self.actions[before:]
                self._action("producer-agent-gone", handle=str(row.get("handle")),
                             detail=f"pane alive, agent {row.get('agentIdentity') or 'none'} != {route.agent}")
                status = "absent-agent-gone"

        if status in ("absent", "absent-agent-gone"):
            self._handle_absent(inventory, row, status, now)
            self._write_heartbeat(True, now)
            return self.actions[before:]

        assert row is not None
        self.state["absentTicks"] = 0
        handle = str(row.get("handle"))
        others = self._others(inventory, handle)

        if not (self.binding or {}).get("bootstrapped"):
            self._ensure_bootstrapped(now)
        self._process_pending_close(inventory, now)

        self._fix_dialogs(inventory, handle, now)
        self._check_workers_for_limits(others, now)
        if self._check_producer_limit(row, now) == "handled":
            self._write_heartbeat(True, now)
            return self.actions[before:]

        screen = self._read_screen(handle)
        if self._rotation_step(row, screen, now):
            self._write_heartbeat(True, now)
            return self.actions[before:]

        # Idle Producer, roster drift, idle Workers -> one nudge naming everything, at a safe prompt only.
        busy = _is_busy(screen)
        changed = self._note_change(handle, screen, now)
        idle_for = now - self.changed_at.get(handle, now)
        busy_stuck = busy and not changed and idle_for >= self.config.busy_stuck_seconds
        producer_idle = (not busy) and not changed and self._is_idle(handle, now, self.config.idle_nudge_seconds)
        seat_status = self._seat_status(others)
        verdict = self.load_verdict(len(others))
        short = [] if verdict.busy else self._short_seats(seat_status)
        idle_workers = self._idle_workers(others, now)
        sleeping_workers = [h for h, e in self.state["sleeping"].items() if e.get("kind") == "worker"]

        want_nudge = producer_idle or busy_stuck or short or idle_workers or verdict.pressure
        if want_nudge and (not busy or busy_stuck) and self._composer_free(handle) \
                and self._can_nudge(handle, now, self.config.idle_nudge_seconds):
            self._type(handle, self._nudge_text(seat_status, idle_workers, sleeping_workers, verdict))
            self.nudged_at[handle] = now
            detail = []
            if producer_idle:
                detail.append("idle")
            if busy_stuck:
                detail.append("busy-stuck")
            if short:
                detail.append("roster-short")
            if idle_workers:
                detail.append("worker-idle")
            if verdict.pressure:
                detail.append("machine-busy")
            self._action("nudge", handle=handle, detail="+".join(detail))

        self._write_heartbeat(True, now)
        return self.actions[before:]

    def _tick_autonomy_off(self, inventory: list[dict], now: float) -> None:
        """Autonomy OFF: only the first-run job — open ONE Producer tab if this project has none.

        No nudges, roster reminders, alarm, rotation or dialog fixes (those are the night behaviours).
        A Producer that was bound and later vanished is NOT relaunched: with autonomy off the owner may
        have closed it on purpose.
        """
        status, row = self._resolve_producer(inventory, now)
        if status == "ambiguous":
            self._incident("ambiguous-producer", "several titled panes in this project; none adopted", now=now)
            return
        if status == "found":
            if not (self.binding or {}).get("bootstrapped"):
                self._ensure_bootstrapped(now)
            return
        if self.binding:
            self._incident("producer-gone", "the Producer tab is gone and autonomy is OFF; not relaunching "
                           "(autonomy on = relaunch)", now=now, every=3600.0)
            return
        if now < float(self.state.get("launchRetryAt") or 0.0):
            return
        route = self.pick_route("start", now)
        if route is None:
            self._incident("no-live-route", "no Producer is open and no route is available", now=now, every=900.0)
            return
        self._launch(route, "start", now)

    def _handle_absent(self, inventory: list[dict], row: dict | None, status: str, now: float) -> None:
        binding = self.binding
        if binding and status == "absent":
            self.state["absentTicks"] = int(self.state["absentTicks"]) + 1
            if self.state["absentTicks"] < self.config.absent_confirm_ticks:
                self._save_state()
                return  # one missing read is not death
        if now < float(self.state.get("launchRetryAt") or 0.0):
            return
        rotation = self.state.get("rotation")
        reason = "rotation" if rotation else ("death" if binding else "start")
        route = self.pick_route("death" if binding else "start", now, (binding or {}).get("route"))
        if route is None:
            self.state["sleeping"].pop(str((binding or {}).get("handle")), None)
            self._incident("no-live-route", "Producer is gone and no route is available (all limited, probe-failed "
                           "or disabled); waiting for one to recover", now=now, every=900.0)
            return
        # The old pane (if any) was a dead seat or a shell: it needs no HANDOVER proof to be closed.
        self._launch(route, reason if reason != "rotation" else "death", now,
                     old=row if status == "absent-agent-gone" else None)
        if binding and binding.get("handle"):
            self.state["sleeping"].pop(str(binding["handle"]), None)
            self._save_state()


# ---------------------------------------------------------------- instance lock
class InstanceLock:
    """An OS-level exclusive lock on a file, held for the life of the process (survives PID reuse)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.handle = stream
        return True

    def release(self) -> None:
        stream, self.handle = self.handle, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()

    @classmethod
    def held(cls, path: Path) -> bool:
        probe = cls(path)
        if probe.acquire():
            probe.release()
            return False
        return True


def _pid_info(runtime: Path) -> dict:
    path = runtime / "guardian.pid"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if raw.isdigit():
        return {"pid": int(raw)}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- supervise / daemon
def _sleep_slices(guardian: Guardian, interval: float, stop_path: Path) -> bool:
    """Sleep ``interval`` in 1 s slices; returns True when a stop was requested."""
    remaining = max(MIN_INTERVAL, interval)
    while remaining > 0:
        if stop_path.exists():
            return True
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
    return stop_path.exists()


def _supervise_loop(guardian: Guardian, interval: float, once: bool) -> int:
    stop_path = guardian.runtime / "guardian.stop"
    guardian._action("supervise-start", detail=f"interval={interval}s once={once}")
    try:
        while True:
            try:
                guardian.tick()
                guardian.consecutive_errors, guardian.last_error = 0, ""
            except Exception as exc:  # noqa: BLE001 - one bad tick must never kill the watchdog
                guardian.consecutive_errors += 1
                guardian.last_error = f"{type(exc).__name__}: {exc}"[:200]
                guardian._action("tick-error", detail=guardian.last_error)
                try:
                    guardian._write_heartbeat(True, guardian.clock())
                except OSError:
                    pass
            if once:
                break
            if _sleep_slices(guardian, interval, stop_path):
                guardian._action("supervise-stop", detail="stop requested")
                break
    except KeyboardInterrupt:
        pass
    return 0


def command_supervise(args: argparse.Namespace) -> int:
    config = load_config()
    token = getattr(args, "token", "") or secrets.token_hex(8)
    guardian = Guardian(config, config_loader=lambda: load_config(strict=True), token=token)
    lock = InstanceLock(guardian.runtime / "guardian.lock")
    if not args.once:
        if not lock.acquire():
            print("another guardian instance holds the lock; exiting", file=sys.stderr)
            return 3
        guardian.runtime.mkdir(parents=True, exist_ok=True)
        _write_json(guardian.pid_path, {"pid": os.getpid(), "token": token, "startedAt": _iso(time.time())})
        try:
            (guardian.runtime / "guardian.stop").unlink()
        except FileNotFoundError:
            pass
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a closing shell must not take the night with it
        guardian._write_heartbeat(bool(read_autonomy(guardian.runtime).get("enabled")), time.time())
    interval = max(MIN_INTERVAL, args.interval if args.interval else config.interval_seconds)
    try:
        return _supervise_loop(guardian, interval, args.once)
    finally:
        if not args.once:
            info = _pid_info(guardian.runtime)
            if info.get("token") == token:
                try:
                    guardian.pid_path.unlink()
                except FileNotFoundError:
                    pass
            lock.release()


def command_start(args: argparse.Namespace) -> int:
    runtime = runtime_dir()
    lock_path = runtime / "guardian.lock"
    if InstanceLock.held(lock_path):
        print(f"guardian already running: pid {_pid_info(runtime).get('pid', '?')}")
        return 0
    token = secrets.token_hex(8)
    script = Path(__file__).resolve()
    argv = [sys.executable, str(script), "supervise", "--token", token]
    if args.interval:
        argv += ["--interval", str(args.interval)]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        argv[0] = str(pythonw) if pythonw.is_file() else sys.executable
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # leave the launching tool's job object when it allows breakaway; otherwise settle for detached
            process = subprocess.Popen(argv, creationflags=flags | 0x01000000, **kwargs)
        except OSError:
            process = subprocess.Popen(argv, creationflags=flags, **kwargs)
    else:
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
    # Readiness handshake: the child must publish OUR token before we say «started».
    deadline = time.time() + 20.0
    while time.time() < deadline:
        beat = _read_json(runtime / "guardian.json")
        if beat.get("token") == token:
            print(f"guardian started: pid {process.pid}")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.5)
    if InstanceLock.held(lock_path):
        print(f"guardian already running: pid {_pid_info(runtime).get('pid', '?')}")
        return 0
    print("guardian failed to start (no readiness handshake); see .runtime/guardian.log", file=sys.stderr)
    return 1


def command_stop(args: argparse.Namespace) -> int:
    runtime = runtime_dir()
    lock_path = runtime / "guardian.lock"
    if not InstanceLock.held(lock_path):
        print("guardian not running")
        try:
            (runtime / "guardian.pid").unlink()
        except FileNotFoundError:
            pass
        return 0
    info = _pid_info(runtime)
    pid = info.get("pid")
    (runtime / "guardian.stop").write_text("stop", encoding="utf-8")
    for _ in range(30):  # graceful: the loop checks the flag every second
        if not InstanceLock.held(lock_path):
            print(f"guardian stopped: pid {pid}")
            return 0
        time.sleep(0.5)
    beat = _read_json(runtime / "guardian.json")
    if not pid or (info.get("token") and beat.get("token") != info.get("token")):
        print("guardian did not stop and its pid file does not match the running instance; not killing anything",
              file=sys.stderr)
        return 1
    try:
        if os.name == "nt":
            done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
            if done.returncode != 0:
                print(f"taskkill failed (rc {done.returncode}) for pid {pid}", file=sys.stderr)
                return 1
        else:
            os.kill(int(pid), signal.SIGTERM)
    except OSError as exc:
        print(f"could not stop pid {pid}: {exc}", file=sys.stderr)
        return 1
    for _ in range(20):
        if not InstanceLock.held(lock_path):
            print(f"guardian stopped: pid {pid}")
            return 0
        time.sleep(0.5)
    print(f"guardian pid {pid} is still running after the stop attempt", file=sys.stderr)
    return 1


def command_status(args: argparse.Namespace) -> int:
    runtime = runtime_dir()
    state = read_autonomy(runtime)
    held = InstanceLock.held(runtime / "guardian.lock")
    info = _pid_info(runtime)
    heartbeat = _read_json(runtime / "guardian.json")
    saved = _read_json(runtime / STATE_FILE)
    config = load_config()
    stale = False
    if held and heartbeat.get("lastTickEpoch"):
        interval = float(heartbeat.get("intervalSeconds") or config.interval_seconds)
        stale = time.time() - float(heartbeat["lastTickEpoch"]) > 3 * interval + 30
    label = "running" if held and not stale else ("STALE — alive but not ticking (hung?)" if held else "stopped")
    print(f"Guardian: {label}" + (f" pid {info.get('pid')}" if held and info.get("pid") else ""))
    if heartbeat.get("lastTick"):
        print(f"last tick: {heartbeat.get('lastTick')}")
    if heartbeat.get("degraded"):
        print(f"DEGRADED: {heartbeat.get('consecutiveErrors')} tick error(s) in a row — {heartbeat.get('lastError')}")
    if heartbeat.get("inventory") == "unverifiable":
        print("terminal list: UNVERIFIABLE — no tab is being touched")
    line = "autonomy: " + ("ON" if state.get("enabled") else "OFF")
    if state.get("enabled") and state.get("since"):
        line += f" since {state['since']}"
    print(line)
    if state.get("objective"):
        print(f"objective: {state['objective']}")
    binding = saved.get("binding") if isinstance(saved.get("binding"), dict) else {}
    if binding.get("handle"):
        print(f"producer: {binding.get('handle')} route {binding.get('route')} gen {binding.get('generation')}"
              + ("" if binding.get("bootstrapped") else " (bootstrap NOT confirmed)"))
    machine = heartbeat.get("machine")
    if isinstance(machine, dict):
        print("machine: " + ("BUSY - " + "; ".join(machine.get("reasons") or []) if machine.get("busy") else "ok")
              + f" (free RAM {machine.get('freeRamGb')} GB, CPU {machine.get('cpuPercent')}%)")
    remote = saved.get("remoteControl") if isinstance(saved.get("remoteControl"), dict) else {}
    for handle, entry in remote.items():
        print(f"remote control: {handle} {entry.get('title') or ''} - {entry.get('state')}")
    sleeping = saved.get("sleeping") if isinstance(saved.get("sleeping"), dict) else {}
    for handle, entry in sleeping.items():
        print(f"sleeping: {handle} ({entry.get('kind')}) wakes {entry.get('wakeAtIso')}"
              + ("" if entry.get("parsed") else " [reset unreadable: retry interval]"))
    for problem in config.problems:
        print(f"config: {problem}")
    incidents = saved.get("incidents")
    if isinstance(incidents, list) and incidents:
        print("incidents:")
        for row in incidents[-3:]:
            print(f"  {row.get('at', '?')} {row.get('kind', '?')} {row.get('detail', '')}")
    actions = heartbeat.get("actions")
    if isinstance(actions, list) and actions:
        print("recent actions:")
        for row in actions[-5:]:
            print(f"  {row.get('at', '?')} {row.get('kind', '?')} {row.get('handle') or '-'} {row.get('detail', '')}")
    return 0


def command_autonomy(args: argparse.Namespace) -> int:
    runtime = runtime_dir()
    path = runtime / "autonomy.json"
    if args.mode == "status":
        state = read_autonomy(runtime)
        line = "autonomy " + ("ON" if state.get("enabled") else "OFF")
        if state.get("enabled"):
            if state.get("since"):
                line += f" since {state['since']}"
            if state.get("by"):
                line += f" by {state['by']}"
            if state.get("objective"):
                line += f" — {state['objective']}"
        print(line)
        return 0
    state = _read_json(path)
    if args.mode == "on":
        state = {
            "enabled": True,
            "since": _iso(time.time()),
            "by": args.by or _whoami(),
            "objective": args.objective or str(state.get("objective") or ""),
        }
        _write_json(path, state)
        print(f"autonomy ON since {state['since']} (objective: {state['objective'] or '—'})")
        return 0
    state["enabled"] = False
    state.setdefault("since", _iso(time.time()))
    state.setdefault("by", args.by or _whoami())
    state.setdefault("objective", args.objective or "")
    _write_json(path, state)
    print("autonomy OFF")
    return 0


# ---------------------------------------------------------------- routes CLI + producer.toml editing
ROUTE_HEADER = "[[producer_routes]]"
_TABLE_RE = re.compile(r"^\s*\[")


def _blocks(lines: list[str], header: str) -> list[tuple[int, int]]:
    """Line spans [start, end) of each ``header`` block; trailing blank/comment lines stay outside the span."""
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() == header:
            end = index + 1
            while end < len(lines) and not _TABLE_RE.match(lines[end]):
                end += 1
            while end > index + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
                end -= 1
            spans.append((index, end))
            index = end
        else:
            index += 1
    return spans


def _block_name(block: list[str]) -> str:
    for line in block:
        match = re.match(r'^\s*name\s*=\s*"([^"]*)"', line)
        if match:
            return match.group(1)
    return ""


def _write_toml_checked(path: Path, text: str) -> None:
    if tomllib is not None:
        tomllib.loads(text)  # never write a file that does not parse
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def reorder_routes(text: str, primary: str) -> str:
    """Move the named ``[[producer_routes]]`` block to the front; the other blocks keep their order."""
    lines = text.splitlines()
    spans = _blocks(lines, ROUTE_HEADER)
    blocks = [lines[start:end] for start, end in spans]
    names = [_block_name(block) for block in blocks]
    if primary not in names:
        raise KeyError(f"no route named {primary!r}; routes: {', '.join(names) or 'none'}")
    order = [names.index(primary)] + [i for i in range(len(blocks)) if i != names.index(primary)]
    out: list[str] = []
    cursor = 0
    for slot, (start, end) in enumerate(spans):
        out.extend(lines[cursor:start])
        out.extend(blocks[order[slot]])
        cursor = end
    out.extend(lines[cursor:])
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def set_route_enabled(text: str, name: str, enabled: bool) -> str:
    lines = text.splitlines()
    for start, end in _blocks(lines, ROUTE_HEADER):
        if _block_name(lines[start:end]) != name:
            continue
        flag = f"enabled = {'true' if enabled else 'false'}"
        for index in range(start, end):
            if re.match(r"^\s*enabled\s*=", lines[index]):
                lines[index] = flag
                break
        else:
            lines.insert(start + 1, flag)
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise KeyError(f"no route named {name!r}")


def command_route(args: argparse.Namespace) -> int:
    path = paths.PROJECT / "producer.toml"
    try:
        text = path.read_text(encoding="utf-8")
        if args.primary:
            new = reorder_routes(text, args.primary)
            message = f"route {args.primary} is now primary"
        elif args.disable:
            new = set_route_enabled(text, args.disable, False)
            message = f"route {args.disable} disabled"
        elif args.enable:
            new = set_route_enabled(text, args.enable, True)
            message = f"route {args.enable} enabled"
        else:
            print("give --primary NAME, --disable NAME or --enable NAME", file=sys.stderr)
            return 2
        _write_toml_checked(path, new)
    except KeyError as exc:
        print(str(exc.args[0]), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"producer.toml not changed: {exc}", file=sys.stderr)
        return 1
    print(message + " (a running Guardian reloads it; the live seat switches at the next rotation, limit or death)")
    return 0


def command_routes(args: argparse.Namespace) -> int:
    config = load_config()
    runtime = runtime_dir()
    saved = _read_json(runtime / STATE_FILE)
    current = (saved.get("binding") or {}).get("route") if isinstance(saved.get("binding"), dict) else None
    guardian = Guardian(config, runtime=runtime)
    now = time.time()
    routes = list(config.routes) or config.effective_routes()
    results: dict[str, bool | None] = {}
    if not args.no_probe:
        keys = guardian._keys_env()
        with ThreadPoolExecutor(max_workers=max(1, len(routes))) as pool:
            futures = {
                route.name: pool.submit(default_probe_runner, route.probe, keys, route.probe_timeout)
                for route in routes if route.probe and route.enabled
            }
            results = {name: future.result() == 0 for name, future in futures.items()}
    for index, route in enumerate(routes, start=1):
        status = (saved.get("routes") or {}).get(route.name, {}) if isinstance(saved.get("routes"), dict) else {}
        flags = []
        if route.name == current:
            flags.append("CURRENT")
        if not route.enabled:
            flags.append("disabled")
        if status.get("limitedAt") is not None:
            until = status.get("until")
            flags.append("limited" + (f" until {_iso(float(until))}" if until else f" since {_iso(float(status['limitedAt']))}"))
        if args.no_probe:
            probe = "not probed"
        elif not route.probe:
            probe = "no probe"
        elif not route.enabled:
            probe = "skipped"
        else:
            probe = "alive" if results.get(route.name) else "PROBE FAILED"
        print(f"{index}. {route.name:<22} {route.agent or '-':<9} probe: {probe:<13} {' '.join(flags)}")
        print(f"     {route.command[:110]}")
    if not routes:
        print("no routes configured")
    elif not args.no_probe:
        print("probe: alive = the route's own probe command exited 0 - liveness only (usually "
              "`<cli> --version`), never a model call, login, credit or quota")
    for problem in config.problems:
        print(f"config: {problem}")
    del now
    return 0


SERVICE_HINT = """\
`guardian.py start` is idempotent (an OS lock keeps a single instance), so a periodic `start` is a restart-on-crash:

Windows (Task Scheduler, every 5 minutes, current user):
  schtasks /Create /SC MINUTE /MO 5 /TN "producer-guardian-{tag}" /TR "\\"{python}\\" \\"{script}\\" start" /F
  remove: schtasks /Delete /TN "producer-guardian-{tag}" /F

Linux (systemd user timer):
  ~/.config/systemd/user/producer-guardian-{tag}.service
    [Service] Type=oneshot ; ExecStart={python} {script} start
  ~/.config/systemd/user/producer-guardian-{tag}.timer
    [Timer] OnBootSec=1min ; OnUnitActiveSec=5min ; [Install] WantedBy=timers.target
  systemctl --user enable --now producer-guardian-{tag}.timer
"""


def command_service(args: argparse.Namespace) -> int:
    tag = hashlib.sha1(str(paths.PROJECT).encode("utf-8")).hexdigest()[:8]
    print(SERVICE_HINT.format(tag=tag, python=sys.executable, script=Path(__file__).resolve()))
    return 0


# ---------------------------------------------------------------- presets
PRESET_SINGLE_CLAUDE = '''\
# Preset «single-claude» — ONE Claude subscription, every seat on the same account (same 5-hour window).
# The Guardian cannot add quota: keep the roster small and let the wake-up alarm carry the night.
[[producer_routes]]
name = "claude-sonnet"
title = "Producer Claude Sonnet"
agent = "claude"
command = "claude --model claude-sonnet-5-5 --effort medium"
args_unattended = "--dangerously-skip-permissions"
limit_patterns = ["usage limit", "\\\\d-hour limit", "weekly limit", "rate limit", "out of credits", "\\\\b429\\\\b"]
probe = "claude --version"

[[roster]]
name = "sonnet"
title_prefix = "Sonnet"
count = 1
command = "claude --model claude-sonnet-5-5 --effort medium"

[[roster]]
name = "haiku"
title_prefix = "Haiku"
count = 1
command = "claude --model claude-haiku-4-5-20251001"
'''


def apply_preset(text: str, preset: str) -> str:
    """Replace every ``[[producer_routes]]`` and ``[[roster]]`` block with the preset's."""
    lines = text.splitlines()
    spans = _blocks(lines, ROUTE_HEADER) + _blocks(lines, "[[roster]]")
    drop = {i for start, end in spans for i in range(start, end)}
    kept = [line for i, line in enumerate(lines) if i not in drop]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n\n" + preset


def command_preset(args: argparse.Namespace) -> int:
    # One name everywhere: `solo-claude` (setup.py's name) is the same preset as the legacy `single-claude`.
    if args.name not in ("single-claude", "solo-claude"):
        print(f"unknown preset {args.name!r}; available: solo-claude", file=sys.stderr)
        return 1
    if not args.apply:
        print(PRESET_SINGLE_CLAUDE)
        print("# (printed only; add --apply to replace the [[producer_routes]] and [[roster]] blocks in producer.toml)")
        return 0
    path = paths.PROJECT / "producer.toml"
    try:
        _write_toml_checked(path, apply_preset(path.read_text(encoding="utf-8"), PRESET_SINGLE_CLAUDE))
    except (OSError, ValueError) as exc:
        print(f"producer.toml not changed: {exc}", file=sys.stderr)
        return 1
    print("preset solo-claude applied to producer.toml")
    return 0


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Small generic Producer Guardian (stdlib only).")
    sub = parser.add_subparsers(dest="command")

    autonomy = sub.add_parser("autonomy", help="turn the autonomy flag on/off or show it")
    autonomy.add_argument("mode", choices=["on", "off", "status"])
    autonomy.add_argument("--objective", default="", help="what the Producer should work on")
    autonomy.add_argument("--by", default="", help="who turned it on (default: the OS user)")
    autonomy.set_defaults(handler=command_autonomy)

    supervise = sub.add_parser("supervise", help="run the watchdog loop (foreground)")
    supervise.add_argument("--once", action="store_true", help="run one tick then exit")
    supervise.add_argument("--interval", type=float, default=None, help="override the interval")
    supervise.add_argument("--token", default="", help=argparse.SUPPRESS)
    supervise.set_defaults(handler=command_supervise)

    start = sub.add_parser("start", help="launch supervise detached; single instance, readiness handshake")
    start.add_argument("--interval", type=float, default=None)
    start.set_defaults(handler=command_start)

    sub.add_parser("stop", help="stop the detached Guardian (graceful, then verified kill)").set_defaults(
        handler=command_stop)
    sub.add_parser("status", help="running/stale, route, sleeping seats, incidents, last actions").set_defaults(
        handler=command_status)

    routes = sub.add_parser("routes", help="list Producer routes and probe each one")
    routes.add_argument("--no-probe", action="store_true", help="do not run the probes")
    routes.set_defaults(handler=command_routes)

    route = sub.add_parser("route", help="reorder or switch a Producer route in producer.toml")
    route.add_argument("--primary", default="", help="move this route to the front")
    route.add_argument("--disable", default="", help="set enabled = false on this route")
    route.add_argument("--enable", default="", help="set enabled = true on this route")
    route.set_defaults(handler=command_route)

    preset = sub.add_parser("preset", help="print or apply a route+roster preset (solo-claude)")
    preset.add_argument("name")
    preset.add_argument("--apply", action="store_true", help="write it into producer.toml")
    preset.set_defaults(handler=command_preset)

    sub.add_parser("service", help="print how to make the Guardian restart after a crash/reboot").set_defaults(
        handler=command_service)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

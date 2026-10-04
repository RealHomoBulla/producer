#!/usr/bin/env python3
"""usage.py — one screen of every Producer route's live quota. Read-only; never prints a key.

    python tools/usage.py              # the human screen
    python tools/usage.py --json       # the same rows as machine-readable JSON
    python tools/usage.py --probe-free # also smoke-probe the free OpenCode ids (slow)

What it reads, and how:

* **Claude / Codex** — ``orca account list --json`` → ``rateLimits``: the rolling 5-hour and
  weekly used percent and the reset time, exactly the shape ``orca_orchestrator.py`` parses
  (``_provider_summary`` / ``_usage_reset_text``, orca_orchestrator.py:2511, 12067).
* **OpenCode Go** — one read-only request to the messages endpoint
  (ROUTING.md §Probes): HTTP 200 proves the route is live, HTTP 429 names the limiting window.
* **OpenRouter** — ``GET /api/v1/auth/key`` → ``limit_remaining``.
* **AGY** — its one-line smoke probe (``--print=ok``).
* **Free OpenCode ids** — ``opencode run -m <id> "Reply with exactly: ok"``, only with
  ``--probe-free`` because each call is slow.

Keys are read from ``producer.toml [paths] keys_file`` (default ``~/.config/producer/keys.env``),
sourced into the probes; a missing CLI or key is reported as «not configured», never an error.
Stdlib only; Windows and Linux. No key, and no fragment of one, is ever printed or logged.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import paths
except ImportError:  # running from another cwd: tools/ sits beside this file
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import paths

OPENCODE_GO_MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
AGY_MODEL = "gemini-3.8-flash-high"

# The free ids worth a smoke probe (ROUTING.md §FREE LANES). A probe is the only proof of quota:
# the free endpoints publish no dependable count.
FREE_IDS = (
    "opencode/space-bunny-free",
    "opencode/muse-spark-1.3-contributor-free",
    "opencode/mimo-v2.6-flash-free",
)

# Names the routes use. A value in the process environment is used when the keys file has none:
# ROUTING.md §Credentials says some keys live in the environment and only some in the file.
KNOWN_KEYS = ("OPENROUTER_API_KEY", "OPENCODE_API_KEY", "DEEPSEEK_API_KEY",
              "COMMAND_CODE_API_KEY", "COMMANDCODE_API_KEY")

# ``exe`` default: resolve via PATH; an explicit ``None`` means «this CLI is not installed».
_UNSET = object()


# ---------------------------------------------------------------- the HTTP seam (tests inject this)
def _http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
    """Return ``(status, text)``; a connection failure is ``(0, message)``, never a traceback.

    A curl-like ``User-Agent`` is set unless the caller gave one: OpenCode's edge answers a bare
    urllib request with Cloudflare ``error code: 1010`` even though the key is valid.
    """
    if not any(key.lower() == "user-agent" for key in headers):
        headers = {**headers, "User-Agent": "curl/8.5.0"}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), (exc.read().decode("utf-8", "replace") if exc.fp else "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


# ---------------------------------------------------------------- keys
def load_keys(path: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (optional ``export``, quotes); never printed or logged.

    The process environment fills any known name the file does not define (ROUTING.md §Credentials:
    some keys live in the environment), so a missing file line still probes when the shell has it.
    """
    target = Path(path) if path else paths.keys_file()
    env: dict[str, str] = {}
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        line = raw.strip()
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
    process = os.environ if environ is None else environ
    for name in KNOWN_KEYS:
        if not env.get(name) and process.get(name):
            env[name] = str(process[name])
    return env


# ---------------------------------------------------------------- shared row helpers
def _row(route: str, label: str, **extra) -> dict:
    row = {"route": route, "label": label, "state": "not-configured", "detail": "", "error": None,
           "windows": {}}
    row.update(extra)
    return row


# English weekday abbreviations, chosen explicitly so a Russian locale console cannot turn the day
# into «??» (strftime('%a') follows the process locale).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _reset_text(window: dict, now: datetime | None = None) -> str | None:
    """A reset time built from ``resetsAt`` in LOCAL time; the raw description only as a fallback.

    Same-day resets read as ``22:50``; a reset beyond today carries its date, e.g.
    ``Fri 10.10 15:00``. Building it ourselves avoids Orca's Cyrillic day name (``пн 15:00``),
    which a console without Cyrillic shows as ``?? 15:00``.
    """
    raw = window.get("resetsAt")
    try:
        epoch = float(raw)
    except (TypeError, ValueError):
        epoch = None
    if epoch is not None:
        if epoch > 10_000_000_000:
            epoch /= 1000.0
        try:
            moment = datetime.fromtimestamp(epoch).astimezone()
        except (OSError, OverflowError, ValueError):
            moment = None
        if moment is not None:
            current = now if now is not None else datetime.now().astimezone()
            if moment.date() == current.date():
                return moment.strftime("%H:%M")
            return f"{_WEEKDAYS[moment.weekday()]} {moment.strftime('%d.%m %H:%M')}"
    description = window.get("resetDescription")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return None


def _windows(row: dict) -> dict:
    windows: dict = {}
    for key, label in (("session", "5h"), ("weekly", "week"), ("fableWeekly", "fable")):
        window = row.get(key)
        if isinstance(window, dict) and window.get("usedPercent") is not None:
            windows[label] = {"usedPercent": window.get("usedPercent"), "reset": _reset_text(window)}
    return windows


def _window_from_error(text: str) -> str | None:
    """Name the limiting window from a 429 body (``metadata.limitName`` or a phrase)."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        data = {}
    if isinstance(data, dict):
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        for candidate in (metadata.get("limitName"), data.get("limitName")):
            if candidate:
                return str(candidate)
        error = data.get("error")
        message = error.get("message") if isinstance(error, dict) else data.get("message")
        if message:
            return str(message)[:120]
    low = (text or "").lower()
    for phrase in ("monthly", "5 hour", "5-hour", "weekly", "daily"):
        if phrase in low:
            return phrase
    return None


# ---------------------------------------------------------------- orca meter (Claude / Codex)
def parse_orca_limits(payload: dict) -> list[dict]:
    """Claude and Codex windows from an ``orca account list --json`` payload."""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    limits = result.get("rateLimits") if isinstance(result, dict) else None
    limits = limits if isinstance(limits, dict) else {}
    rows: list[dict] = []
    for provider, label in (("claude", "Claude"), ("codex", "Codex")):
        row = limits.get(provider)
        if not isinstance(row, dict):
            rows.append(_row(provider, label, detail="no meter from Orca"))
            continue
        if row.get("status") not in (None, "ok"):
            rows.append(_row(provider, label, state="unavailable",
                             error=str(row.get("error") or row.get("status"))))
            continue
        windows = _windows(row)
        state = "ok" if windows else "unavailable"
        rows.append(_row(provider, label, state=state, windows=windows,
                         detail="" if windows else "meter reported no window"))
    return rows


def orca_limits(runner=subprocess.run, exe=_UNSET) -> list[dict]:
    if exe is _UNSET:
        exe = shutil.which("orca")
    if not exe:
        return [_row("claude", "Claude", detail="orca CLI not on PATH"),
                _row("codex", "Codex", detail="orca CLI not on PATH")]
    try:
        done = runner([exe, "account", "list", "--json"], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return [_row("claude", "Claude", state="error", error=str(exc)),
                _row("codex", "Codex", state="error", error=str(exc))]
    if getattr(done, "returncode", 1) != 0:
        detail = ((done.stderr or done.stdout or "").strip()[:160] or "shifted")
        return [_row("claude", "Claude", state="error", error=detail),
                _row("codex", "Codex", state="error", error=detail)]
    try:
        payload = json.loads(done.stdout or "{}")
    except ValueError as exc:
        return [_row("claude", "Claude", state="error", error=f"unreadable JSON: {exc}"),
                _row("codex", "Codex", state="error", error=f"unreadable JSON: {exc}")]
    return parse_orca_limits(payload)


# ---------------------------------------------------------------- route probes
def probe_opencode_go(keys: dict[str, str], http=_http, timeout: float = 25) -> dict:
    row = _row("opencode-go", "OpenCode Go (DeepSeek 4.1 Flash)")
    key = keys.get("OPENCODE_API_KEY")
    if not key:
        row["detail"] = "OPENCODE_API_KEY not set"
        return row
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "x-opencode-session": "probe", "content-type": "application/json"}
    body = json.dumps({"model": "deepseek-v4.1-flash", "max_tokens": 16,
                       "messages": [{"role": "user", "content": "ping"}]}).encode("utf-8")
    status, text = http("POST", OPENCODE_GO_MESSAGES_URL, headers, body, timeout)
    if status == 200:
        row.update(state="ok", detail="live (probe)")
    elif status == 429:
        window = _window_from_error(text)
        row.update(state="limited", detail=f"429 — {window + ' window' if window else 'limit reached'}")
    else:
        row.update(state="error", detail=f"HTTP {status}", error=(text or "").strip()[:200] or None)
    return row


def probe_openrouter(keys: dict[str, str], http=_http, timeout: float = 20) -> dict:
    row = _row("openrouter", "OpenRouter (DeepSeek 4.1 Flash)")
    key = keys.get("OPENROUTER_API_KEY")
    if not key:
        row["detail"] = "OPENROUTER_API_KEY not set"
        return row
    status, text = http("GET", OPENROUTER_KEY_URL,
                        {"Authorization": f"Bearer {key}", "Accept": "application/json"}, None, timeout)
    if status != 200:
        row.update(state="error", detail=f"HTTP {status}", error=(text or "").strip()[:200] or None)
        return row
    try:
        data = (json.loads(text or "{}").get("data") or {})
    except ValueError as exc:
        row.update(state="error", error=f"unreadable JSON: {exc}")
        return row
    remaining = data.get("limit_remaining")
    limit = data.get("limit")
    if remaining is None:
        row.update(state="ok", detail="key valid (no spend cap reported)")
        return row
    try:
        detail = f"${float(remaining):.2f} remaining"
        if limit is not None:
            detail += f" of ${float(limit):.2f}"
    except (TypeError, ValueError):
        detail = f"{remaining} remaining"
    row.update(state="ok", detail=detail)
    return row


def probe_agy(runner=subprocess.run, exe=_UNSET, timeout: float = 120) -> dict:
    row = _row("agy", "AGY (Gemini 3.8 Flash High)")
    if exe is _UNSET:
        exe = shutil.which("agy")
    if not exe:
        row["detail"] = "agy CLI not on PATH"
        return row
    try:
        done = runner([exe, "--model", AGY_MODEL, "--output-format", "json", "--print=ok"],
                      stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        row.update(state="error", error=str(exc))
        return row
    if getattr(done, "returncode", 1) == 0 and (done.stdout or "").strip():
        row.update(state="ok", detail="live (probe)")
    else:
        row.update(state="error", detail="probe returned nothing",
                   error=((done.stderr or done.stdout or "").strip()[:160] or None))
    return row


def probe_free(runner=subprocess.run, exe=_UNSET, timeout: float = 90) -> list[dict]:
    """One ``opencode run`` smoke call per free id. Slow, so only ever run on request."""
    if exe is _UNSET:
        exe = shutil.which("opencode")
    if not exe:
        return [_row(f"free:{mid}", f"Free {mid.split('/')[-1]}", detail="opencode CLI not on PATH")
                for mid in FREE_IDS]
    rows: list[dict] = []
    for model in FREE_IDS:
        row = _row(f"free:{model}", f"Free {model.split('/')[-1]}")
        try:
            done = runner([exe, "run", "-m", model, "Reply with exactly: ok"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            row.update(state="error", error=str(exc))
            rows.append(row)
            continue
        output = ((done.stdout or "") + (done.stderr or "")).strip()
        if getattr(done, "returncode", 1) == 0 and "ok" in output.lower():
            row.update(state="ok", detail="live (probe)")
        elif "limit" in output.lower() or "429" in output:
            row.update(state="limited", detail="limit reached")
        else:
            row.update(state="error", detail="probe failed",
                       error=output[:160] or "no output")
        rows.append(row)
    return rows


# ---------------------------------------------------------------- collect + render
def collect(keys: dict[str, str], *, runner=subprocess.run, http=_http, with_free: bool = False,
            cli: dict[str, str | None] | None = None) -> list[dict]:
    """Every route's row, in display order. Missing CLI/key is «not configured», not an error."""
    paths_found = cli if cli is not None else {
        name: shutil.which(name) for name in ("orca", "opencode", "agy")
    }
    rows = orca_limits(runner=runner, exe=paths_found.get("orca"))
    rows.append(probe_opencode_go(keys, http=http))
    rows.append(probe_openrouter(keys, http=http))
    rows.append(probe_agy(runner=runner, exe=paths_found.get("agy")))
    if with_free:
        rows.extend(probe_free(runner=runner, exe=paths_found.get("opencode")))
    else:
        rows.append(_row("free", "Free OpenCode ids", state="skipped",
                         detail="not probed (add --probe-free)"))
    return rows


def render(rows: list[dict]) -> str:
    width = max((len(row["label"]) for row in rows), default=0)
    lines = ["Model usage - read-only, no key shown", ""]
    for row in rows:
        label = row["label"].ljust(width)
        if row["state"] == "not-configured":
            note = f"not configured - {row['detail']}" if row["detail"] else "not configured"
        elif row["state"] == "skipped":
            note = row["detail"] or "skipped"
        elif row["state"] == "unavailable":
            note = f"unavailable - {row['error'] or row['detail'] or 'no meter'}"
        elif row["state"] == "limited":
            note = row["detail"] or "limited"
        elif row["state"] == "error":
            note = f"error - {row['error'] or row['detail']}"
        else:
            note = _format_windows(row)
        lines.append(f"  {label}  {note}")
    return "\n".join(lines)


def _format_windows(row: dict) -> str:
    windows = row.get("windows") or {}
    if windows:
        parts = []
        for key in ("5h", "week", "fable", "month"):
            window = windows.get(key)
            if window:
                reset = f" resets {window['reset']}" if window.get("reset") else ""
                parts.append(f"{key} {window['usedPercent']}%{reset}")
        if parts:
            return " | ".join(parts)
    return row.get("detail") or "live"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One read-only screen of every route's live quota.")
    parser.add_argument("--json", action="store_true", help="machine-readable rows instead of the screen")
    parser.add_argument("--probe-free", action="store_true",
                        help="also smoke-probe the free OpenCode ids (slow: one run each)")
    parser.add_argument("--keys-file", default="", help="override the keys file path")
    return parser


def main(argv: list[str] | None = None) -> int:
    paths.console_safe()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    keys = load_keys(Path(args.keys_file) if args.keys_file else None)
    rows = collect(keys, with_free=args.probe_free)
    if args.json:
        print(json.dumps({"routes": rows}, indent=2, ensure_ascii=False))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

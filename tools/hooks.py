#!/usr/bin/env python3
"""hooks.py — two small Claude Code hooks for the Producer. Both fail OPEN.

    python tools/hooks.py owner-message   # UserPromptSubmit: remember what the owner submitted
    python tools/hooks.py stop-guard      # Stop: do not end an autonomous Producer's turn on an unhandled Delivery
    python tools/hooks.py status          # installed? last result of each hook?

Claude Code feeds each hook a JSON object on stdin. A hook that crashes, times out or cannot read
its input must never block the owner or trap an agent, so every path ends in exit code 0; a
failure is written to `.runtime/hooks-status.json` (which `status` and `producer.py doctor` read),
never raised.

owner-message
    Appends the submitted prompt to `work/agents/state/OWNER_LAST_MESSAGES.md` (newest last, the
    last 30 kept). Prompts that are obviously automation - Orca's injected briefs, Guardian
    nudges, the bootstrap line - are skipped (`AUTOMATION_PREFIXES`, plus `[hooks]
    ignore_prompt_prefixes` in producer.toml). What it keeps is "submitted in a Claude session of
    this project", NOT proof the owner typed it: a message from a Worker tab is never a ruling
    (AGENTS.md); only the Producer chat or a submitted blitz answer counts.

stop-guard
    Blocks a stop only when ALL of these hold: autonomy is ON (`.runtime/autonomy.json`), this
    session is the Producer pane the Guardian bound (`ORCA_TERMINAL_HANDLE` equals the bound
    handle), and the Run mailbox has actionable messages after the cursor. It then asks the
    Producer to handle them first. It never invents work: an empty queue, no autonomy, an unknown
    pane or an unreadable file all let the turn end. After `MAX_BLOCKS` blocks for the same
    mailbox state it lets go and records the incident, so it cannot loop.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import paths
import state_io

OWNER_PAGE = paths.AGENT_STATE / "OWNER_LAST_MESSAGES.md"
STATUS_FILE = paths.RUNTIME / "hooks-status.json"
GUARD_FILE = paths.RUNTIME / "stop-guard.json"
KEEP = 30
MAX_CHARS = 2000
MAX_BLOCKS = 3
HEADER = (
    "# Owner's last messages\n\n"
    "Appended by the `UserPromptSubmit` hook (`tools/hooks.py owner-message`), newest last, the last "
    f"{KEEP} kept. A line here was **submitted in a Claude session of this project**; it can still be "
    "an automated nudge the filters did not recognise or text from a Worker tab - a Worker-tab message "
    "is never a ruling. When a line matters, copy it into `HANDOVER.md` «Standing» or `OPEN.md` with "
    "its date.\n")
# Prompts that automation, not the owner, submits. Matched against the start of the prompt.
AUTOMATION_PREFIXES = (
    "You are working inside Orca",
    "Please carry out this task from my Orca coordinator",
    "You are the Producer",
    "ROTATE NOW",
    "[Guardian]",
    "Read work/agents/orca/START_PROMPT.md",
    "<system-reminder>",
    "<task-notification>",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(hook: str, ok: bool, detail: str = "") -> None:
    """Remember the last result of a hook; never raises."""
    try:
        state = state_io.load_json(STATUS_FILE, {})
    except state_io.StateCorrupt:
        state = {}
    try:
        entry = state.setdefault(hook, {})
        entry["lastRun"] = now()
        if ok:
            entry["lastOk"] = entry["lastRun"]
        else:
            entry["lastError"] = {"at": entry["lastRun"], "detail": detail[:300]}
        state_io.save_json(STATUS_FILE, state)
    except Exception:  # noqa: BLE001 - recording a failure must not itself fail the hook
        pass


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        value = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def extra_prefixes() -> tuple[str, ...]:
    try:
        value = paths._section("hooks").get("ignore_prompt_prefixes")
    except Exception:  # noqa: BLE001
        return ()
    return tuple(str(v) for v in value) if isinstance(value, (list, tuple)) else ()


def is_automation(prompt: str) -> bool:
    head = prompt.lstrip()
    return any(head.startswith(prefix) for prefix in AUTOMATION_PREFIXES + extra_prefixes())


def split_entries(text: str) -> list[str]:
    """The `## ...` entries of the page, each as its own block of text."""
    parts = text.split("\n## ")
    entries = [("## " + part if i else part) for i, part in enumerate(parts) if i or part.startswith("## ")]
    return [e.strip("\n") for e in entries if e.strip()]


def append_owner_message(prompt: str, session: str = "", page: Path | None = None) -> bool:
    """Add one entry, keep the newest `KEEP`; returns whether it was written."""
    page = page or OWNER_PAGE
    body = prompt.strip()
    if not body or is_automation(body):
        return False
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS].rstrip() + " …(cut)"
    tag = f" · session {session[:8]}" if session else ""
    entry = f"## {now()}{tag}\n\n{body}"
    with state_io.locked(page):
        try:
            existing = page.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        entries = split_entries(existing)
        entries.append(entry)
        text = HEADER + "\n" + "\n\n".join(entries[-KEEP:]) + "\n"
        page.parent.mkdir(parents=True, exist_ok=True)
        temporary = page.with_name(page.name + ".tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, page)
    return True


def command_owner_message() -> int:
    payload = read_payload()
    try:
        written = append_owner_message(str(payload.get("prompt") or ""), str(payload.get("session_id") or ""))
        record("owner-message", True, "written" if written else "skipped")
    except Exception as exc:  # noqa: BLE001 - fail open
        record("owner-message", False, f"{type(exc).__name__}: {exc}")
    return 0


# ------------------------------------------------------------------ stop guard
def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def producer_handle() -> str:
    """The Producer pane the Guardian bound, else the handle a checkpoint recorded."""
    binding = _json(paths.RUNTIME / "guardian-state.json").get("binding")
    if isinstance(binding, dict) and binding.get("handle"):
        return str(binding["handle"])
    return str(_json(paths.SUPERVISOR).get("activeHandle") or "")


def unhandled_rows() -> list[tuple[int, dict]] | None:
    """Actionable mailbox rows after the cursor; None when it cannot be judged (no cursor yet)."""
    import mailbox_cursor
    mark, rows = mailbox_cursor.new_rows(mailbox_cursor.LOG, mailbox_cursor.CURSOR)
    return None if mark is None else rows


def guard_decision(payload: dict, environ: dict | None = None) -> dict | None:
    """The `{"decision": "block", ...}` to print, or None to let the turn end."""
    env = os.environ if environ is None else environ
    if not _json(paths.RUNTIME / "autonomy.json").get("enabled"):
        return None
    mine, bound = str(env.get("ORCA_TERMINAL_HANDLE") or ""), producer_handle()
    if not mine or not bound or mine != bound:
        return None  # a Worker, the owner's own tab, or an unknown pane: never held back
    rows = unhandled_rows()
    if not rows:
        GUARD_FILE.unlink(missing_ok=True)
        return None
    fingerprint = f"{rows[0][0]}-{rows[-1][0]}-{len(rows)}"
    state = _json(GUARD_FILE)
    blocks = int(state.get("blocks", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
    state_io.save_json(GUARD_FILE, {"fingerprint": fingerprint, "blocks": blocks, "at": now()})
    if blocks > MAX_BLOCKS:
        record("stop-guard", False, f"released after {MAX_BLOCKS} blocks on the same mailbox state ({fingerprint})")
        return None
    kinds = ", ".join(sorted({str(row.get("type")) for _n, row in rows}))
    last = rows[-1][0]
    return {"decision": "block", "reason": (
        f"Autonomy is ON and the Run mailbox holds {len(rows)} unhandled message(s) ({kinds}). Handle each "
        "(inspect → route → verify → acknowledge → checkpoint; START_PROMPT.md §1-§7), then move the cursor: "
        f"`python tools/mailbox_cursor.py ack {last}`. If after handling there is nothing left to do, you may stop.")}


def command_stop_guard() -> int:
    payload = read_payload()
    try:
        decision = guard_decision(payload)
        record("stop-guard", True, "blocked" if decision else "allowed")
        if decision:
            print(json.dumps(decision, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - fail open: never trap the agent
        record("stop-guard", False, f"{type(exc).__name__}: {exc}")
    return 0


# ------------------------------------------------------------------ status
def installed_hooks(settings: Path | None = None) -> dict[str, bool]:
    """Which of the two hooks `.claude/settings.json` wires to this script."""
    settings = settings or paths.PROJECT / ".claude" / "settings.json"
    text = ""
    try:
        text = settings.read_text(encoding="utf-8")
    except OSError:
        pass
    return {name: f"hooks.py {name}" in text for name in ("owner-message", "stop-guard")}


def command_status() -> int:
    wired = installed_hooks()
    state = _json(STATUS_FILE)
    problems = 0
    for name in ("owner-message", "stop-guard"):
        info = state.get(name, {})
        error = info.get("lastError") or {}
        flag = "wired" if wired[name] else "NOT wired in .claude/settings.json"
        last = info.get("lastOk") or "never ran"
        problem = ""
        if error and error.get("at", "") >= info.get("lastOk", ""):
            problem = f" · LAST ERROR {error.get('at')}: {error.get('detail')}"
            problems += 1
        print(f"{name:<14} {flag} · last ok {last}{problem}")
    return 1 if problems or not all(wired.values()) else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths.console_safe()
    command = args[0] if args else ""
    if command == "owner-message":
        return command_owner_message()
    if command == "stop-guard":
        return command_stop_guard()
    if command == "status":
        return command_status()
    print(__doc__)
    return 0 if command in ("", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main())

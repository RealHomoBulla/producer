#!/usr/bin/env python3
"""Small Producer control CLI: status, bind a Run, a checkpoint, and a rotation alarm.

    python tools/producer.py status
    python tools/producer.py doctor                   # is this machine ready? ok/WARN/FAIL + the fix
    python tools/producer.py bind-run run_xxxx        # the ONE place a Run is bound
    python tools/producer.py checkpoint
    python tools/producer.py rotation-alarm --at HH:MM
    python tools/producer.py rotation-alarm --fire    # internal: what the scheduler runs

`status` prints one screen: is the Run bound, is the drainer alive, the mailbox cursor against the
log length, the digest unreported count, the unanswered count, whether a commit review is due, and
the git dirty count. It never raises and never claims a clean state it could not check: a git
failure prints "unverifiable", not "0 dirty".

`bind-run <run_id>` records the Run in `.runtime/producer-supervisor.json`. Every tool (status,
checkpoint, drainer, rotation alarm) resolves the Run through `paths.resolve_run()`, so they can
never look at different Runs; `status` warns when `producer.toml` or the checkpoint disagree.

`checkpoint` writes `.runtime/producer-checkpoint.json` from `orca orchestration run-show` /
`task-list` plus `git status`, so a successor Producer can reconcile.

`rotation-alarm --at HH:MM` arms a one-shot OS task (Windows schtasks / Linux systemd-run) that
types `ROTATE NOW` into the Run's coordinator terminal through `orca terminal send`. The handle is
resolved at FIRE time, never baked in, because every Orca restart reissues handles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orca_cli
import paths
import producer_mailbox

PROJECT = paths.PROJECT
RUNTIME = paths.RUNTIME
SUPERVISOR = paths.SUPERVISOR
CHECKPOINT = paths.CHECKPOINT
ROTATION_ALARM_TEXT = "ROTATE NOW"


def project_slug() -> str:
    """A short per-project id (`<name>-<hash of the repo path>`) for OS-wide names."""
    name = re.sub(r"[^A-Za-z0-9]+", "-", paths.project_name()).strip("-") or "project"
    digest = hashlib.sha1(str(PROJECT).encode("utf-8")).hexdigest()[:6]
    return f"{name[:24]}-{digest}"


def rotation_alarm_task() -> str:
    """The scheduler task name: one per project, so two projects never overwrite each other's alarm."""
    return f"ProducerRotate-{project_slug()}"


def _hidden() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _git_run(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=PROJECT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=_hidden())
    except OSError:
        return None


def _git_lines(*args: str) -> list[str] | None:
    """Output lines of a git command, or None when git failed (not a repository, git missing)."""
    done = _git_run(*args)
    if done is None or done.returncode != 0:
        return None
    return [line for line in done.stdout.splitlines() if line.strip()]


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _orca(*args: str, timeout: float = 60) -> dict | None:
    """One best-effort Orca JSON call; None on any failure (the dashboard must not raise)."""
    try:
        return orca_cli.run_json(*args, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def _result(payload: dict | None) -> dict:
    if not payload:
        return {}
    value = payload.get("result", {})
    return value if isinstance(value, dict) else {}


def bound_run() -> str | None:
    """The Run this project is on, from the one resolver (`paths.resolve_run`)."""
    return paths.resolve_run() or None


def drainer_alive() -> bool:
    """Whether a drainer holds its single-instance lock."""
    lock = Path(producer_mailbox.mailbox_file(producer_mailbox.LOCK_NAME))
    if not lock.exists():
        return False
    handle = open(lock, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fd = handle.fileno()
            here = os.lseek(fd, 0, os.SEEK_CUR)
            os.lseek(fd, 0x7FFFFFF0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return False
            except OSError:
                return True
            finally:
                os.lseek(fd, here, os.SEEK_SET)
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
    finally:
        handle.close()


def _mailbox_line() -> str:
    import mailbox_cursor
    log = mailbox_cursor.LOG
    cursor = mailbox_cursor.CURSOR
    lines = mailbox_cursor.complete_lines(log)
    mark = mailbox_cursor.read_cursor(cursor)
    if mark is None:
        return f"mailbox: {len(lines)} lines · cursor NONE (run `mailbox_cursor.py` to start)"
    new = sum(1 for n, t in enumerate(lines, 1) if n > mark[0] and t.strip())
    return f"mailbox: {len(lines)} lines · cursor {mark[0]} · {new} new"


def _digest_line() -> str:
    try:
        import digest
        rows = digest.products(3)
        state = digest.load()
        unreported = [r for r in rows
                      if state["reported"].get(r["path"], {}).get("mtime") != r["mtime"]]
        return f"digest: {len(unreported)} unreported product(s)"
    except Exception as exc:  # noqa: BLE001
        return f"digest: unavailable ({exc.__class__.__name__})"


def _unanswered_line() -> str:
    try:
        import unanswered
        rows = [r for r in unanswered.load()["rows"] if r["status"] == "open"]
        worst = min((r["priority"] for r in rows), default="—")
        return f"unanswered: {len(rows)} open · worst {worst}"
    except Exception as exc:  # noqa: BLE001
        return f"unanswered: unavailable ({exc.__class__.__name__})"


def _commit_review_line() -> str:
    try:
        import commit_review
        state = commit_review.load_state()
        waiting = commit_review.unreviewed(state)
        pending = [b for b in state["batches"] if b["status"] == "pending"]
        mark = "DUE" if len(waiting) >= commit_review.BATCH else "ok"
        return f"commit review: {len(waiting)}/{commit_review.BATCH} · {mark} · {len(pending)} pending"
    except Exception as exc:  # noqa: BLE001
        return f"commit review: unavailable ({exc.__class__.__name__})"


def _hooks_line() -> str:
    try:
        import hooks
        wired = hooks.installed_hooks()
        status = hooks._json(hooks.STATUS_FILE)
        bad = [n for n in wired if not wired[n] or (status.get(n, {}).get("lastError") or {}).get("at", "")
               > status.get(n, {}).get("lastOk", "")]
        return "hooks        : " + ("ok (owner-message, stop-guard)" if not bad else "CHECK " + ", ".join(bad)
                                    + " (python tools/hooks.py status)")
    except Exception as exc:  # noqa: BLE001
        return f"hooks        : unavailable ({exc.__class__.__name__})"


def _git_line() -> str:
    dirty = _git_lines("status", "--porcelain")
    if dirty is None:
        return "git          : unverifiable (not a git repository or git is missing)"
    head = _git_run("rev-parse", "--verify", "HEAD")
    unborn = " · no commits yet" if head is None or head.returncode != 0 else ""
    return f"git          : {len(dirty)} dirty path(s){unborn}"


def command_status(_args: argparse.Namespace) -> int:
    run = bound_run()
    print(f"project      : {paths.project_name()} ({PROJECT})")
    print(f"Run          : {run or 'not bound (python tools/producer.py bind-run <run_id>)'}")
    conflicts = paths.run_conflicts()
    if conflicts:
        print("WARNING      : recorded Runs disagree: " + " · ".join(conflicts)
              + " - rebind with `producer.py bind-run`")
    print(f"drainer      : {'alive' if drainer_alive() else 'NOT running'}")
    print(_mailbox_line())
    print(_digest_line())
    print(_unanswered_line())
    print(_commit_review_line())
    print(_hooks_line())
    print(_git_line())
    return 0


def doctor_checks() -> list[tuple[str, str, str]]:
    """(level, what, fix) per check; level is ok / WARN / FAIL. Read-only: nothing is started or written."""
    rows: list[tuple[str, str, str]] = []

    def add(level: str, what: str, fix: str = "") -> None:
        rows.append((level, what, fix))

    if sys.version_info >= (3, 11):
        add("ok", f"Python {sys.version.split()[0]}")
    else:
        add("FAIL", f"Python {sys.version.split()[0]} is too old", "install Python 3.11+ (3.12 recommended)")
    config = PROJECT / "producer.toml"
    try:
        import tomllib
        tomllib.loads(config.read_text(encoding="utf-8"))
        add("ok", "producer.toml parses")
    except (OSError, ValueError) as exc:
        add("FAIL", f"producer.toml is not readable TOML ({exc})", "fix the file or restore it from git")
    import shutil
    for name, level, fix in (("git", "FAIL", "install Git: https://git-scm.com/downloads"),
                             ("claude", "FAIL", "install Claude Code: npm i -g @anthropic-ai/claude-code, then run `claude` once and /login")):
        found = shutil.which(name)
        add("ok" if found else level, f"{name} on PATH" + (f" ({found})" if found else ""), "" if found else fix)
    try:
        cli = orca_cli.resolve_cli()
        done = subprocess.run([cli, "orchestration", "--help"], capture_output=True, text=True,
                              timeout=30, encoding="utf-8", errors="replace", creationflags=_hidden())
        if done.returncode == 0:
            add("ok", f"Orca CLI {cli} answers")
        else:
            add("WARN", f"Orca CLI {cli} found but `orchestration --help` failed",
                "open the Orca desktop app and retry; update Orca if the command is unknown")
    except orca_cli.OrcaCliError as exc:
        add("FAIL", f"Orca CLI: {exc}", "install the Orca desktop app and open it once; without Orca you can still use "
            "the Producer role in a single chat, but there are no parallel tabs and no Guardian")
    keys = paths.keys_file()
    add("ok" if keys.exists() else "WARN", f"keys file {keys}" + ("" if keys.exists() else " is missing"),
        "" if keys.exists() else "run `python tools/setup.py` (creates it with variable NAMES only)")
    mailbox = producer_mailbox.mailbox_dir()
    parent = mailbox
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if os.access(parent, os.W_OK):
        add("ok", f"mailbox directory {mailbox} can be created/written")
    else:
        add("FAIL", f"mailbox directory {mailbox} is not writable", "check permissions on the project folder")
    tz, warning = paths.owner_tzinfo()
    add("WARN" if warning else "ok", warning or f"timezone {paths.timezone_name()}",
        "set [project] timezone to an IANA name (and `pip install tzdata` on Windows) or a UTC offset" if warning else "")
    hook = PROJECT / ".git" / "hooks" / "commit-msg"
    if not (PROJECT / ".git").exists():
        add("WARN", "not a git repository", "run `git init` (setup.py does it) so commits and reviews work")
    elif hook.exists() and "knowledge_gate" in hook.read_text(encoding="utf-8", errors="replace"):
        add("ok", "commit-msg hook installed")
    else:
        add("WARN", "commit-msg hook is not installed", "run `python tools/knowledge_gate.py install`")
    try:
        import hooks
        wired = hooks.installed_hooks()
        status = hooks._json(hooks.STATUS_FILE)
        missing = [name for name, ok in wired.items() if not ok]
        failing = [name for name in wired if (status.get(name, {}).get("lastError") or {}).get("at", "")
                   > status.get(name, {}).get("lastOk", "")]
        if missing:
            add("WARN", "Claude hooks not wired: " + ", ".join(missing),
                "restore the `hooks` block of .claude/settings.json (owner messages / stop guard are optional)")
        elif failing:
            add("WARN", "a Claude hook reported an error: " + ", ".join(failing), "python tools/hooks.py status")
        else:
            add("ok", "Claude hooks wired (owner-message, stop-guard)")
    except Exception:  # noqa: BLE001 - optional check
        pass
    language = paths.owner_language()
    other = "en" if language == "ru" else "ru"
    stray = [p for kind in ("digest", "unanswered") if not paths.owner_page(kind, language).exists()
             and paths.owner_page(kind, other).exists() for p in [paths.owner_page(kind, other)]]
    if stray:
        add("WARN", f"owner pages are in the other language ({stray[0].name})",
            f"run `python tools/setup.py --lang {language}` to localise the untouched seed pages")
    try:
        import structure_check
        missing, stale = structure_check.check()
        problem = bool(missing or stale)
        add("WARN" if problem else "ok",
            "STRUCTURE.md disagrees with the file tree" if problem else "STRUCTURE.md matches the file tree",
            "python tools/structure_check.py" if problem else "")
    except Exception:  # noqa: BLE001 - the doctor must not crash on an optional check
        pass
    return rows


def command_doctor(_args: argparse.Namespace) -> int:
    rows = doctor_checks()
    for level, what, fix in rows:
        print(f"[{level:>4}] {what}" + (f"\n         -> {fix}" if fix else ""))
    failed = sum(1 for level, _w, _f in rows if level == "FAIL")
    warned = sum(1 for level, _w, _f in rows if level == "WARN")
    print(f"doctor: {failed} FAIL, {warned} WARN" + (" - NOT READY" if failed else " - ready"))
    return 1 if failed else 0


def command_bind_run(args: argparse.Namespace) -> int:
    """Record the Run in the supervisor file, and say what else still disagrees."""
    run = args.run_id
    if not run.startswith("run_"):
        print(f"a Run id looks like run_xxxx, got {run!r}", file=sys.stderr)
        return 2
    state = _read_json(SUPERVISOR)
    state["activeRunId"] = run
    state["boundAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    SUPERVISOR.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"bound Run {run} (recorded in {SUPERVISOR.relative_to(PROJECT).as_posix()})")
    stale = paths.run_conflicts()
    if stale:
        print("note: still recorded elsewhere: " + " · ".join(stale)
              + " - `bind-run` wins; update producer.toml [orca] run or run `checkpoint` to align")
    return 0


def _slim_tasks(payload: dict) -> dict:
    rows = [row for row in (payload.get("tasks") or []) if isinstance(row, dict)]
    counts: dict[str, int] = {}
    open_rows: list[dict] = []
    settled = {"completed", "failed", "cancelled"}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status in settled:
            continue
        open_rows.append({k: row.get(k) for k in ("id", "title", "status", "dispatch_id")
                          if row.get(k) is not None})
    return {"runId": payload.get("runId"), "taskTotal": len(rows),
            "taskCounts": dict(sorted(counts.items())), "openTasks": open_rows}


def command_checkpoint(_args: argparse.Namespace) -> int:
    run = bound_run()
    checkpoint: dict = {
        "version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reportedBy": "producer",
        "activeRun": None,
        "tasks": {},
        "gitStatus": _git_lines("status", "--short"),
    }
    if run:
        run_detail = _result(_orca("orchestration", "run-show", "--id", run, "--json"))
        checkpoint["activeRun"] = run_detail.get("run") or {"id": run}
        tasks = _result(_orca("orchestration", "task-list", "--run", run, "--json"))
        checkpoint["tasks"] = _slim_tasks(tasks)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Keep the supervisor's activeRunId pointed at the same Run, so a successor finds it.
    if run:
        state = _read_json(SUPERVISOR)
        state["activeRunId"] = run
        state["checkpointAt"] = checkpoint["createdAt"]
        SUPERVISOR.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"checkpoint saved: {CHECKPOINT} · Run {run or 'none'}")
    return 0


def _rotation_target() -> tuple[str | None, str]:
    """The live Producer pane at FIRE time: supervisor activeHandle, else the Run's coordinator."""
    state = _read_json(SUPERVISOR)
    candidates: list[str] = []
    active = str(state.get("activeHandle") or "")
    if active:
        candidates.append(active)
    run = bound_run()
    if run:
        coordinator = str((_result(_orca("orchestration", "run-show", "--id", run, "--json"))
                           .get("run") or {}).get("coordinator_handle") or "")
        if coordinator and coordinator not in candidates:
            candidates.append(coordinator)
    if not candidates:
        return None, "no recorded Producer handle and no bound Run"
    # Resolve against the live terminal list so a dead handle does not receive the text.
    payload = _orca("terminal", "list", "--limit", "200")
    if payload is None:
        return None, "terminal list unreadable; not typing into an unverified handle"
    terminals = _result(payload)
    rows = terminals.get("terminals") or terminals.get("rows") or []
    live = {str(r.get("handle")) for r in rows if isinstance(r, dict) and r.get("connected")}
    for handle in candidates:
        if handle in live:
            return handle, "ok"
    return None, "recorded handles are not connected terminals"


def command_rotation_alarm(args: argparse.Namespace) -> int:
    if args.fire:
        handle, detail = _rotation_target()
        if not handle:
            print(f"rotation alarm: {detail}", file=sys.stderr)
            return 1
        payload = _orca("terminal", "send", "--terminal", handle, "--text", ROTATION_ALARM_TEXT,
                        "--enter", timeout=90)
        if not payload:
            print("rotation alarm: terminal send failed", file=sys.stderr)
            return 1
        print(f"rotation alarm: {ROTATION_ALARM_TEXT} → {handle}")
        return 0

    try:
        hour, minute = (int(part) for part in str(args.at).split(":", 1))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        print(f"--at wants local HH:MM, got {args.at!r}", file=sys.stderr)
        return 2
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        action = subprocess.list2cmdline([sys.executable, script, "rotation-alarm", "--fire"])
        command = ["schtasks", "/Create", "/SC", "ONCE", "/ST", f"{hour:02d}:{minute:02d}",
                   "/TN", rotation_alarm_task(), "/F", "/TR", action]
    else:
        now_local = datetime.now()
        target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)
        seconds = int((target - now_local).total_seconds())
        command = ["systemd-run", "--user", f"--on-active={seconds}s",
                   f"--unit=producer-rotate-{project_slug()}-{int(time.time())}",
                   sys.executable, script, "rotation-alarm", "--fire"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False,
                               creationflags=_hidden())
    if completed.returncode != 0:
        print(f"rotation alarm not armed: {(completed.stderr or completed.stdout).strip()[:300]}",
              file=sys.stderr)
        return 1
    print(f"rotation alarm armed for {hour:02d}:{minute:02d} local; it resolves the Producer handle "
          "when it fires")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="one screen of Producer state").set_defaults(func=command_status)
    sub.add_parser("doctor", help="read-only readiness check with the fix for each problem").set_defaults(
        func=command_doctor)
    bind = sub.add_parser("bind-run", help="bind this project to an Orca Run")
    bind.add_argument("run_id")
    bind.set_defaults(func=command_bind_run)
    sub.add_parser("checkpoint", help="write .runtime/producer-checkpoint.json").set_defaults(func=command_checkpoint)

    alarm = sub.add_parser("rotation-alarm", help="arm/fire the ROTATE NOW alarm")
    alarm.add_argument("--at", help="local HH:MM to arm for")
    alarm.add_argument("--fire", action="store_true", help="internal: type ROTATE NOW and exit")
    alarm.set_defaults(func=command_rotation_alarm)

    args = parser.parse_args(argv)
    paths.console_safe()
    if args.command == "rotation-alarm" and not args.at and not args.fire:
        print("rotation-alarm needs --at HH:MM or --fire", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

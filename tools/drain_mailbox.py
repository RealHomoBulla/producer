"""Keep the Run mailbox from piling up unread.

An unacked Delivery makes Orca TYPE «You have N orchestration messages» into the Producer
terminal = the owner's chat composer, and auto-send it mid-sentence. So this drainer claims and
acks EVERY Delivery. Nothing is lost by that:
  * every actionable message -- worker_done, question, escalation, any unknown type, and a
    `status` whose subject carries 🔴 -- is appended to the actionable log BEFORE the ack (a crash
    re-claims, never drops; a re-claimed message id is not written twice). The row carries the
    WHOLE message (`message`: task/dispatch ids, outcome, report path, files, full body) so the
    Producer can route it; heartbeats and plain status nudges are skipped;
  * `orca orchestration inbox` reads messages regardless of ack state.

Because of the ack, `check --peek` shows NOTHING actionable: the Producer reads the jsonl, BY
CURSOR. Every row this drainer writes carries `line` = its physical 1-based line number in the
file (monotonic; append-only, never rotated) and `at` = ISO date-time UTC.

The log, error log and lock live in THIS project's `.runtime/mailbox/` (producer_mailbox.py), so
two projects on one machine each run their own drainer.

Run: `setsid nohup python3 tools/drain_mailbox.py [seconds] [--run RUN] &`
Windows: `powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath pythonw
  -ArgumentList 'tools/drain_mailbox.py','604800'"` -- pythonw = no window.

The run id comes from `--run`, else the Run bound by `producer.py bind-run` (one resolver,
`paths.resolve_run()`), re-read every pass. There is deliberately NO hard-coded fallback: an unbound run
is logged and retried, never guessed. Errors go to the error log.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import paths
import producer_mailbox
import orca_cli

PROJECT = paths.PROJECT
LOG = str(producer_mailbox.mailbox_file(producer_mailbox.ACTIONABLE_NAME))
ERR = str(producer_mailbox.mailbox_file(producer_mailbox.ERR_NAME))
LOCK = str(producer_mailbox.mailbox_file(producer_mailbox.LOCK_NAME))

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
    # Windows byte locks are MANDATORY: a lock on real data would make a reader fail. The lock sits
    # on one byte far past any real end of file, which Windows allows and no reader touches.
    _LOCK_OFFSET = 0x7FFFFFF0
    _TEXT_KW = {"encoding": "utf-8", "errors": "replace", "creationflags": subprocess.CREATE_NO_WINDOW}
    _BYTES_KW = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    import fcntl
    _TEXT_KW, _BYTES_KW = {}, {}


def _lock(handle, blocking=True):
    """Exclusive lock on `handle`: flock on POSIX, msvcrt on Windows."""
    if not WINDOWS:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    fd = handle.fileno()
    here = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
    try:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise
                time.sleep(0.05)
    finally:
        os.lseek(fd, here, os.SEEK_SET)


def _unlock(handle):
    if not WINDOWS:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    fd = handle.fileno()
    here = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, here, os.SEEK_SET)


def _orca():
    """The resolved Orca IDE CLI (never a bare `orca`, which may be the GNOME screen reader)."""
    return orca_cli.resolve_cli()


def is_actionable(message):
    """Heartbeats never; `status` only with 🔴 in the subject; every other type always."""
    kind = message.get("type")
    if kind == "heartbeat":
        return False
    return kind != "status" or "🔴" in (message.get("subject") or "")


def _known_ids(data):
    """Message ids already in the log text (a row that does not parse is ignored)."""
    ids = set()
    for text in data.split("\n"):
        if '"id"' not in text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id"):
            ids.add(row["id"])
    return ids


def append_actionable(path, delivery, messages, *, now=None, run=None):
    """Append rows with a monotonic physical `line` number; returns the rows written.

    A message id already in the log is skipped (a crash between append and ack re-claims the same
    Delivery). Each row keeps the whole message under `message`, and the append is fsynced before
    the caller acks.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = []
    with open(path, "a+", encoding="utf-8") as handle:
        _lock(handle)
        try:
            handle.seek(0)
            data = handle.read()
            line = data.count("\n")
            seen = _known_ids(data)
            if data and not data.endswith("\n"):
                handle.write("\n")  # never glue a row onto a torn last line
                line += 1
            for message in messages:
                if message.get("id") in seen:
                    continue
                line += 1
                row = {"line": line, "at": stamp, "run": run, "delivery": delivery,
                       "id": message.get("id"), "type": message.get("type"),
                       "subject": message.get("subject") or "", "body": message.get("body") or "",
                       "message": message}
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows.append(row)
                seen.add(message.get("id"))
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            _unlock(handle)
    return rows


def current_run(explicit=None):
    """The Run to drain: `--run`, else the Run this project is bound to (`paths.resolve_run`).

    Never a hard-coded fallback. Returns "" when unbound, which the caller skips.
    """
    return paths.resolve_run(explicit)


def log(path, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(datetime.now(timezone.utc).isoformat(timespec="seconds") + " " + text + "\n")


def coordinator_handle(run, runner=subprocess.run):
    """The Run's CURRENT coordinator terminal handle (`run-show`), or None. Read only on a fence."""
    try:
        out = runner([_orca(), "orchestration", "run-show", "--id", run, "--json"],
                     capture_output=True, text=True, timeout=60, **_TEXT_KW).stdout
        handle = ((json.loads(out).get("result") or {}).get("run") or {}).get("coordinator_handle")
    except Exception:  # noqa: BLE001
        return None
    return handle if isinstance(handle, str) and handle.startswith("term_") else None


def check_cmd(run, terminal, *extra):
    """`orca orchestration check` for this Run; `--terminal` only once a fence made us name the caller."""
    cmd = [_orca(), "orchestration", "check", "--run", run]
    if terminal:
        cmd += ["--terminal", terminal]
    return cmd + list(extra)


def on_fenced(state, run, runner=subprocess.run, log_fn=log):
    """`consumer_fenced` = our terminal is no longer the Run's coordinator. Re-bind, never exit."""
    handle = coordinator_handle(run, runner)
    if handle and handle != state.get("terminal"):
        log_fn(ERR, f'fenced on {run}: rebound {state.get("terminal") or "env"} -> {handle}')
        state["terminal"] = handle
        return 2
    log_fn(ERR, f'fenced on {run}: no newer coordinator ({handle}); retry in 30 s')
    return 30


def single_instance(path=None):
    """Take this project's one-drainer lock; returns the open handle (keep it referenced) or None."""
    handle = open(path or LOCK, "a+")
    try:
        _lock(handle, blocking=False)
    except OSError:
        handle.close()
        return None
    return handle


def drain_pass(run, state, runner=subprocess.run, log_fn=log):
    """One claim -> log -> ack pass. Returns seconds to sleep."""
    out = runner(check_cmd(run, state.get("terminal"), "--wait", "--timeout-ms", "60000", "--json"),
                 capture_output=True, text=True, timeout=90, **_TEXT_KW).stdout
    if '"waiter_exists"' in out:
        out = runner(check_cmd(run, state.get("terminal"), "--json"),
                     capture_output=True, text=True, timeout=60, **_TEXT_KW).stdout
    payload = json.loads(out)
    if not payload.get("ok", True) or "result" not in payload:
        code = (payload.get("error") or {}).get("code", "no result")
        log_fn(ERR, f"orca error on {run}: {code}")
        return on_fenced(state, run, runner, log_fn) if code in (
            "consumer_fenced", "stable_pane_required") else 2
    result = payload["result"]
    delivery = result.get("deliveryId")
    if delivery:
        act = [m for m in (result.get("messages") or []) if is_actionable(m)]
        if act:
            append_actionable(LOG, delivery, act, run=run)
        done = runner(check_cmd(run, state.get("terminal"), "--ack", delivery, "--peek", "--json"),
                      capture_output=True, text=True, timeout=60, **_TEXT_KW)
        if getattr(done, "returncode", 0):
            # Not acked: Orca will replay the Delivery; the id dedupe keeps the log clean.
            log_fn(ERR, f"ack of {delivery} on {run} failed: "
                        f"{(getattr(done, 'stderr', '') or '')[:200]}")
            return 5
    return 2


def main(argv):
    parser = argparse.ArgumentParser(description=f"Drain the Run mailbox into {LOG}.")
    parser.add_argument("seconds", nargs="?", type=float, default=7 * 86400)
    parser.add_argument("--run", default=None, help="Run id; default: the Run bound by producer.py bind-run")
    args = parser.parse_args(argv)
    producer_mailbox.ensure_mailbox_dir()
    lock = single_instance()
    if lock is None:
        log(ERR, "another drainer holds the lock; this one exits")
        return
    explicit = args.run
    end = time.time() + args.seconds
    state = {}
    while time.time() < end:
        run = current_run(explicit)
        if not run:
            log(ERR, "no Run bound (run `python tools/producer.py bind-run <run_id>` or pass --run)")
            time.sleep(30)
            continue
        try:
            pause = drain_pass(run, state)
        except Exception as exc:  # noqa: BLE001 -- a drainer that dies lets Orca type into the chat
            log(ERR, type(exc).__name__ + ": " + str(exc)[:200])
            pause = 15
        time.sleep(pause)


if __name__ == "__main__":
    main(sys.argv[1:])

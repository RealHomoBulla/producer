#!/usr/bin/env python3
"""worker.py - launch a Worker tab WITH PROOF, close only settled ones, reap what is left.

The Producer's three Worker chores, each of which has cost hours when done by hand:

    python tools/worker.py launch --task <task_id> --route <roster name> [--title "..."] [--product <path>]
    python tools/worker.py close  --terminal <handle> [--product <path>]
    python tools/worker.py reap   [--apply]
    python tools/worker.py load                      # what the [limits] say right now

* **launch** checks the machine first (``producer.toml [limits]``: ``max_workers``, ``min_free_ram_gb``, ``max_load``) and
  refuses when it is over a limit. It then creates the tab with the roster seat's command, waits for the TUI, runs
  ``orca orchestration dispatch --inject`` and PROVES RECEIPT before it says OK: the brief is not stuck in the composer
  (``[Pasted Content ...]``) AND there is a sign of work (the screen is busy, the task id appears in a fresh Claude
  transcript, a watched product file changed, or the screen changed after the dispatch). A stuck paste gets ONE Enter; a
  paste that never arrived gets ONE short re-injection; after that the launch is reported FAILED with the reason and the
  tab is left open for inspection. A banner or ``dispatched`` is not proof.
* **close** closes a tab only when the Worker behind it is settled: its Orca task is completed / failed / cancelled, or
  ``--product`` is committed (for a Worker that has no dispatch capability and so cannot call worker_done). It never closes
  the Producer, the owner's Blitz tab, the Run's coordinator or the tab it is run from, and always closes the whole tab.
* **reap** lists this project's settled Worker tabs (dry by default); ``--apply`` closes them through the same checks.

Everything goes through the Guardian's one Orca seam, so ownership is the Guardian's: only tabs of THIS project's
worktree are ever read, typed into or closed. Stdlib only (Windows + Linux).

Exit codes: 0 done / 2 launch FAILED (tab left open) / 3 refused (machine busy, bad input, protected tab).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import guardian
import paths
from guardian import (
    Guardian, GuardianConfig, LoadVerdict, Seat, _first_handle, _has_pasted, _is_busy, _screen_digest, _tail,
    _unwrap, compose_command, load_config, read_machine,
)

SETTLED = frozenset({"completed", "failed", "cancelled", "canceled"})
READY_SECONDS = 60.0         # wait for the TUI prompt after the tab is created
RECEIPT_SECONDS = 90.0       # proof of receipt must arrive within this
POLL_SECONDS = 3.0
STUCK_SECONDS = 8.0          # a paste still in the composer this long gets its one Enter
TRANSCRIPT_FILES = 25        # newest Claude transcripts scanned for the task id
TRANSCRIPT_BYTES = 2_000_000
LEDGER = "worker-launches.jsonl"
REINJECT_TEXT = "Your Orca task is {task}. Read the brief your Task spec names COMPLETELY first and execute it exactly."
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 2, 3


class Refused(Exception):
    """The action is not allowed (machine busy, bad input, protected or unsettled tab). Nothing was changed."""


@dataclass
class Receipt:
    ok: bool
    signal: str = ""              # busy | transcript | product | screen
    reason: str = ""              # why not, when not ok
    enter_sent: bool = False
    reinjected: bool = False


@dataclass
class TaskView:
    id: str
    status: str
    assignee: str
    created_by: str
    title: str = ""


@dataclass
class Bench:
    """The Guardian's seam plus the few extras the Worker chores need. Everything is injectable for tests."""

    guardian: Guardian
    own_handle: str = ""
    transcript_dirs: list[Path] = field(default_factory=list)
    supervisor: Path = paths.SUPERVISOR
    wall: Callable[[], float] = time.time           # real time, for file mtimes
    receipt_seconds: float = RECEIPT_SECONDS
    ready_seconds: float = READY_SECONDS

    @property
    def config(self) -> GuardianConfig:
        return self.guardian.config

    def orca(self, *args: str, allow_error: bool = False, timeout: int = guardian.ORCA_TIMEOUT) -> dict:
        return self.guardian.orca(*args, allow_error=allow_error, timeout=timeout)


def make_bench(**over) -> Bench:
    config = load_config()
    machine = over.pop("machine", read_machine)
    g = Guardian(config, machine=machine)
    own = os.environ.get("ORCA_TERMINAL_HANDLE", "")
    dirs = [Path.home() / ".claude" / "projects"]
    return Bench(guardian=g, own_handle=own, transcript_dirs=dirs, **over)


# ------------------------------------------------------------------ Orca reads
def run_tasks(bench: Bench, run: str) -> list[TaskView]:
    payload = bench.orca("orchestration", "task-list", "--run", run)
    rows = _unwrap(payload).get("tasks")
    if not isinstance(rows, list):
        raise Refused("Orca task-list returned no tasks array - cannot tell which tabs are settled")
    return [
        TaskView(id=str(r.get("id") or ""), status=str(r.get("status") or "").casefold(),
                 assignee=str(r.get("assignee_handle") or ""), created_by=str(r.get("created_by_terminal_handle") or ""),
                 title=str(r.get("task_title") or ""))
        for r in rows if isinstance(r, dict)
    ]


def inventory(bench: Bench) -> list[dict]:
    rows = bench.guardian._inventory()
    if rows is None:
        raise Refused("terminal list is unverifiable (Orca unreachable?) - no tab is touched")
    return rows


def resolve_run(explicit: str | None) -> str:
    run = paths.resolve_run(explicit)
    if not run:
        raise Refused("no Orca Run bound: pass --run or set producer.toml [orca] run (or `producer.py bind-run`)")
    return run


# ------------------------------------------------------------------ ownership
def _supervisor_handle(bench: Bench) -> str:
    value = guardian._read_json(bench.supervisor).get("activeHandle")
    return value if isinstance(value, str) else ""


def protected_reason(bench: Bench, row: dict, tasks: list[TaskView] | None = None) -> str | None:
    """Why this tab must never be closed (or counted as a Worker), or None. Title alone is never enough to PROVE a Worker."""
    g, cfg = bench.guardian, bench.config
    handle = str(row.get("handle"))
    if bench.own_handle and handle == bench.own_handle:
        return "your own tab"
    if handle == g.producer_handle:
        return "the Producer (bound by the Guardian)"
    if handle and handle == _supervisor_handle(bench):
        return "the Producer (producer-supervisor.json activeHandle)"
    if any(t.created_by == handle for t in tasks or []):
        return "a coordinator tab (it created tasks in this Run)"
    title = str(row.get("title") or "").casefold().strip()
    names = {cfg.producer_title, *(r.title for r in cfg.routes), *cfg.remote_control.titles, "Blitz", "Блиц"}
    for name in names:
        name = name.casefold().strip()
        if name and (title == name or title.startswith((name + " ", name + "#", name + " #"))):
            return f"an owner/Producer tab by title ('{row.get('title')}')"
    if g._ignored(row):
        return "an owner tab (guardian.ignore_titles)"
    if handle in set(g.state["retired"]):
        return "a retired Producer seat (the Guardian closes those after HANDOVER proof)"
    return None


def worker_rows(bench: Bench, rows: list[dict], tasks: list[TaskView] | None = None) -> list[dict]:
    """Connected agent tabs of this project that are not protected: the tabs the limits count."""
    return [r for r in rows if r.get("connected") and bench.guardian._is_agent(r) and protected_reason(bench, r, tasks) is None]


def settled_reason(bench: Bench, row: dict, tasks: list[TaskView], product: str | None = None) -> tuple[bool, str]:
    """Is the Worker behind this tab done? (settled?, why). Unknown is never settled."""
    handle = str(row.get("handle"))
    mine = [t for t in tasks if t.assignee == handle]
    open_tasks = [t for t in mine if t.status not in SETTLED]
    product_note = ""
    if product:
        committed, why = product_committed(bench, product)
        product_note = f"; product {product} is not committed ({why})"
        if committed:
            text, _draft = bench.guardian._view(handle, fresh=True)
            if _is_busy(text):
                return False, f"product {product} is committed but the tab is still working"
            return True, f"product {product} is committed ({why})"
        if not mine:
            return False, f"no registered task and product not committed ({why})"
    if not mine:
        return False, "no Orca task is assigned to this tab - cannot prove it is a finished Worker"
    if open_tasks:
        t = open_tasks[0]
        return False, f"task {t.id} is {t.status or 'unknown'}{product_note}"
    return True, f"task {mine[-1].id} is {mine[-1].status}"


def product_committed(bench: Bench, product: str) -> tuple[bool, str]:
    g = bench.guardian
    code, out, _err = g.git_runner(g.project, ["log", "-1", "--format=%h", "--", product])
    sha = out.strip()
    if code != 0 or not sha:
        return False, "no commit touches it"
    code, out, _err = g.git_runner(g.project, ["status", "--porcelain", "--", product])
    if code != 0:
        return False, "git status failed"
    if out.strip():
        return False, "uncommitted changes in it"
    return True, sha


# ------------------------------------------------------------------ machine limits
def machine_verdict(bench: Bench, rows: list[dict], tasks: list[TaskView] | None = None) -> LoadVerdict:
    g = bench.guardian
    g._verdict = g._machine = None
    return g.load_verdict(len(worker_rows(bench, rows, tasks)))


# ------------------------------------------------------------------ receipt
def _stuck_in_composer(text: str, draft: str) -> bool:
    if _has_pasted(draft):
        return True
    return not draft.strip() and not _is_busy(text) and _has_pasted(_tail(text, 4))


def _transcript_hit(bench: Bench, task: str, since: float) -> bool:
    files: list[tuple[float, Path]] = []
    for base in bench.transcript_dirs:
        try:
            for path in base.glob("*/*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= since:
                    files.append((mtime, path))
        except OSError:
            continue
    for _mtime, path in sorted(files, reverse=True)[:TRANSCRIPT_FILES]:
        try:
            with path.open("rb") as stream:
                if task.encode() in stream.read(TRANSCRIPT_BYTES):
                    return True
        except OSError:
            continue
    return False


def _product_changed(bench: Bench, product: str | None, before: float | None) -> bool:
    if not product:
        return False
    try:
        mtime = (bench.guardian.project / product).stat().st_mtime
    except OSError:
        return False
    return before is None or mtime > before


def _product_mtime(bench: Bench, product: str | None) -> float | None:
    try:
        return (bench.guardian.project / product).stat().st_mtime if product else None
    except OSError:
        return None


def prove_receipt(bench: Bench, handle: str, task: str, *, baseline: str, since: float,
                  product: str | None = None, product_before: float | None = None) -> Receipt:
    """Wait for proof that the Worker RECEIVED and started its brief; remedy a stalled paste at most once each way."""
    g = bench.guardian
    receipt = Receipt(ok=False)
    waited = 0.0
    last_state = "no sign of work"
    while True:
        text, draft = g._view(handle, fresh=True)
        stuck = _stuck_in_composer(text, draft)
        signal = ""
        if not stuck:
            if _is_busy(text):
                signal = "busy"
            elif _transcript_hit(bench, task, since):
                signal = "transcript"
            elif _product_changed(bench, product, product_before):
                signal = "product"
            elif not draft.strip() and _screen_digest(text) != baseline:
                signal = "screen"
        if signal:
            receipt.ok, receipt.signal = True, signal
            return receipt
        last_state = ("the brief is still in the composer" + (" after Enter" if receipt.enter_sent else "")
                      if stuck else "no sign of work: screen unchanged, task id not in a fresh transcript, no product change")
        if stuck and not receipt.enter_sent and waited >= STUCK_SECONDS:
            bench.orca("terminal", "send", "--terminal", handle, "--enter", timeout=60)
            g._view_cache.pop(handle, None)
            receipt.enter_sent = True
        elif (not stuck and not receipt.reinjected and waited >= bench.receipt_seconds / 2.0):
            g._type(handle, REINJECT_TEXT.format(task=task))
            receipt.reinjected = True
        if waited >= bench.receipt_seconds:
            receipt.reason = last_state
            return receipt
        g.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def wait_ready(bench: Bench, handle: str) -> bool:
    """Wait for the Worker's TUI to be idle at a prompt. False = it never came up (the caller reports it)."""
    g = bench.guardian
    try:
        bench.orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                   "--timeout-ms", str(int(bench.ready_seconds * 1000)))
        return True
    except Exception:  # noqa: BLE001 - an unsupported/failed wait falls back to polling the screen
        pass
    waited = 0.0
    while waited < bench.ready_seconds:
        text, _draft = g._view(handle, fresh=True)
        if text.strip() and not _is_busy(text):
            return True
        g.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False


# ------------------------------------------------------------------ launch
def find_seat(config: GuardianConfig, name: str) -> Seat:
    seats = [s for s in config.roster if name.casefold() in (s.name.casefold(), s.title_prefix.casefold())]
    if not seats:
        known = ", ".join(s.name for s in config.roster) or "none - add a [[roster]] seat to producer.toml"
        raise Refused(f"unknown route {name!r}; roster seats: {known}")
    seat = seats[0]
    if not seat.command.strip():
        raise Refused(f"roster seat {seat.name!r} has no command")
    return seat


def _ledger(bench: Bench, row: dict) -> None:
    path = bench.guardian.runtime / LEDGER
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": guardian._iso(bench.guardian.clock()), **row}, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the ledger is an aid; the launch result is printed either way


def launch(bench: Bench, task: str, route: str, *, title: str | None = None, run: str | None = None,
           product: str | None = None) -> tuple[int, dict]:
    """Create, brief and PROVE one Worker. Returns (exit code, result row)."""
    g = bench.guardian
    seat = find_seat(bench.config, route)
    run_id = resolve_run(run)
    tasks = run_tasks(bench, run_id)
    mine = next((t for t in tasks if t.id == task), None)
    if mine is None:
        raise Refused(f"task {task} is not in Run {run_id}")
    if mine.status in SETTLED:
        raise Refused(f"task {task} is already {mine.status}")
    rows = inventory(bench)
    if mine.assignee and any(str(r.get("handle")) == mine.assignee and r.get("connected") for r in rows):
        raise Refused(f"task {task} already has a live Worker tab ({mine.assignee})")
    verdict = machine_verdict(bench, rows, tasks)
    if verdict.busy:
        raise Refused(verdict.text())
    tab_title = title or f"{seat.title_prefix} · {mine.title or task}"[:60]
    args = ["terminal", "create", "--worktree", f"path:{g.project_posix}", "--title", tab_title,
            "--command", compose_command(seat.command, seat.keys, bench.config.keys_file_raw)]
    if os.name == "nt" and seat.shell_windows:
        args += ["--shell", seat.shell_windows]
    payload = bench.orca(*args, allow_error=True)
    handle = _first_handle(_unwrap(payload)) or _first_handle(payload)
    result = {"task": task, "route": seat.name, "run": run_id, "handle": handle or "", "title": tab_title}
    if not handle:
        result.update(ok=False, reason="Orca returned no terminal handle for the new tab")
        _ledger(bench, result)
        return EXIT_FAILED, result
    if not wait_ready(bench, handle):
        result.update(ok=False, reason=f"the TUI did not reach a prompt within {int(bench.ready_seconds)} s")
        _ledger(bench, result)
        return EXIT_FAILED, result
    baseline = _screen_digest(g._view(handle, fresh=True)[0])
    since = bench.wall() - 1.0
    before = _product_mtime(bench, product)
    try:
        bench.orca("orchestration", "dispatch", "--run", run_id, "--task", task, "--to", handle, "--inject", timeout=90)
    except Exception as exc:  # noqa: BLE001 - reported with the tab handle, never swallowed
        result.update(ok=False, reason=f"dispatch --inject failed: {exc}"[:200])
        _ledger(bench, result)
        return EXIT_FAILED, result
    receipt = prove_receipt(bench, handle, task, baseline=baseline, since=since, product=product, product_before=before)
    result.update(ok=receipt.ok, signal=receipt.signal, reason=receipt.reason,
                  enterSent=receipt.enter_sent, reinjected=receipt.reinjected)
    _ledger(bench, result)
    return (EXIT_OK if receipt.ok else EXIT_FAILED), result


# ------------------------------------------------------------------ close / reap
def close(bench: Bench, handle: str, *, run: str | None = None, product: str | None = None) -> str:
    """Close ONE settled Worker tab. Returns why it was allowed; raises Refused otherwise."""
    run_id = resolve_run(run)
    tasks = run_tasks(bench, run_id)
    row = next((r for r in inventory(bench) if str(r.get("handle")) == handle), None)
    if row is None:
        raise Refused(f"{handle} is not a tab of this project's worktree (or is already gone)")
    why_not = protected_reason(bench, row, tasks)
    if why_not:
        raise Refused(f"{handle} is {why_not} - never closed by this tool")
    settled, why = settled_reason(bench, row, tasks, product)
    if not settled:
        raise Refused(f"{handle} is not settled: {why}")
    if not bench.guardian._close(handle):
        raise Refused(f"Orca refused to close {handle}")
    return why


def reap(bench: Bench, *, run: str | None = None, apply: bool = False) -> list[dict]:
    """Classify this project's agent tabs: settled (closable) or kept with the reason. Dry unless ``apply``."""
    run_id = resolve_run(run)
    tasks = run_tasks(bench, run_id)
    out: list[dict] = []
    for row in inventory(bench):
        if not row.get("connected") or not bench.guardian._is_agent(row):
            continue
        handle = str(row.get("handle"))
        entry = {"handle": handle, "title": str(row.get("title") or "")}
        protected = protected_reason(bench, row, tasks)
        if protected:
            entry.update(action="keep", why=f"protected: {protected}")
        else:
            settled, why = settled_reason(bench, row, tasks)
            entry.update(action="close" if settled else "keep", why=why)
        if apply and entry["action"] == "close":
            entry["closed"] = bool(bench.guardian._close(handle))
        out.append(entry)
    return out


# ------------------------------------------------------------------ CLI
def _print_result(code: int, result: dict) -> None:
    if result.get("ok"):
        notes = []
        if result.get("enterSent"):
            notes.append("stalled paste: Enter pressed once")
        if result.get("reinjected"):
            notes.append("paste never arrived: short re-injection sent once")
        print(f"Worker {result['handle']} ({result['title']}) received task {result['task']}: proof = {result['signal']}"
              + (f" [{'; '.join(notes)}]" if notes else ""))
        print(f"RESULT: ok handle={result['handle']} signal={result['signal']}")
        return
    print(f"LAUNCH FAILED for task {result['task']}: {result.get('reason')}")
    if result.get("handle"):
        print(f"The tab {result['handle']} was left open: read it with `orca terminal read --terminal {result['handle']} --screen`,"
              " fix or close it, then re-run. Orca still holds the task as dispatched.")
    print(f"RESULT: FAILED handle={result.get('handle') or '-'} reason={result.get('reason')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worker.py", description="Launch with proof, close settled, reap Worker tabs.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("launch", help="create a Worker tab, dispatch a task into it and prove receipt")
    p.add_argument("--task", required=True)
    p.add_argument("--route", required=True, help="a [[roster]] seat name from producer.toml")
    p.add_argument("--title")
    p.add_argument("--run")
    p.add_argument("--product", help="repo-relative file the Worker will write; a change counts as proof of work")
    p.add_argument("--receipt-seconds", type=float, default=RECEIPT_SECONDS)
    p = sub.add_parser("close", help="close one SETTLED Worker tab")
    p.add_argument("--terminal", required=True)
    p.add_argument("--run")
    p.add_argument("--product", help="repo-relative product; committed + clean + tab idle = settled")
    p = sub.add_parser("reap", help="list (and with --apply close) this project's settled Worker tabs")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--run")
    sub.add_parser("load", help="show the [limits] verdict for this machine now")
    return parser


def main(argv: list[str] | None = None, bench: Bench | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bench = bench or make_bench()
        if args.command == "launch":
            bench.receipt_seconds = args.receipt_seconds
            code, result = launch(bench, args.task, args.route, title=args.title, run=args.run, product=args.product)
            _print_result(code, result)
            return code
        if args.command == "close":
            why = close(bench, args.terminal, run=args.run, product=args.product)
            print(f"closed {args.terminal}: {why}")
            return EXIT_OK
        if args.command == "reap":
            rows = reap(bench, run=args.run, apply=args.apply)
            for row in rows:
                state = ("CLOSED" if row.get("closed") else "close-FAILED") if args.apply and row["action"] == "close" \
                    else ("would close" if row["action"] == "close" else "keep")
                print(f"{state:12} {row['handle']}  {row['title'][:40]:40}  {row['why']}")
            settled = sum(1 for row in rows if row["action"] == "close")
            print(f"{settled} settled of {len(rows)} agent tab(s)" + ("" if args.apply or not settled else " - run with --apply to close them"))
            return EXIT_OK
        rows = inventory(bench)
        verdict = machine_verdict(bench, rows)
        machine = bench.guardian._machine
        limits = bench.config.limits
        print(f"limits: max_workers={limits.max_workers or 'off'} min_free_ram_gb={limits.min_free_ram_gb or 'off'} "
              f"max_load={limits.max_load or 'off'}")
        print(f"machine: free RAM {machine.free_ram_gb if machine else '-'} GB, CPU {machine.cpu_percent if machine else '-'}%, "
              f"live workers {len(worker_rows(bench, rows))}")
        for note in verdict.notes:
            print(f"note: {note}")
        print("BUSY - " + verdict.text() if verdict.busy else "ok - new Workers may be launched")
        return EXIT_REFUSED if verdict.busy else EXIT_OK
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except guardian.orca_cli.OrcaCliError as exc:
        print(f"ORCA ERROR: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())

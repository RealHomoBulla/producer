"""worker.py: launch with proof of receipt, load backoff, close only settled Workers, reap. Against a fake Orca."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardian  # noqa: E402
import worker  # noqa: E402
from guardian import Limits, Machine, Seat  # noqa: E402
from test_guardian import FakeOrca, NL, _closes, _creates, _flag, harness, make_config  # noqa: E402

RUN = "run_test"
BUSY = "\u2736 Working\u2026 (esc to interrupt)"
PASTE = "[Pasted Content 900 chars]"


class WorkerOrca(FakeOrca):
    """FakeOrca plus the two orchestration calls worker.py makes.

    ``dispatch_mode`` is what `dispatch --inject` leaves in the new tab:
      works          the brief was submitted and the Worker is busy
      paste-stalls   the paste sits in the composer (inject's own Enter was lost); OUR Enter submits it
      never-arrives  nothing at all reaches the tab; a short typed re-injection does
    """

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[dict] = []
        self.dispatch_mode = "works"
        self.ignore_text = False        # a tab that swallows typed text too
        self.dispatch_error = ""

    def task(self, task_id: str, status: str = "pending", assignee: str = "", created_by: str = "term_boss",
             title: str = "T") -> dict:
        row = {"id": task_id, "status": status, "assignee_handle": assignee,
               "created_by_terminal_handle": created_by, "task_title": title}
        self.tasks.append(row)
        return row

    def __call__(self, *args, timeout=45, allow_error=False):
        if args[:2] == ("orchestration", "task-list"):
            self.calls.append(list(args))
            return {"ok": True, "result": {"runId": RUN, "tasks": [dict(t) for t in self.tasks]}}
        if args[:2] == ("orchestration", "dispatch"):
            self.calls.append(list(args))
            if self.dispatch_error:
                raise guardian.orca_cli.OrcaCliError(self.dispatch_error)
            handle = _flag(args, "--to")
            if self.dispatch_mode == "works":
                self.screens[handle] = "banner" + NL + BUSY
            elif self.dispatch_mode == "paste-stalls":
                self.drafts[handle] = PASTE
            for t in self.tasks:
                if t["id"] == _flag(args, "--task"):
                    t["status"], t["assignee_handle"] = "dispatched", handle
            return {"ok": True, "result": {}}
        if args[:2] == ("terminal", "send"):
            handle = _flag(args, "--terminal")
            if "--text" in args and self.ignore_text:
                self.calls.append(list(args))
                return {"ok": True, "result": {}}
            had = self.drafts.get(handle, "")
            out = super().__call__(*args, timeout=timeout, allow_error=allow_error)
            if "--enter" in args and had and not self.swallow_enter:
                self.screens[handle] = self.screens.get(handle, "") + NL + BUSY
            return out
        return super().__call__(*args, timeout=timeout, allow_error=allow_error)


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


class GitStub:
    def __init__(self) -> None:
        self.committed: dict[str, str] = {}   # path -> sha
        self.dirty: set[str] = set()

    def __call__(self, project, args):
        path = args[-1]
        if args[0] == "log":
            return 0, (self.committed.get(path, "") + NL if path in self.committed else ""), ""
        if args[0] == "status":
            return 0, (f" M {path}" + NL if path in self.dirty else ""), ""
        return 0, "", ""


def make(workdir, *, limits=None, machine=None, seats=None, **over):
    config = make_config(
        roster=seats if seats is not None else [Seat("sonnet", "Sonnet", 1, "claude --model sonnet")],
        limits=limits or Limits(), **over)
    fake = WorkerOrca()
    git = GitStub()
    extra = dict(orca=fake, git_runner=git)
    if machine:
        extra["machine"] = machine
    g, _unused, clock = harness(workdir, config, **extra)
    bench = worker.Bench(guardian=g, own_handle="term_me", transcript_dirs=[workdir / "claude-projects"],
                         supervisor=workdir / "producer-supervisor.json", wall=lambda: 0.0)
    return bench, fake, clock, git


def create_calls(fake):
    return _creates(fake)


# ------------------------------------------------------------------ launch: receipt
def test_launch_proves_receipt_when_the_worker_is_busy(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.task("task_1", title="Build the thing")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == 0 and result["ok"] and result["signal"] == "busy"
    created = create_calls(fake)[0]
    assert _flag(created, "--command") == "claude --model sonnet"
    assert _flag(created, "--title").startswith("Sonnet") and "Build the thing" in _flag(created, "--title")
    dispatch = [c for c in fake.calls if c[:2] == ["orchestration", "dispatch"]][0]
    assert "--inject" in dispatch and _flag(dispatch, "--task") == "task_1" and _flag(dispatch, "--run") == RUN
    assert not result["enterSent"] and not result["reinjected"]
    ledger = (bench.guardian.runtime / worker.LEDGER).read_text(encoding="utf-8").splitlines()
    assert json.loads(ledger[0])["ok"] is True


def test_stalled_paste_is_resubmitted_once_and_then_proved(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_mode = "paste-stalls"
    fake.task("task_1")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == 0 and result["ok"] and result["enterSent"] and not result["reinjected"]
    enters = [c for c in fake.calls if c[:2] == ["terminal", "send"] and "--enter" in c and "--text" not in c]
    assert len(enters) == 1, "exactly one Enter for a stalled paste"


def test_a_paste_that_never_arrived_gets_one_short_reinjection(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_mode = "never-arrives"
    fake.task("task_1")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == 0 and result["reinjected"] and not result["enterSent"]
    typed = [_flag(c, "--text") for c in fake.calls if c[:2] == ["terminal", "send"] and "--text" in c]
    assert typed == [worker.REINJECT_TEXT.format(task="task_1")]


def test_launch_fails_with_the_reason_when_the_paste_stays_in_the_composer(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_mode = "paste-stalls"
    fake.swallow_enter = True       # the Enter never submits
    fake.task("task_1")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == worker.EXIT_FAILED and not result["ok"]
    assert "still in the composer after Enter" in result["reason"]
    assert result["enterSent"] and not _closes(fake), "a failed launch leaves the tab open for inspection"


def test_launch_fails_when_there_is_no_sign_of_work(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_mode = "never-arrives"
    fake.ignore_text = True
    fake.task("task_1")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == worker.EXIT_FAILED and "no sign of work" in result["reason"] and result["reinjected"]
    row = json.loads((bench.guardian.runtime / worker.LEDGER).read_text(encoding="utf-8").splitlines()[-1])
    assert row["ok"] is False and row["handle"] == result["handle"]


def test_a_fresh_transcript_that_names_the_task_is_proof(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_mode = "never-arrives"
    transcripts = workdir / "claude-projects" / "proj"
    transcripts.mkdir(parents=True)
    (transcripts / "session.jsonl").write_text('{"text": "Your task ID is: task_1"}', encoding="utf-8")
    fake.task("task_1")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == 0 and result["signal"] == "transcript"


def test_a_changed_product_file_is_proof(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_mode = "never-arrives"
    product = bench.guardian.project / "report.md"
    product.write_text("old", encoding="utf-8")
    fake.task("task_1")
    orig_dispatch = fake.__call__

    def touching(*args, **kw):
        out = orig_dispatch(*args, **kw)
        if args[:2] == ("orchestration", "dispatch"):
            import os
            os.utime(product, (product.stat().st_mtime + 50, product.stat().st_mtime + 50))
        return out

    bench.guardian.orca = touching
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN, product="report.md")
    assert code == 0 and result["signal"] == "product"


def test_dispatch_failure_is_reported_with_the_tab_handle(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.dispatch_error = "capability missing"
    fake.task("task_1")
    code, result = worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert code == worker.EXIT_FAILED and "dispatch --inject failed" in result["reason"] and result["handle"]


# ------------------------------------------------------------------ launch: refusals
def test_unknown_route_task_and_settled_task_are_refused_before_any_tab_opens(workdir):
    bench, fake, _clock, _git = make(workdir)
    fake.task("task_1")
    fake.task("task_done", status="completed")
    for call, text in (
        (lambda: worker.launch(bench, "task_1", "nope", run=RUN), "unknown route"),
        (lambda: worker.launch(bench, "task_missing", "sonnet", run=RUN), "not in Run"),
        (lambda: worker.launch(bench, "task_done", "sonnet", run=RUN), "already completed"),
    ):
        with pytest.raises(worker.Refused, match=text):
            call()
    assert create_calls(fake) == []


def test_a_task_that_already_has_a_live_tab_is_not_launched_twice(workdir):
    bench, fake, _clock, _git = make(workdir)
    live = fake.add("Sonnet · T", screen=BUSY)
    fake.task("task_1", status="dispatched", assignee=live)
    with pytest.raises(worker.Refused, match="already has a live Worker"):
        worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert create_calls(fake) == []


def test_load_above_the_limit_launches_nothing(workdir):
    bench, fake, _clock, _git = make(workdir, limits=Limits(min_free_ram_gb=2.0), machine=lambda: Machine(1.0, 10.0))
    fake.task("task_1")
    with pytest.raises(worker.Refused) as refused:
        worker.launch(bench, "task_1", "sonnet", run=RUN)
    assert "Machine busy (free RAM 1 GB < 2 GB) \u2014 close finished tabs, do not open new." in str(refused.value)
    assert create_calls(fake) == [] and not [c for c in fake.calls if c[:2] == ["orchestration", "dispatch"]]


def test_worker_cap_counts_only_unprotected_agent_tabs(workdir):
    bench, fake, _clock, _git = make(workdir, limits=Limits(max_workers=2))
    fake.add("Producer Claude Opus", screen=BUSY)
    fake.add("Blitz #2", screen=BUSY)
    fake.add("DS worker 1", screen=BUSY)
    fake.task("task_1")
    code, _result = worker.launch(bench, "task_1", "sonnet", run=RUN)   # 1 worker + 2 protected < cap 2: allowed
    assert code == 0
    fake.task("task_2")
    with pytest.raises(worker.Refused, match=r"workers 2/2"):          # DS worker + the one just launched
        worker.launch(bench, "task_2", "sonnet", run=RUN)


def test_cli_load_reports_busy_with_exit_3(workdir, capsys):
    bench, fake, _clock, _git = make(workdir, limits=Limits(max_load=50.0), machine=lambda: Machine(9.0, 91.0))
    assert worker.main(["load"], bench=bench) == worker.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "BUSY" in out and "CPU 91% > 50%" in out
    bench2, _fake2, _c, _g = make(workdir, limits=Limits(max_load=50.0), machine=lambda: Machine(9.0, 10.0))
    assert worker.main(["load"], bench=bench2) == 0


# ------------------------------------------------------------------ close / reap
def test_close_refuses_the_producer_blitz_own_tab_and_a_coordinator(workdir):
    bench, fake, _clock, _git = make(workdir)
    producer = fake.add("Producer Claude Opus", screen="idle")
    blitz = fake.add("Blitz #4", screen="idle")
    own = fake.add("Anything", screen="idle", handle="term_me")
    boss = fake.add("Planner", screen="idle", handle="term_boss")
    bound = fake.add("Renamed by the owner", screen="idle")
    bench.guardian.state["binding"] = {"handle": bound}
    supervised = fake.add("Elsewhere", screen="idle")
    bench.supervisor.write_text(json.dumps({"activeHandle": supervised}), encoding="utf-8")
    for handle in (producer, blitz, own, boss, bound, supervised):
        fake.task(f"t_{handle}", status="completed", assignee=handle)   # even with a completed task
    for handle in (producer, blitz, own, boss, bound, supervised):
        with pytest.raises(worker.Refused, match="never closed"):
            worker.close(bench, handle, run=RUN)
    assert _closes(fake) == []


def test_close_refuses_unsettled_unregistered_and_foreign_tabs(workdir):
    bench, fake, _clock, _git = make(workdir)
    working = fake.add("Sonnet 1", screen=BUSY)
    fake.task("t_work", status="dispatched", assignee=working)
    nameless = fake.add("DS 1", screen="idle")
    foreign = fake.add("Other project", screen="idle", worktree="C:/elsewhere")
    with pytest.raises(worker.Refused, match="is dispatched"):
        worker.close(bench, working, run=RUN)
    with pytest.raises(worker.Refused, match="no Orca task is assigned"):
        worker.close(bench, nameless, run=RUN)
    with pytest.raises(worker.Refused, match="not a tab of this project"):
        worker.close(bench, foreign, run=RUN)
    assert _closes(fake) == []


def test_close_closes_the_whole_tab_of_a_settled_worker(workdir):
    bench, fake, _clock, _git = make(workdir)
    done = fake.add("Sonnet 1", screen="idle")
    fake.task("t_done", status="completed", assignee=done)
    why = worker.close(bench, done, run=RUN)
    assert "t_done is completed" in why
    closed = _closes(fake)
    assert len(closed) == 1 and "--tab" in closed[0] and _flag(closed[0], "--terminal") == done


def test_a_failed_task_is_settled_too(workdir):
    bench, fake, _clock, _git = make(workdir)
    dead = fake.add("DS 1", screen="idle")
    fake.task("t_dead", status="failed", assignee=dead)
    assert "failed" in worker.close(bench, dead, run=RUN)


def test_close_by_committed_product_for_a_worker_without_capability(workdir):
    bench, fake, _clock, git = make(workdir)
    tab = fake.add("Free seat", screen="idle")
    fake.task("t_free", status="dispatched", assignee=tab)
    with pytest.raises(worker.Refused, match="no commit touches it|is dispatched"):
        worker.close(bench, tab, run=RUN, product="work/agents/reports/x.md")
    git.committed["work/agents/reports/x.md"] = "abc1234"
    git.dirty.add("work/agents/reports/x.md")
    with pytest.raises(worker.Refused, match="uncommitted"):
        worker.close(bench, tab, run=RUN, product="work/agents/reports/x.md")
    git.dirty.clear()
    fake.screens[tab] = BUSY
    with pytest.raises(worker.Refused, match="still working"):
        worker.close(bench, tab, run=RUN, product="work/agents/reports/x.md")
    fake.screens[tab] = "idle"
    assert "abc1234" in worker.close(bench, tab, run=RUN, product="work/agents/reports/x.md")


def test_reap_is_dry_by_default_and_apply_closes_only_settled_unprotected_tabs(workdir):
    bench, fake, _clock, _git = make(workdir)
    producer = fake.add("Producer", screen="idle")
    blitz = fake.add("Blitz", screen="idle")
    done = fake.add("Sonnet 1", screen="idle")
    working = fake.add("Sonnet 2", screen=BUSY)
    stray = fake.add("DS 3", screen="idle")
    fake.task("t_done", status="completed", assignee=done)
    fake.task("t_work", status="dispatched", assignee=working)
    fake.task("t_producer_done", status="completed", assignee=producer)
    dry = {row["handle"]: row for row in worker.reap(bench, run=RUN)}
    assert dry[done]["action"] == "close" and _closes(fake) == []
    assert dry[producer]["action"] == "keep" and dry[blitz]["action"] == "keep"
    assert dry[working]["action"] == "keep" and dry[stray]["action"] == "keep"
    applied = {row["handle"]: row for row in worker.reap(bench, run=RUN, apply=True)}
    assert applied[done]["closed"] is True
    assert [_flag(c, "--terminal") for c in _closes(fake)] == [done]


def test_cli_close_and_reap_report_refusals_with_exit_3(workdir, capsys):
    bench, fake, _clock, _git = make(workdir)
    producer = fake.add("Producer", screen="idle")
    assert worker.main(["close", "--terminal", producer, "--run", RUN], bench=bench) == worker.EXIT_REFUSED
    assert "REFUSED" in capsys.readouterr().out and _closes(fake) == []
    done = fake.add("Sonnet 1", screen="idle")
    fake.task("t_done", status="completed", assignee=done)
    assert worker.main(["reap", "--run", RUN], bench=bench) == 0
    assert "would close" in capsys.readouterr().out and _closes(fake) == []

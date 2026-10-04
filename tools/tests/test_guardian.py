"""Tests for the small generic Guardian (`tools/guardian.py`).

The Guardian's only side effect is the Orca CLI, funnelled through one wrapper, so the
whole watchdog is exercised against a fake that returns terminal lists and screens and
records every call. A fake clock makes the idle / seat-age windows deterministic.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import guardian  # noqa: E402
from guardian import Guardian, GuardianConfig, Route, Seat  # noqa: E402


@pytest.fixture
def workdir():
    """A throwaway dir via `tempfile`, not pytest's `workdir`.

    The machine's pytest basetemp (`%TEMP%\\pytest-of-<user>`) can carry an ACL this user
    cannot enumerate; `tempfile` uses the plain writable `%TEMP%`, so the suite runs anywhere.
    """
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


# ------------------------------------------------------------------ fakes
NL = chr(10)
PROJECT = "C:/fake/project" if sys.platform == "win32" else "/fake/project"


class FakeOrca:
    """A stand-in for `_run_orca`: terminal list/read/create/send/close/wait.

    Models what the real CLI does that the Guardian depends on: rows carry ``worktreePath`` and
    ``incarnationId``; ``send --text`` fills the composer *draft* and ``send --enter`` moves it into
    the transcript (so a bootstrap receipt can be observed); a list can fail or be malformed.
    """

    def __init__(self) -> None:
        self.terminals: list[dict] = []
        self.screens: dict[str, str] = {}
        self.drafts: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.list_mode = "ok"          # ok | raise | malformed
        self.swallow_enter = False     # an Enter that never submits (stalled paste)
        self.refuse_close = False
        self._next = 0

    def add(self, title: str, *, handle: str | None = None, connected: bool = True, screen: str = "",
            worktree: str | None = None, agent: str | None = "claude") -> str:
        handle = handle or f"term_{self._next:04d}"
        self._next += 1
        row = {"handle": handle, "title": title, "connected": connected,
               "worktreePath": PROJECT if worktree is None else worktree,
               "incarnationId": f"inc-{handle}"}
        if agent is not None:
            row["agentIdentity"] = agent
        self.terminals.append(row)
        self.screens[handle] = screen
        return handle

    def remove(self, handle: str) -> None:
        self.terminals = [row for row in self.terminals if row["handle"] != handle]

    def __call__(self, *args: str, timeout: int = 45, allow_error: bool = False) -> dict:
        self.calls.append(list(args))
        if len(args) >= 2 and args[0] == "terminal":
            action = args[1]
            if action == "list":
                if self.list_mode == "raise":
                    raise guardian.orca_cli.OrcaCliError("orca is not running")
                if self.list_mode == "malformed":
                    return {"ok": True, "result": {"oops": 1}}
                return {"ok": True, "result": {"terminals": [dict(row) for row in self.terminals]}}
            if action == "read":
                handle = _flag(args, "--terminal")
                text = self.screens.get(handle, "")
                return {"ok": True, "result": {"terminal": {"tail": text.split(NL), "draft": self.drafts.get(handle, "")}}}
            if action == "create":
                handle = self.add(_flag(args, "--title") or "Producer", screen="")
                row = next(r for r in self.terminals if r["handle"] == handle)
                return {"ok": True, "result": {"terminal": {"handle": handle, "incarnationId": row["incarnationId"]}}}
            if action == "send":
                handle = _flag(args, "--terminal")
                if "--text" in args:
                    self.drafts[handle] = (self.drafts.get(handle, "") + _flag(args, "--text"))
                if "--enter" in args and not self.swallow_enter:
                    draft = self.drafts.pop(handle, "")
                    if draft:
                        self.screens[handle] = (self.screens.get(handle, "") + NL + "> " + draft).strip(NL)
                return {"ok": True, "result": {}}
            if action == "close":
                if self.refuse_close:
                    raise guardian.orca_cli.OrcaCliError("close refused")
                self.remove(_flag(args, "--terminal"))
                return {"ok": True, "result": {}}
            if action == "wait":
                return {"ok": True, "result": {}}
        return {"ok": True, "result": {}}


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def _send_text(call) -> str:
    return _flag(call, "--text") or ""


def _nudges(fake: FakeOrca) -> list[str]:
    return [
        _send_text(call)
        for call in fake.calls
        if call[:2] == ["terminal", "send"] and "GUARDIAN" in _send_text(call)
    ]


def _sent(fake: FakeOrca) -> list[str]:
    return [_send_text(call) for call in fake.calls if call[:2] == ["terminal", "send"] and "--text" in call]


def _creates(fake: FakeOrca) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] == ["terminal", "create"]]


def _closes(fake: FakeOrca) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] == ["terminal", "close"]]


# ------------------------------------------------------------------ harness
def make_config(**over) -> GuardianConfig:
    base = dict(
        interval_seconds=60.0,
        idle_nudge_minutes=10 / 60.0,  # 10 s
        worker_idle_minutes=15 / 60.0,  # 15 s
        seat_max_hours=3.0,  # real value; the rotation test overrides it
        producer_title="Producer",
        producer_command="claude --dangerously-skip-permissions",
        bootstrap_prompt="BOOTSTRAP-PROMPT",
        roster=[],
        absent_confirm_ticks=1,  # most tests do not exercise the confirmation window
    )
    base.update(over)
    return GuardianConfig(**base)


class NoGit:
    """git_runner stand-in: HANDOVER committed (clean) unless a test says otherwise."""

    def __init__(self) -> None:
        self.dirty = False
        self.repo = True

    def __call__(self, project, args):
        if args[:1] == ["rev-parse"]:
            return (0, "true" + NL, "") if self.repo else (128, "", "fatal: not a git repository")
        if args[:1] == ["status"]:
            return 0, (" M work/agents/state/HANDOVER.md" + NL if self.dirty else ""), ""
        return 0, "", ""


def harness(workdir: Path, config: GuardianConfig, *, autonomy: bool = True, start: float = 100.0, **extra):
    fake = FakeOrca()
    clock = Clock(start)
    runtime = Path(workdir)
    if autonomy:
        (runtime / "autonomy.json").write_text(
            json.dumps({"enabled": True, "since": "2026-10-04T00:00:00+00:00",
                        "by": "test", "objective": "test"}), encoding="utf-8")
    project = runtime / "proj"
    (project / "work/agents/state").mkdir(parents=True, exist_ok=True)
    kwargs = dict(orca=fake, clock=clock, runtime=runtime, project=project, sleep=clock.advance,
                  git_runner=NoGit(), probe_runner=lambda command, env, timeout: 0)
    kwargs.update(extra)
    guardian_obj = Guardian(config, **kwargs)
    guardian_obj.project_posix = PROJECT  # the fake's rows carry this worktreePath
    return guardian_obj, fake, clock


def write_handover(guardian_obj: Guardian, when: float, *, checkpoint: bool = True) -> None:
    import os
    guardian_obj.handover_path.write_text("# NOW" + NL, encoding="utf-8")
    os.utime(guardian_obj.handover_path, (when, when))
    if checkpoint:
        guardian_obj.checkpoint_path.write_text("{}", encoding="utf-8")
        os.utime(guardian_obj.checkpoint_path, (when, when))


# ------------------------------------------------------------------ tests
def test_config_parsed_from_toml(workdir):
    (workdir / "producer.toml").write_text(
        '[guardian]\n'
        'interval_seconds = 30\n'
        'idle_nudge_minutes = 5\n'
        'worker_idle_minutes = 8\n'
        'seat_max_hours = 2\n'
        'producer_title = "Boss"\n'
        'producer_command = "claude --x"\n'
        'bootstrap_prompt = "go"\n'
        '\n'
        '[[roster]]\n'
        'name = "sonnet"\n'
        'title_prefix = "Sonnet"\n'
        'count = 1\n'
        'command = "claude --sonnet"\n'
        '\n'
        '[[roster]]\n'
        'name = "deepseek"\n'
        'title_prefix = "DS"\n'
        'count = 2\n'
        'command = "opencode --model ds"\n',
        encoding="utf-8",
    )
    config = guardian.load_config(workdir)
    assert config.interval_seconds == 30
    assert config.producer_title == "Boss"
    assert config.bootstrap_prompt == "go"
    assert [seat.name for seat in config.roster] == ["sonnet", "deepseek"]
    assert config.roster[1].count == 2
    assert config.roster[1].title_prefix == "DS"


def test_autonomy_flag_roundtrip(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    assert guardian.main(["autonomy", "on", "--objective", "build site"]) == 0
    state = json.loads((workdir / "autonomy.json").read_text(encoding="utf-8"))
    assert state["enabled"] is True and state["objective"] == "build site"
    capsys.readouterr()
    assert guardian.main(["autonomy", "status"]) == 0
    assert "ON" in capsys.readouterr().out
    assert guardian.main(["autonomy", "off"]) == 0
    state = json.loads((workdir / "autonomy.json").read_text(encoding="utf-8"))
    assert state["enabled"] is False


def test_missing_producer_launches_and_bootstraps(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config())
    guardian_obj.tick(now=100.0)
    assert any(call[:2] == ["terminal", "create"] for call in fake.calls)
    assert guardian_obj.producer_handle
    assert any(_send_text(call).startswith("BOOTSTRAP-PROMPT") for call in fake.calls)
    assert guardian_obj.binding["bootstrapped"] is True  # the receipt was seen on a fresh screen


def test_idle_producer_nudged_once(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    guardian_obj.tick(now=100.0)  # first sight: not yet idle
    guardian_obj.tick(now=111.0)  # 11 s idle -> one nudge
    guardian_obj.tick(now=112.0)  # inside the window -> no second
    assert len(_nudges(fake)) == 1


def test_busy_producer_not_nudged(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config())
    fake.add("Producer", screen="Working... esc to interrupt")
    guardian_obj.tick(now=100.0)
    guardian_obj.tick(now=111.0)
    assert _nudges(fake) == []


def test_roster_short_nudge_names_seat(workdir):
    config = make_config(roster=[Seat("deepseek", "DS", 2)])
    guardian_obj, fake, _clock = harness(workdir, config)
    fake.add("Producer", screen="idle composer")
    fake.add("DS worker 1", screen="busy")
    guardian_obj.tick(now=100.0)
    nudges = _nudges(fake)
    assert nudges and "deepseek" in nudges[0]


def test_worker_idle_nudge_names_handle(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    worker = fake.add("Worker-1", screen="waiting")
    guardian_obj.tick(now=100.0)
    guardian_obj.tick(now=116.0)
    nudges = _nudges(fake)
    assert nudges and worker in nudges[0]


def test_connection_lost_dialog_fixed(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    fake.add("DS worker", screen="Connection lost, reconnecting...")
    guardian_obj.tick(now=100.0)
    assert any(_send_text(call).strip().lower() == "continue" for call in fake.calls)


def test_seat_age_rotate_then_relaunch(workdir):
    """Rotation is two-phase: ROTATE NOW, then (HANDOVER committed) successor up, THEN the old tab closes."""
    guardian_obj, fake, _clock = harness(workdir, make_config(seat_max_hours=3 / 3600.0))
    old = fake.add("Producer", screen="idle composer")
    guardian_obj.tick(now=100.0)
    assert not any("ROTATE NOW" in _send_text(call) for call in fake.calls)
    guardian_obj.tick(now=105.0)  # age 5 s > 3.6 s
    assert any("ROTATE NOW" in _send_text(call) for call in fake.calls)
    write_handover(guardian_obj, 110.0)
    guardian_obj.config.seat_max_hours = 3.0  # do not rotate the successor at once
    guardian_obj.tick(now=120.0)  # HANDOVER rewritten + committed -> successor launched
    assert _creates(fake) and guardian_obj.producer_handle != old
    assert not _closes(fake), "the old seat must not close in the same tick as the successor launch"
    guardian_obj.tick(now=130.0)  # successor is up and bootstrapped -> now the old tab closes
    assert [call[call.index("--terminal") + 1] for call in _closes(fake)] == [old]


def test_autonomy_off_with_a_producer_present_does_nothing(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config(), autonomy=False)
    fake.add("Producer", screen="idle composer")
    guardian_obj.tick(now=100.0)
    guardian_obj.tick(now=500.0)
    assert not [c for c in fake.calls if c[:2] not in (["terminal", "list"], ["terminal", "read"])]


def test_autonomy_off_no_producer_opens_exactly_one_then_no_duplicate(workdir):
    """The friend flow: `guardian.py start` with autonomy OFF and no Producer tab opens ONE, immediately."""
    guardian_obj, fake, _clock = harness(workdir, make_config(), autonomy=False)
    guardian_obj.tick(now=100.0)
    assert len(_creates(fake)) == 1
    assert any(_send_text(call).startswith("BOOTSTRAP-PROMPT") for call in fake.calls)
    guardian_obj.tick(now=160.0)
    guardian_obj.tick(now=220.0)
    assert len(_creates(fake)) == 1, "the second tick must adopt the tab it opened, not open another"
    assert not _nudges(fake) and not _closes(fake)


def test_autonomy_off_launch_carries_no_unattended_flags(workdir):
    config = make_config()
    config.routes = [Route(name="c", command="claude --model x", args_unattended="--dangerously-skip-permissions")]
    guardian_obj, fake, _clock = harness(workdir, config, autonomy=False)
    guardian_obj.tick(now=100.0)
    assert "--dangerously" not in _flag(_creates(fake)[0], "--command")


def test_autonomy_off_does_not_relaunch_a_producer_the_owner_closed(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config(), autonomy=False)
    guardian_obj.tick(now=100.0)
    fake.remove(guardian_obj.producer_handle)
    guardian_obj.tick(now=200.0)
    assert len(_creates(fake)) == 1


def test_empty_brief_bootstrap_says_run_the_kickoff(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config(), autonomy=False)
    guardian_obj.tick(now=100.0)
    sent = next(t for t in _sent(fake) if t.startswith("BOOTSTRAP-PROMPT"))
    assert "START_PROMPT" in sent and "\u00a70a" in sent and "\u00a70 " in sent


def test_filled_brief_bootstrap_has_no_kickoff_note(workdir):
    guardian_obj, fake, _clock = harness(workdir, make_config(), autonomy=False)
    brief = guardian_obj.project / "work" / "\u0411\u0420\u0418\u0424.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("# brief\n\nstatus: filled.\nA shop for bread.\n", encoding="utf-8")
    guardian_obj.tick(now=100.0)
    sent = next(t for t in _sent(fake) if t.startswith("BOOTSTRAP-PROMPT"))
    assert "kickoff" not in sent

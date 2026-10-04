"""Guardian safety tests: ownership, ambiguity, durable succession, idle/roster, dialogs, bootstrap.

Every test runs the real ``Guardian`` against the fake Orca from ``test_guardian`` (rows carry
``worktreePath`` / ``incarnationId`` / ``agentIdentity``; a draft only reaches the transcript on Enter).
"""
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
from guardian import Guardian, Route, Seat  # noqa: E402
from test_guardian import (  # noqa: E402
    PROJECT, Clock, FakeOrca, _closes, _creates, _flag, _nudges, _send_text, _sent,
    harness, make_config, write_handover,
)


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def _mutations(fake: FakeOrca) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] in (["terminal", "create"], ["terminal", "send"], ["terminal", "close"])]


# ------------------------------------------------------------------ ownership and ambiguity
def test_unreadable_inventory_touches_nothing(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    fake.calls.clear()
    fake.list_mode = "raise"
    g.tick(now=300.0)
    assert _mutations(fake) == []
    assert g._inventory_state == "unverifiable"
    assert json.loads(g.heartbeat_path.read_text(encoding="utf-8"))["inventory"] == "unverifiable"


def test_malformed_inventory_is_not_an_empty_one(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.list_mode = "malformed"
    g.tick(now=100.0)
    assert _mutations(fake) == [], "a list with no 'terminals' array must not launch a Producer"


def test_foreign_worktree_producer_is_never_adopted(workdir):
    g, fake, _ = harness(workdir, make_config())
    other = fake.add("Producer", worktree="C:/some/other/project", screen="idle composer")
    g.tick(now=100.0)
    assert _creates(fake), "this project has no Producer of its own, so one is launched"
    assert g.producer_handle != other
    assert not any(_flag(call, "--terminal") == other for call in _mutations(fake))


def test_foreign_tab_dialogs_and_workers_untouched(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    foreign = fake.add("Blitz", worktree="C:/some/other/project", screen="Connection lost, reconnecting...")
    g.tick(now=100.0)
    g.tick(now=300.0)
    assert not any(_flag(call, "--terminal") == foreign for call in _mutations(fake))


def test_owner_blitz_tab_in_same_project_is_ignored(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    blitz = fake.add("Blitz", screen="Connection lost, reconnecting...")
    g.tick(now=100.0)
    g.tick(now=300.0)
    assert not any(_flag(call, "--terminal") == blitz for call in _mutations(fake))


def test_two_titled_panes_are_ambiguous_nothing_adopted_or_launched(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer A", screen="x")
    fake.add("Producer B", screen="y")
    g.tick(now=100.0)
    assert _mutations(fake) == []
    assert g.binding is None
    assert any(row["kind"] == "incident-ambiguous-producer" for row in g.actions)


def test_bound_producer_absent_needs_confirmation_ticks(workdir):
    g, fake, _ = harness(workdir, make_config(absent_confirm_ticks=2))
    old = fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    fake.remove(old)
    g.tick(now=160.0)
    assert not _creates(fake), "one missing read is not death"
    g.tick(now=220.0)
    assert _creates(fake)


def test_agent_gone_but_shell_survives_is_a_dead_producer(workdir):
    config = make_config(absent_confirm_ticks=1, startup_grace_seconds=0.0)
    config.routes = [Route(name="claude", command="claude", agent="claude")]
    g, fake, _ = harness(workdir, config)
    shell = fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    for row in fake.terminals:
        if row["handle"] == shell:
            row["agentIdentity"] = ""  # the agent exited; the shell tab stayed connected
    g.tick(now=200.0)
    assert _creates(fake)
    assert g.producer_handle != shell


# ------------------------------------------------------------------ idle / roster / nudges
def test_roster_count_zero_disables_the_seat(workdir):
    config = make_config(roster=[Seat("off", "Off", 0)])
    assert guardian.load_config  # sanity: the parser keeps 0 (tested below); here the dataclass path
    g, fake, _ = harness(workdir, config)
    fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    assert _nudges(fake) == [], "count 0 means switched off, not 'wanted 1'"


def test_toml_roster_count_zero_is_not_promoted_to_one(workdir):
    (workdir / "producer.toml").write_text('[[roster]]\nname = "x"\ncount = 0\n', encoding="utf-8")
    assert guardian.load_config(workdir).roster[0].count == 0


def test_busy_producer_is_not_nudged_even_when_roster_is_short(workdir):
    config = make_config(roster=[Seat("deepseek", "DS", 2)])
    g, fake, _ = harness(workdir, config)
    fake.add("Producer", screen="Working (12s · esc to interrupt)")
    g.tick(now=100.0)
    g.tick(now=111.0)
    assert _nudges(fake) == []


def test_pending_notice_survives_a_busy_screen_and_is_sent_when_idle(workdir):
    config = make_config(roster=[Seat("deepseek", "DS", 2)])
    g, fake, _ = harness(workdir, config)
    producer = fake.add("Producer", screen="Working (12s · esc to interrupt)")
    g.tick(now=100.0)
    assert _nudges(fake) == []
    fake.screens[producer] = "idle composer"
    g.tick(now=111.0)
    assert _nudges(fake) and "deepseek" in _nudges(fake)[0]


def test_old_prose_containing_working_does_not_hold_the_producer_busy(workdir):
    g, fake, _ = harness(workdir, make_config())
    old_chat = "I am working on the plan\n" + "\n".join(f"line {i}" for i in range(20)) + "\nidle composer"
    fake.add("Producer", screen=old_chat)
    g.tick(now=100.0)
    g.tick(now=111.0)
    assert len(_nudges(fake)) == 1


def test_ticking_timer_and_spinner_do_not_defeat_idle_detection(workdir):
    g, fake, _ = harness(workdir, make_config())
    producer = fake.add("Producer", screen="done ⠋ 12s")
    g.tick(now=100.0)
    fake.screens[producer] = "done ⠙ 13s"  # only the spinner glyph and the counter repainted
    g.tick(now=111.0)
    assert len(_nudges(fake)) == 1


def test_busy_stuck_producer_is_eventually_nudged(workdir):
    g, fake, _ = harness(workdir, make_config(busy_stuck_minutes=1.0))
    fake.add("Producer", screen="Working (1s · esc to interrupt)")
    g.tick(now=100.0)
    g.tick(now=170.0)  # busy but byte-for-byte frozen for > 1 minute
    assert len(_nudges(fake)) == 1


def test_worker_brief_text_worker_done_does_not_hide_an_idle_worker(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    worker = fake.add("Worker-1", screen="brief: call worker_done when finished\nwaiting")
    g.tick(now=100.0)
    g.tick(now=116.0)
    assert any(worker in text for text in _nudges(fake))


def test_composer_draft_blocks_a_nudge(workdir):
    g, fake, _ = harness(workdir, make_config())
    producer = fake.add("Producer", screen="idle composer")
    fake.drafts[producer] = "the owner is typing this"
    g.tick(now=100.0)
    g.tick(now=111.0)
    assert _nudges(fake) == []


def test_roster_surplus_is_named_but_does_not_trigger_a_nudge(workdir):
    config = make_config(roster=[Seat("ds", "DS", 1)])
    g, fake, _ = harness(workdir, config)
    fake.add("Producer", screen="Working (1s · esc to interrupt)")
    fake.add("DS 1", screen="busy esc to interrupt")
    fake.add("DS 2", screen="busy esc to interrupt")
    g.tick(now=100.0)
    assert _nudges(fake) == []
    assert "surplus" in g._nudge_text(g._seat_status(g._others(g._inventory(), g.producer_handle)), []).lower()


# ------------------------------------------------------------------ dialogs
def test_connection_lost_retries_back_off(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    fake.add("DS worker", screen="Connection lost, reconnecting...")
    g.tick(now=100.0)
    g.tick(now=130.0)  # inside the backoff -> no second 'continue'
    g.tick(now=230.0)  # past the doubled (120 s) backoff -> second try
    assert [t for t in _sent(fake) if t == "continue"].__len__() == 2


def test_connection_lost_in_old_scrollback_is_not_current(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    scroll = "Connection lost\n" + "\n".join(f"output {i}" for i in range(12)) + "\nprompt>"
    fake.add("DS worker", screen=scroll)
    g.tick(now=100.0)
    assert "continue" not in _sent(fake)


def test_model_switch_picks_the_keep_option_on_codex_only(workdir):
    menu = "Select a model\n 1. Try gpt-9\n 2. Keep the current model\n 3. Never ask again"
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    codex = fake.add("Luna", screen=menu, agent="codex")
    other = fake.add("Sonnet", screen=menu, agent="claude")
    g.tick(now=100.0)
    sent_to = {(_flag(c, "--terminal"), _flag(c, "--text")) for c in fake.calls if c[:2] == ["terminal", "send"] and "--text" in c}
    assert (codex, "2") in sent_to
    assert not any(handle == other for handle, _ in sent_to)


def test_model_switch_without_a_keep_option_is_an_incident_not_a_blind_digit(workdir):
    menu = "Select a model\n 1. gpt-9\n 2. gpt-10"
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    codex = fake.add("Luna", screen=menu, agent="codex")
    g.tick(now=100.0)
    assert not any(_flag(c, "--terminal") == codex for c in _mutations(fake))
    assert any(row["kind"] == "incident-dialog-unrecognised" for row in g.actions)


def test_plain_shell_pane_gets_no_dialog_fix(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    shell = fake.add("shell", screen="Connection lost", agent=None)
    fake.terminals[-1]["agentIdentity"] = ""
    g.tick(now=100.0)
    assert not any(_flag(c, "--terminal") == shell for c in _mutations(fake))


# ------------------------------------------------------------------ bootstrap receipt
def test_stalled_paste_is_unverified_then_verified_on_retry(workdir):
    g, fake, _ = harness(workdir, make_config(bootstrap_receipt_seconds=6.0))
    fake.swallow_enter = True
    g.tick(now=100.0)
    assert g.binding["bootstrapped"] is False
    assert any(row["kind"] == "bootstrap-unverified" for row in g.actions)
    fake.swallow_enter = False
    fake.drafts[g.producer_handle] = ""  # the stuck draft is cleared by the retry's fresh send
    g.tick(now=200.0)
    assert g.binding["bootstrapped"] is True


def test_bootstrap_gives_up_visibly_after_max_attempts(workdir):
    g, fake, _ = harness(workdir, make_config(bootstrap_receipt_seconds=3.0, bootstrap_max_attempts=2))
    fake.swallow_enter = True
    for moment in (100.0, 200.0, 300.0, 400.0):
        g.tick(now=moment)
    assert any(row["kind"] == "incident-bootstrap-failed" for row in g.actions)
    assert len(_creates(fake)) == 1, "a failed brief must not spawn a second Producer"


# ------------------------------------------------------------------ durable succession
def _start_rotation(workdir, **config):
    g, fake, _ = harness(workdir, make_config(seat_max_hours=3 / 3600.0, **config))
    old = fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    g.tick(now=105.0)  # ROTATE NOW asked at 105
    g.config.seat_max_hours = 3.0
    return g, fake, old


def test_no_close_and_no_launch_while_handover_is_uncommitted(workdir):
    g, fake, old = _start_rotation(workdir)
    write_handover(g, 110.0)
    g.git_runner.dirty = True
    g.tick(now=120.0)
    g.tick(now=130.0)
    assert not _creates(fake) and not _closes(fake)
    assert g.producer_handle == old


def test_no_close_when_handover_was_not_rewritten_after_the_request(workdir):
    g, fake, old = _start_rotation(workdir)
    write_handover(g, 50.0)  # older than the ROTATE NOW at 105
    g.tick(now=120.0)
    assert not _creates(fake) and not _closes(fake)


def test_successor_is_up_before_the_old_seat_closes(workdir):
    g, fake, old = _start_rotation(workdir)
    write_handover(g, 110.0)
    g.tick(now=120.0)
    order = [call[1] for call in fake.calls if call[:2] in (["terminal", "create"], ["terminal", "close"])]
    assert order == ["create"]
    g.tick(now=130.0)
    order = [call[1] for call in fake.calls if call[:2] in (["terminal", "create"], ["terminal", "close"])]
    assert order == ["create", "close"]


def test_old_seat_stays_when_the_successor_brief_is_unconfirmed(workdir):
    g, fake, old = _start_rotation(workdir, bootstrap_receipt_seconds=3.0)
    write_handover(g, 110.0)
    fake.swallow_enter = True
    g.tick(now=120.0)
    g.tick(now=130.0)
    assert _creates(fake) and not _closes(fake)
    assert any(item["handle"] == old for item in g.state["pendingClose"])


def test_stalled_rotation_never_hard_closes(workdir):
    g, fake, old = _start_rotation(workdir, rotate_max_asks=2, rotate_ask_minutes=1.0)
    for moment in (200.0, 300.0, 400.0, 2400.0, 5000.0):
        g.tick(now=moment)
    asks = [t for t in _sent(fake) if t.startswith("ROTATE NOW")]
    assert len(asks) == 2
    assert not _creates(fake) and not _closes(fake)
    assert any(row["kind"] == "incident-rotation-stalled" for row in g.actions)


def test_close_refusal_keeps_the_old_seat_pending_and_visible(workdir):
    g, fake, old = _start_rotation(workdir)
    write_handover(g, 110.0)
    g.tick(now=120.0)
    fake.refuse_close = True
    g.tick(now=130.0)
    assert any(row["kind"] == "incident-close-failed" for row in g.actions)
    fake.refuse_close = False
    g.tick(now=140.0)
    assert [_flag(c, "--terminal") for c in _closes(fake)][-1] == old


def test_seat_start_survives_a_guardian_restart(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    again = Guardian(make_config(), orca=fake, clock=Clock(500.0), runtime=workdir, project=g.project,
                     sleep=lambda s: None, git_runner=g.git_runner)
    again.project_posix = PROJECT
    assert again.producer_started_at == 100.0
    assert again.producer_handle == g.producer_handle


def test_rotation_request_is_void_if_a_different_pane_is_bound(workdir):
    g, fake, old = _start_rotation(workdir)
    g.state["binding"]["handle"] = fake.add("Producer", screen="idle composer")
    write_handover(g, 110.0)
    g.tick(now=120.0)
    assert not _closes(fake)

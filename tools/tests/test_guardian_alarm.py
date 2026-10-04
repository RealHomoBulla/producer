"""The wake-up alarm: reset-time parsing, sleep -> wake at reset (+2 min), a dead tab after reset is relaunched.

One Claude subscription = one 5-hour window shared by every seat, so when the limit message shows and no other
route exists, the Guardian parks the seat and wakes it when the window resets. These tests run the real tick.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardian  # noqa: E402
from guardian import Route, Seat, WAKE_TEXT, WORKER_WAKE_TEXT, parse_reset_time  # noqa: E402
from test_guardian import _closes, _creates, _flag, _sent, harness, make_config  # noqa: E402


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def utc(year, month, day, hour=0, minute=0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


NOW = utc(2026, 10, 4, 12, 0)
DOT = "∙"


# ------------------------------------------------------------------ reset-time parsing
@pytest.mark.parametrize(
    "text, expected",
    [
        # format 1 — clock time (the next such time after now)
        (f"5-hour limit reached {DOT} resets 7pm (UTC)", utc(2026, 10, 4, 19)),
        ("Claude usage limit reached. Your limit will reset at 7:30 AM (UTC).", utc(2026, 10, 5, 7, 30)),
        ("limit resets 19:00 (UTC)", utc(2026, 10, 4, 19)),
        # format 2 — date + time (Codex prints a full date) and ISO
        ("You've hit your usage limit. Try again at Oct 7th, 2026 4:03 PM.", utc(2026, 10, 7, 16, 3)),
        ("limit resets Oct 9 at 7pm (UTC)", utc(2026, 10, 9, 19)),
        ("resets 2026-10-05T07:00:00Z", utc(2026, 10, 5, 7)),
        # format 3 — relative
        ("limit resets in 2h 15m", NOW + 2 * 3600 + 15 * 60),
        ("try again in 45 minutes", NOW + 45 * 60),
        ("try again in 1 day 3 hours", NOW + 27 * 3600),
    ],
)
def test_reset_time_formats(text, expected):
    assert parse_reset_time(text, NOW, "UTC") == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello world",
        "limit resets sometime soon",
        "try again at 99:99",
        "resets 13pm",
        "resets Dec 1, 2027 3pm (UTC)",  # more than 8 days away is not a reset time
    ],
)
def test_reset_time_garbage_is_none(text):
    assert parse_reset_time(text, NOW, "UTC") is None


def test_reset_time_clock_rolls_to_tomorrow_when_already_past():
    evening = utc(2026, 10, 4, 20, 0)
    assert parse_reset_time("resets 7pm (UTC)", evening, "UTC") == pytest.approx(utc(2026, 10, 5, 19))


def test_reset_time_named_zone_in_the_message():
    try:
        guardian.ZoneInfo("Europe/Berlin")
    except Exception:  # noqa: BLE001 - no tzdata on this machine
        pytest.skip("tzdata not installed")
    # Berlin is UTC+2 on 4 Oct 2026: 7pm there = 17:00 UTC
    assert parse_reset_time("resets 7pm (Europe/Berlin)", NOW, "UTC") == pytest.approx(utc(2026, 10, 4, 17))


def test_reset_time_without_any_zone_uses_the_machine_zone():
    result = parse_reset_time("resets 7pm", NOW, "")
    assert result is not None and NOW < result <= NOW + 24 * 3600


# ------------------------------------------------------------------ sleep -> wake
LIMIT = f"5-hour limit reached {DOT} resets 7pm (UTC)"


def alarm_harness(workdir, screen=LIMIT, **config):
    cfg = make_config(timezone_name="UTC", **config)  # one subscription: only the legacy single route
    g, fake, clock = harness(workdir, cfg, start=NOW)
    producer = fake.add("Producer", screen=screen)
    return g, fake, producer


def test_limit_marks_the_seat_sleeping_in_guardian_json_then_wakes_at_reset_plus_two_minutes(workdir):
    g, fake, producer = alarm_harness(workdir)
    g.tick(now=NOW)            # limit message seen once
    g.tick(now=NOW + 60)       # confirmed -> sleep (no other route to hand over to)
    beat = json.loads(g.heartbeat_path.read_text(encoding="utf-8"))
    sleeping = {row["handle"]: row for row in beat["sleeping"]}
    assert producer in sleeping and sleeping[producer]["kind"] == "producer" and sleeping[producer]["parsed"]
    assert sleeping[producer]["wakeAt"] == pytest.approx(utc(2026, 10, 4, 19, 2))
    for moment in (utc(2026, 10, 4, 15), utc(2026, 10, 4, 19, 1)):  # before reset + 2 min: silence
        g.tick(now=moment)
    assert WAKE_TEXT not in _sent(fake)
    assert not _creates(fake) and not _closes(fake)
    g.tick(now=utc(2026, 10, 4, 19, 2, ) + 1)
    assert _sent(fake).count(WAKE_TEXT) == 1
    beat = json.loads(g.heartbeat_path.read_text(encoding="utf-8"))
    assert beat["sleeping"] == []


def test_a_sleeping_producer_is_not_nudged_or_rotated(workdir):
    g, fake, producer = alarm_harness(workdir, seat_max_hours=1 / 3600.0)
    g.tick(now=NOW)
    g.tick(now=NOW + 60)
    fake.calls.clear()
    g.tick(now=NOW + 3600)
    g.tick(now=NOW + 7200)
    assert _sent(fake) == []


def test_unreadable_reset_falls_back_to_a_thirty_minute_retry(workdir):
    g, fake, producer = alarm_harness(workdir, screen="You have hit your usage limit.")
    g.tick(now=NOW)
    g.tick(now=NOW + 60)
    entry = g.state["sleeping"][producer]
    assert entry["parsed"] is False
    assert entry["wakeAt"] == pytest.approx(NOW + 60 + 30 * 60)
    g.tick(now=NOW + 60 + 29 * 60)
    assert WAKE_TEXT not in _sent(fake)
    g.tick(now=NOW + 60 + 31 * 60)
    assert _sent(fake).count(WAKE_TEXT) == 1


def test_message_still_there_after_the_wake_puts_the_seat_back_to_sleep(workdir):
    g, fake, producer = alarm_harness(workdir, screen="You have hit your usage limit.")
    g.tick(now=NOW)
    g.tick(now=NOW + 60)
    g.tick(now=NOW + 2000)                  # woken
    assert producer not in g.state["sleeping"]
    g.tick(now=NOW + 2010)                  # inside the wake grace: not re-slept yet
    assert producer not in g.state["sleeping"]
    g.tick(now=NOW + 2400)                  # grace over, message still on screen
    g.tick(now=NOW + 2460)
    assert producer in g.state["sleeping"]  # still limited: sleeps again, never gives up


def test_dead_tab_after_the_reset_is_relaunched_with_the_bootstrap(workdir):
    g, fake, producer = alarm_harness(workdir)
    g.tick(now=NOW)
    g.tick(now=NOW + 60)                    # asleep until 19:02
    fake.remove(producer)                   # the tab dies while asleep
    g.tick(now=utc(2026, 10, 4, 15))
    g.tick(now=utc(2026, 10, 4, 19, 1))
    assert not _creates(fake), "relaunching before the reset would only hit the limit again"
    g.tick(now=utc(2026, 10, 4, 19, 3))
    assert len(_creates(fake)) == 1
    assert any(text.startswith("BOOTSTRAP-PROMPT") and "resume from HANDOVER" in text for text in _sent(fake))
    assert g.producer_handle != producer and g.binding["bootstrapped"]


def test_a_worker_on_a_limit_sleeps_and_is_woken_at_reset(workdir):
    config = make_config(timezone_name="UTC", roster=[Seat("sonnet", "Sonnet", 1)])
    g, fake, _clock = harness(workdir, config, start=NOW)
    fake.add("Producer", screen="Working (3s · esc to interrupt)")
    worker = fake.add("Sonnet 1", screen="Claude usage limit reached. Your limit will reset at 7pm (UTC).")
    g.tick(now=NOW)
    g.tick(now=NOW + 60)
    entry = g.state["sleeping"][worker]
    assert entry["kind"] == "worker" and entry["parsed"]
    g.tick(now=utc(2026, 10, 4, 18, 59))
    assert WORKER_WAKE_TEXT not in _sent(fake)
    g.tick(now=utc(2026, 10, 4, 19, 3))
    sent_to_worker = [
        _flag(call, "--text") for call in fake.calls
        if call[:2] == ["terminal", "send"] and _flag(call, "--terminal") == worker and "--text" in call
    ]
    assert sent_to_worker.count(WORKER_WAKE_TEXT) == 1


def test_with_another_route_a_limit_hands_over_instead_of_sleeping(workdir):
    config = make_config(timezone_name="UTC")
    config.routes = [Route("claude", "claude", title="Producer Claude", limit_patterns=["limit reached"]),
                     Route("codex", "codex", title="Producer Codex")]
    g, fake, _clock = harness(workdir, config, start=NOW)
    old = fake.add("Producer Claude", screen=LIMIT)
    g.tick(now=NOW)
    g.tick(now=NOW + 60)
    assert g.binding["route"] == "codex" and not g.state["sleeping"]
    status = g.state["routes"]["claude"]
    assert status["until"] == pytest.approx(utc(2026, 10, 4, 19, 2)), "the limited route is retried at its reset time"
    assert g.producer_handle != old


# The shipped default route block, kept here verbatim so the test never reads the project's live
# producer.toml: `setup.py` rewrites that file to the owner's preset, so asserting a preset's route
# names on it would fail for a stranger who ran setup (the documented flow). Written to a tmp dir
# and loaded as a "template default".
DEFAULT_PRODUCER_TOML = """\
[project]
name = "producer"

[guardian]
producer_command = "claude --model claude-opus-5-5 --effort high"

[[producer_routes]]
name = "claude-opus"
title = "Producer Claude Opus"
agent = "claude"
command = "claude --model claude-opus-5-5 --effort high"
limit_patterns = ['usage limit', '\\d-hour limit', 'weekly limit', 'rate limit', 'out of credits', '\\b429\\b']
probe = "claude --version"

[[producer_routes]]
name = "codex-luna"
title = "Producer Codex Luna"
agent = "codex"
command = "codex -m gpt-6-luna -c model_reasoning_effort=max"
limit_patterns = ['usage limit', 'try again at', 'rate limit', 'out of credits', '\\b429\\b']
probe = "codex --version"
"""


def test_default_producer_toml_routes_are_loadable_and_ordered(tmp_path):
    (tmp_path / "producer.toml").write_text(DEFAULT_PRODUCER_TOML, encoding="utf-8")
    config = guardian.load_config(tmp_path, strict=True)
    assert config.problems == []
    assert [route.name for route in config.routes][:2] == ["claude-opus", "codex-luna"]
    assert all(route.command and route.limit_patterns for route in config.routes)


def test_the_projects_own_producer_toml_always_loads_after_setup():
    """Whatever preset `setup.py` wrote, the project's live config must load with no problems and
    complete routes — this is the file a stranger actually ends up with after the setup step."""
    config = guardian.load_config(strict=True)
    assert config.problems == []
    assert config.routes and all(route.name and route.command and route.limit_patterns for route in config.routes)

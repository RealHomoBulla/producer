"""Producer routes: limit -> switch, probe fail -> skip, recover -> return at rotation, keys, route CLI, presets."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardian  # noqa: E402
from guardian import Route  # noqa: E402
from test_guardian import (  # noqa: E402
    _closes, _creates, _flag, _sent, harness, make_config, write_handover,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def routes_config(**over):
    config = make_config(**over)
    config.routes = [
        Route("A", "cmd-A", title="Producer A", agent="claude", probe="probe-A", limit_patterns=["usage limit"]),
        Route("B", "cmd-B", title="Producer B", agent="claude", probe="probe-B", limit_patterns=["usage limit"]),
        Route("C", "cmd-C", title="Producer C", agent="opencode"),
    ]
    return config


class Probes:
    def __init__(self) -> None:
        self.failing: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, command, env, timeout):
        self.calls.append(command)
        return 1 if command in self.failing else 0


def routes_harness(workdir, **over):
    probes = Probes()
    g, fake, clock = harness(workdir, routes_config(**over), probe_runner=probes)
    return g, fake, probes


def command_of(call) -> str:
    return _flag(call, "--command") or ""


def title_of(call) -> str:
    return _flag(call, "--title") or ""


# ------------------------------------------------------------------ limit -> switch
def test_limit_message_hands_the_seat_to_the_next_live_route(workdir):
    g, fake, probes = routes_harness(workdir)
    old = fake.add("Producer A", screen="You've hit your usage limit")
    g.tick(now=100.0)
    assert not _creates(fake), "one sighting is not enough"
    g.tick(now=160.0)
    create = _creates(fake)[-1]
    assert command_of(create).startswith("cmd-B") and title_of(create) == "Producer B"
    assert g.binding["route"] == "B"
    brief = next(t for t in _sent(fake) if t.startswith("BOOTSTRAP-PROMPT"))
    assert "Previous seat hit its limit; resume from HANDOVER." in brief and "route B" in brief
    assert g.producer_handle != old


def test_limit_text_while_the_producer_is_busy_is_ignored(workdir):
    g, fake, probes = routes_harness(workdir)
    fake.add("Producer A", screen="old: usage limit\nWorking (3s · esc to interrupt)")
    g.tick(now=100.0)
    g.tick(now=160.0)
    assert not _creates(fake)


def test_limit_seat_is_left_open_until_handover_is_committed(workdir):
    g, fake, probes = routes_harness(workdir)
    old = fake.add("Producer A", screen="You've hit your usage limit")
    g.tick(now=100.0)
    g.tick(now=160.0)
    g.tick(now=220.0)
    assert not _closes(fake)
    assert any(row["kind"] == "incident-close-deferred" for row in g.actions)
    write_handover(g, 300.0)
    g.tick(now=360.0)
    assert [_flag(c, "--terminal") for c in _closes(fake)] == [old]


def test_route_whose_probe_fails_is_skipped(workdir):
    g, fake, probes = routes_harness(workdir)
    probes.failing.add("probe-B")
    fake.add("Producer A", screen="You've hit your usage limit")
    g.tick(now=100.0)
    g.tick(now=160.0)
    assert command_of(_creates(fake)[-1]).startswith("cmd-C")


def test_dead_producer_whose_route_probe_fails_goes_to_the_next_route(workdir):
    g, fake, probes = routes_harness(workdir)
    old = fake.add("Producer A", screen="idle composer")
    g.tick(now=100.0)
    probes.failing.add("probe-A")
    fake.remove(old)
    g.tick(now=200.0)
    assert command_of(_creates(fake)[-1]).startswith("cmd-B")
    brief = next(t for t in _sent(fake) if t.startswith("BOOTSTRAP-PROMPT"))
    assert "Previous seat died; resume from HANDOVER." in brief


def test_dead_producer_whose_probe_is_fine_comes_back_on_the_same_route(workdir):
    g, fake, probes = routes_harness(workdir)
    old = fake.add("Producer A", screen="idle composer")
    g.tick(now=100.0)
    fake.remove(old)
    g.tick(now=200.0)
    assert command_of(_creates(fake)[-1]).startswith("cmd-A")


def test_every_route_limited_launches_nothing_and_says_so(workdir):
    g, fake, probes = routes_harness(workdir)
    old = fake.add("Producer A", screen="idle composer")
    g.tick(now=100.0)
    for name in ("A", "B", "C"):
        g.mark_limited(name, 100.0, None)
    fake.remove(old)
    g.tick(now=200.0)
    assert not _creates(fake)
    assert any(row["kind"] == "incident-no-live-route" for row in g.actions)
    g.tick(now=100.0 + 31 * 60)  # recovery window over: the best route is back
    assert command_of(_creates(fake)[-1]).startswith("cmd-A")


# ------------------------------------------------------------------ recover -> return at rotation
def _rotate_at(g, fake, t0: float) -> None:
    g.config.seat_max_hours = 3 / 3600.0
    g.tick(now=t0)                 # ROTATE NOW
    write_handover(g, t0 + 1)
    g.tick(now=t0 + 10)            # HANDOVER committed -> successor on the chosen route


def _seat_on_b_with_a_limited(workdir):
    g, fake, probes = routes_harness(workdir)
    fake.add("Producer A", screen="idle composer")
    g.tick(now=100.0)
    g.mark_limited("A", 100.0, None)
    g.state["binding"]["route"] = "B"
    return g, fake, probes


def test_higher_route_is_not_returned_to_before_thirty_minutes(workdir):
    g, fake, probes = _seat_on_b_with_a_limited(workdir)
    _rotate_at(g, fake, 400.0)
    assert command_of(_creates(fake)[-1]).startswith("cmd-B")


def test_recovered_higher_route_is_returned_to_at_the_next_rotation(workdir):
    g, fake, probes = _seat_on_b_with_a_limited(workdir)
    _rotate_at(g, fake, 100.0 + 31 * 60)
    assert command_of(_creates(fake)[-1]).startswith("cmd-A")
    assert "limitedAt" not in g.state["routes"]["A"]


def test_recovered_route_whose_probe_still_fails_is_not_returned_to(workdir):
    g, fake, probes = _seat_on_b_with_a_limited(workdir)
    probes.failing.add("probe-A")
    _rotate_at(g, fake, 100.0 + 31 * 60)
    assert command_of(_creates(fake)[-1]).startswith("cmd-B")


# ------------------------------------------------------------------ launch command shape, keys
def test_route_command_sources_keys_appends_unattended_flags_and_leaks_no_secret(workdir):
    keys = workdir / "keys.env"
    keys.write_text('export OPENCODE_API_KEY="sk-SECRET-123"\n# comment\nOTHER=plain\n', encoding="utf-8")
    config = make_config()
    config.keys_file_raw = "~/.config/producer/keys.env"
    config.routes = [Route("go", "claude --model m", title="Producer Go", keys=True, shell_windows="git-bash",
                           args_unattended="--dangerously-skip-permissions", probe="probe-go")]
    seen_env: list[dict] = []
    g, fake, _clock = harness(workdir, config, probe_runner=lambda c, e, t: seen_env.append(e) or 0)
    g.config.keys_file_raw = str(keys)
    g.tick(now=100.0)
    create = _creates(fake)[0]
    command = command_of(create)
    assert command == f'set -a; . "{str(keys).replace(chr(92), "/")}"; set +a; claude --model m --dangerously-skip-permissions'
    assert ("--shell" in create) == (os.name == "nt")
    assert seen_env and seen_env[0]["OPENCODE_API_KEY"] == "sk-SECRET-123" and seen_env[0]["OTHER"] == "plain"
    assert "SECRET" not in repr(fake.calls) and "SECRET" not in g.log_path.read_text(encoding="utf-8")


def test_default_keys_path_uses_home_not_a_machine_path(workdir):
    config = make_config()
    config.routes = [Route("go", "claude", keys=True)]
    g, fake, _clock = harness(workdir, config)
    assert g._launch_command(config.routes[0]).startswith('set -a; . "$HOME/.config/producer/keys.env"; set +a; ')


def test_load_keys_env_parses_export_quotes_and_comments(workdir):
    path = workdir / "k.env"
    path.write_text("# c\nexport A='1'\nB=\"two words\"\n\nC=3\nbad line\n", encoding="utf-8")
    assert guardian.load_keys_env(path) == {"A": "1", "B": "two words", "C": "3"}
    assert guardian.load_keys_env(workdir / "missing.env") == {}


# ------------------------------------------------------------------ config + route CLI
SAMPLE = """\
# preamble
[project]
name = "x"

[guardian]
interval_seconds = 60

# first route
[[producer_routes]]
name = "one"
command = "c1"

# second route
[[producer_routes]]
name = "two"
command = "c2"
enabled = true

[[producer_routes]]
name = "three"
command = "c3"

[[roster]]
name = "w"
count = 1
"""


def test_reorder_routes_moves_the_primary_and_keeps_everything_else():
    text = guardian.reorder_routes(SAMPLE, "three")
    assert tomllib.loads(text)["producer_routes"][0]["name"] == "three"
    assert [r["name"] for r in tomllib.loads(text)["producer_routes"]] == ["three", "one", "two"]
    assert "# preamble" in text and "[[roster]]" in text and "interval_seconds = 60" in text
    with pytest.raises(KeyError):
        guardian.reorder_routes(SAMPLE, "nope")


def test_set_route_enabled_edits_or_inserts_the_flag():
    off = guardian.set_route_enabled(SAMPLE, "two", False)
    assert [r.get("enabled", True) for r in tomllib.loads(off)["producer_routes"]] == [True, False, True]
    inserted = guardian.set_route_enabled(SAMPLE, "one", False)
    assert tomllib.loads(inserted)["producer_routes"][0]["enabled"] is False
    assert tomllib.loads(guardian.set_route_enabled(off, "two", True))["producer_routes"][1]["enabled"] is True


def test_route_command_rewrites_producer_toml_and_rejects_unknown_names(workdir, monkeypatch, capsys):
    (workdir / "producer.toml").write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(guardian.paths, "PROJECT", workdir)
    assert guardian.main(["route", "--primary", "two"]) == 0
    assert guardian.load_config(workdir).routes[0].name == "two"
    before = (workdir / "producer.toml").read_text(encoding="utf-8")
    assert guardian.main(["route", "--primary", "nope"]) == 1
    assert (workdir / "producer.toml").read_text(encoding="utf-8") == before
    assert guardian.main(["route", "--disable", "one"]) == 0
    assert [r.name for r in guardian.load_config(workdir).effective_routes()] == ["two", "three"]


def test_routes_command_lists_routes_and_probe_status(workdir, monkeypatch, capsys):
    (workdir / "producer.toml").write_text(SAMPLE.replace('command = "c1"', 'command = "c1"\nprobe = "p1"')
                                           .replace('command = "c3"', 'command = "c3"\nprobe = "p3"'), encoding="utf-8")
    monkeypatch.setattr(guardian.paths, "PROJECT", workdir)
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir / "rt"))
    monkeypatch.setattr(guardian, "default_probe_runner", lambda command, env, timeout: 0 if command == "p1" else 1)
    assert guardian.main(["routes"]) == 0
    out = capsys.readouterr().out
    assert "one" in out and "alive" in out and "PROBE FAILED" in out and "no probe" in out
    assert guardian.main(["routes", "--no-probe"]) == 0
    assert "not probed" in capsys.readouterr().out


def test_config_problems_are_reported_not_silently_defaulted(workdir):
    (workdir / "producer.toml").write_text(
        '[guardian]\ninterval_seconds = -5\nseat_max_hours = "soon"\n'
        '[[producer_routes]]\nname = "bad"\n'
        '[[producer_routes]]\nname = "rx"\ncommand = "c"\nlimit_patterns = ["(unclosed"]\n'
        '[[producer_routes]]\nname = "rx"\ncommand = "c2"\n', encoding="utf-8")
    config = guardian.load_config(workdir)
    assert config.interval_seconds >= 5.0
    text = " | ".join(config.problems)
    assert "interval_seconds" in text and "seat_max_hours" in text
    assert "needs name and command" in text and "bad limit pattern" in text and "duplicate route name" in text


def test_strict_load_refuses_a_broken_file(workdir):
    (workdir / "producer.toml").write_text("[guardian\nbroken", encoding="utf-8")
    with pytest.raises(guardian.ConfigError):
        guardian.load_config(workdir, strict=True)
    assert guardian.load_config(workdir).problems


def test_legacy_producer_command_becomes_a_single_route():
    config = guardian.GuardianConfig(producer_command="claude --x")
    assert [route.name for route in config.effective_routes()] == ["default"]
    assert guardian.GuardianConfig().effective_routes() == []


# ------------------------------------------------------------------ presets
def test_single_claude_preset_replaces_routes_and_roster_and_parses(workdir, monkeypatch, capsys):
    monkeypatch.setattr(guardian.paths, "PROJECT", workdir)
    (workdir / "producer.toml").write_text(SAMPLE, encoding="utf-8")
    assert guardian.main(["preset", "single-claude"]) == 0  # legacy alias, print only
    assert guardian.main(["preset", "solo-claude"]) == 0    # setup.py's name is the same preset
    assert (workdir / "producer.toml").read_text(encoding="utf-8") == SAMPLE
    assert guardian.main(["preset", "solo-claude", "--apply"]) == 0
    config = guardian.load_config(workdir)
    assert [r.name for r in config.routes] == ["claude-sonnet"]
    assert [s.name for s in config.roster] == ["sonnet", "haiku"]
    assert "sonnet-5-5 --effort medium" in config.routes[0].command
    assert "haiku" in config.roster[1].command
    assert tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))["project"]["name"] == "x"
    assert guardian.main(["preset", "nope"]) == 1

"""Guardian: [limits] load backoff (config, readers, nudge text) and opt-in Remote Control (typed once per tab)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardian  # noqa: E402
from guardian import Limits, Machine, RemoteControlConfig, Route, Seat  # noqa: E402
from test_guardian import (  # noqa: E402
    FakeOrca, NL, _nudges, _send_text, _sent, harness, make_config,
)

RC = "/remote-control"
COMPOSER = "❯ " + NL + "  ⏵⏵ bypass permissions on (shift+tab to cycle)"
COMPOSER_RC_ON = "❯ " + NL + "  ⏵⏵ bypass permissions on · /rc"
COMPOSER_MASKED = "❯ " + NL + "  ⏵⏵ bypass permissions on · 1 shell, 1 monitor · ← for agents"


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def rc_sends(fake: FakeOrca, handle: str | None = None) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] == ["terminal", "send"] and _send_text(call) == RC
            and (handle is None or call[call.index("--terminal") + 1] == handle)]


# ------------------------------------------------------------------ [limits] and [remote_control] config
def test_limits_and_remote_control_are_off_by_default(workdir):
    config = guardian.load_config(workdir)
    assert config.limits == Limits(0, 0.0, 0.0) and not config.limits.measured
    assert config.remote_control.enabled is False
    assert config.remote_control.titles == ["Producer", "Blitz"]


def test_limits_and_remote_control_parsed_from_toml(workdir):
    (workdir / "producer.toml").write_text(
        '[limits]\nmax_workers = 4\nmin_free_ram_gb = 2.5\nmax_load = 85\n\n'
        '[remote_control]\nenabled = true\ntitles = ["Producer", "Owner"]\nverify_seconds = 30\n\n'
        '[[roster]]\nname = "ds"\ntitle_prefix = "DS"\nkeys = true\nshell_windows = "git-bash"\ncommand = "opencode"\n',
        encoding="utf-8")
    config = guardian.load_config(workdir)
    assert (config.limits.max_workers, config.limits.min_free_ram_gb, config.limits.max_load) == (4, 2.5, 85.0)
    assert config.remote_control.enabled is True and config.remote_control.titles == ["Producer", "Owner"]
    assert config.remote_control.verify_seconds == 30
    assert config.roster[0].keys is True and config.roster[0].shell_windows == "git-bash"


def test_bad_limit_values_switch_that_limit_off_and_say_so(workdir):
    (workdir / "producer.toml").write_text(
        '[limits]\nmax_workers = 2.5\nmin_free_ram_gb = "lots"\nmax_load = 400\n[remote_control]\nenabled = "yes"\n',
        encoding="utf-8")
    config = guardian.load_config(workdir)
    assert config.limits == Limits(0, 0.0, 0.0)
    assert config.remote_control.enabled is False  # only a real boolean opts in
    text = " | ".join(config.problems)
    assert "max_workers" in text and "min_free_ram_gb" in text and "max_load" in text


# ------------------------------------------------------------------ machine readers and verdict
def test_linux_reader_computes_cpu_percent_from_two_samples(workdir):
    proc = workdir / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 4194304 kB\n", encoding="utf-8")
    (proc / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\n", encoding="utf-8")

    def sleep(_seconds):  # between the samples: 200 busy ticks, 100 idle ticks -> 66.7 %
        (proc / "stat").write_text("cpu  200 0 200 750 150 0 0 0 0 0\n", encoding="utf-8")

    machine = guardian._read_machine_linux(proc, sleep)
    assert machine.free_ram_gb == 4.0 and machine.cpu_percent == 66.7 and not machine.error


def test_linux_reader_reports_unreadable_proc(workdir):
    assert guardian._read_machine_linux(workdir / "nope", lambda s: None).error


def test_windows_reader_parses_cim_output_and_reports_failure():
    ok = guardian._read_machine_windows(lambda argv, timeout: (0, "8388608\r\n42\r\n"), lambda: None)
    assert ok.free_ram_gb == 8.0 and ok.cpu_percent == 42.0
    kernel = guardian._read_machine_windows(lambda argv, timeout: (0, "1048576\r\n42\r\n"), lambda: 6.5)
    assert kernel.free_ram_gb == 6.5, "the kernel's available figure (with standby cache) beats CIM FreePhysicalMemory"
    bad = guardian._read_machine_windows(lambda argv, timeout: (1, "boom"), lambda: None)
    assert bad.free_ram_gb is None and "CIM read failed" in bad.error
    half = guardian._read_machine_windows(lambda argv, timeout: (1, "boom"), lambda: 3.0)
    assert half.free_ram_gb == 3.0 and half.cpu_percent is None


def test_assess_load_blocks_over_each_limit_and_never_on_unmeasured():
    limits = Limits(max_workers=3, min_free_ram_gb=2.0, max_load=80.0)
    assert not guardian.assess_load(limits, Machine(8.0, 30.0), 2).busy
    full = guardian.assess_load(limits, Machine(8.0, 30.0), 3)
    assert full.busy and not full.pressure and "workers 3/3" in full.text()
    ram = guardian.assess_load(limits, Machine(1.2, 30.0), 0)
    assert ram.busy and ram.pressure and "free RAM 1.2 GB < 2 GB" in ram.text()
    cpu = guardian.assess_load(limits, Machine(8.0, 93.0), 0)
    assert cpu.busy and "CPU 93% > 80%" in cpu.text()
    blind = guardian.assess_load(limits, Machine(error="no proc"), 0)
    assert not blind.busy and "not measurable" in blind.notes[0]
    assert guardian.assess_load(Limits(), Machine(0.1, 99.0), 50).busy is False  # no limits = no brake


# ------------------------------------------------------------------ the nudge
def test_busy_machine_nudge_tells_the_producer_not_to_open_new(workdir):
    config = make_config(limits=Limits(min_free_ram_gb=2.0), roster=[Seat("deepseek", "DS", 2)])
    g, fake, _clock = harness(workdir, config, machine=lambda: Machine(1.0, 10.0))
    fake.add("Producer", screen="idle composer")
    fake.add("DS worker 1", screen="busy")
    g.tick(now=100.0)
    nudges = _nudges(fake)
    assert nudges, "memory pressure alone must reach the Producer"
    assert "Machine busy (free RAM 1 GB < 2 GB) — close finished tabs, do not open new." in nudges[0]
    assert "Roster short" not in nudges[0], "a busy machine must not also say «launch the missing seat»"
    assert g.state["incidents"] == [] and g._machine_row()["busy"] is True


def test_worker_cap_reached_suppresses_roster_short_but_does_not_nag(workdir):
    config = make_config(limits=Limits(max_workers=1), roster=[Seat("deepseek", "DS", 2)])
    g, fake, _clock = harness(workdir, config)
    fake.add("Producer", screen="busy esc to interrupt")
    fake.add("DS worker 1", screen="busy esc to interrupt")
    g.tick(now=100.0)
    assert _nudges(fake) == [], "at the cap nothing is wrong: no nudge, only no new launches"


def test_roster_short_nudge_still_fires_when_the_machine_is_fine(workdir):
    config = make_config(limits=Limits(min_free_ram_gb=2.0), roster=[Seat("deepseek", "DS", 2)])
    g, fake, _clock = harness(workdir, config, machine=lambda: Machine(9.0, 10.0))
    fake.add("Producer", screen="idle composer")
    fake.add("DS worker 1", screen="busy")
    g.tick(now=100.0)
    assert "Roster short" in _nudges(fake)[0] and "Machine busy" not in _nudges(fake)[0]


def test_unmeasurable_machine_never_blocks_and_is_reported_once(workdir):
    config = make_config(limits=Limits(max_load=50.0))
    g, fake, _clock = harness(workdir, config, machine=lambda: Machine(error="CIM read failed"))
    fake.add("Producer", screen="idle composer")
    g.tick(now=100.0)
    g.tick(now=101.0)
    assert [row["kind"] for row in g.state["incidents"]] == ["machine-unmeasured"]
    assert not g.load_verdict(0).busy


# ------------------------------------------------------------------ Remote Control
def rc_config(**over):
    return make_config(remote_control=RemoteControlConfig(enabled=True, verify_seconds=120.0), **over)


def test_remote_control_is_off_by_default(workdir):
    g, fake, _clock = harness(workdir, make_config())
    fake.add("Producer", screen=COMPOSER)
    g.tick(now=100.0)
    assert rc_sends(fake) == []


def test_remote_control_typed_once_per_tab_across_many_ticks(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    producer = fake.add("Producer Claude Opus", screen=COMPOSER)
    blitz = fake.add("Blitz #3", screen=COMPOSER)
    worker = fake.add("Sonnet · task", screen=COMPOSER)
    for tick in range(5):
        g.tick(now=100.0 + tick)
    assert len(rc_sends(fake, producer)) == 1 and len(rc_sends(fake, blitz)) == 1
    assert rc_sends(fake, worker) == [], "a Worker tab is never an owner tab"
    assert g.state["remoteControl"][producer]["state"] == "typed"


def test_remote_control_works_with_autonomy_off(workdir):
    g, fake, _clock = harness(workdir, rc_config(), autonomy=False)
    producer = fake.add("Producer", screen=COMPOSER)
    g.tick(now=100.0)
    g.tick(now=101.0)
    assert len(rc_sends(fake, producer)) == 1


def test_remote_control_confirmed_when_rc_shows_and_never_retyped(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    producer = fake.add("Producer", screen=COMPOSER)
    g.tick(now=100.0)
    fake.screens[producer] = COMPOSER_RC_ON
    g.tick(now=105.0)
    g.tick(now=400.0)
    assert g.state["remoteControl"][producer]["state"] == "on" and len(rc_sends(fake)) == 1


def test_remote_control_already_on_is_recorded_not_typed(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    producer = fake.add("Producer", screen=COMPOSER_RC_ON)
    g.tick(now=100.0)
    assert rc_sends(fake) == [] and g.state["remoteControl"][producer]["state"] == "on"


def test_remote_control_masked_status_bar_is_unknown_not_off(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    fake.add("Producer", screen=COMPOSER_MASKED)
    g.tick(now=100.0)
    g.tick(now=101.0)
    assert rc_sends(fake) == [], "background tasks hide /rc: typing would open the Disconnect menu"


def test_remote_control_waits_for_a_free_prompt(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    producer = fake.add("Producer", screen=COMPOSER + NL + "working… (esc to interrupt)")
    g.tick(now=100.0)
    assert rc_sends(fake) == []
    fake.screens[producer] = COMPOSER
    fake.drafts[producer] = "half-typed owner text"
    g.tick(now=101.0)
    assert rc_sends(fake) == [], "never collide with text somebody is typing"
    fake.drafts[producer] = ""
    g.tick(now=102.0)
    assert len(rc_sends(fake)) == 1


def test_remote_control_skips_non_claude_tabs(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    fake.add("Producer", screen=COMPOSER, agent="codex")
    g.tick(now=100.0)
    assert rc_sends(fake) == []


def test_remote_control_unconfirmed_is_reported_and_not_retyped(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    producer = fake.add("Producer", screen=COMPOSER)
    g.tick(now=100.0)
    g.tick(now=300.0)  # 200 s later, still no /rc
    g.tick(now=900.0)
    assert len(rc_sends(fake)) == 1
    assert g.state["remoteControl"][producer]["state"] == "unverified"
    assert any(row["kind"] == "remote-control-unverified" and "type /remote-control yourself" in row["detail"]
               for row in g.state["incidents"])


def test_remote_control_retyped_only_for_a_new_incarnation_of_the_pane(workdir):
    g, fake, _clock = harness(workdir, rc_config())
    producer = fake.add("Producer", screen=COMPOSER)
    g.tick(now=100.0)
    for row in fake.terminals:
        row["incarnationId"] = "inc-restarted"  # Claude Code was restarted in the same pane
    g.tick(now=105.0)
    assert len(rc_sends(fake, producer)) == 2


def test_remote_control_skips_a_proxy_route_that_has_no_claude_login(workdir):
    config = rc_config()
    config.routes = [Route("proxy", "claude --model x", title="Producer via proxy", agent="claude", keys=True)]
    g, fake, _clock = harness(workdir, config)
    g.tick(now=100.0)  # launches the Producer on the only route
    handle = g.producer_handle
    fake.screens[handle] = COMPOSER
    g.tick(now=101.0)
    assert rc_sends(fake) == [] and g.state["remoteControl"][handle]["state"] == "skipped-api-route"


def test_remote_control_is_typed_before_the_bootstrap_brief(workdir):
    class ComposerOrca(FakeOrca):
        def __call__(self, *args, **kw):
            out = super().__call__(*args, **kw)
            if args[:2] == ("terminal", "create"):
                self.screens[out["result"]["terminal"]["handle"]] = COMPOSER
            return out

    fake = ComposerOrca()
    g, _unused, _clock = harness(workdir, rc_config(), orca=fake)
    g.tick(now=100.0)
    sent = [text for text in _sent(fake)]
    assert RC in sent and any(text.startswith("BOOTSTRAP-PROMPT") for text in sent)
    assert sent.index(RC) < next(i for i, text in enumerate(sent) if text.startswith("BOOTSTRAP-PROMPT"))
    g.tick(now=101.0)
    g.tick(now=102.0)
    assert len(rc_sends(fake)) == 1, "the per-tick step must not type a second time after the launch path did"

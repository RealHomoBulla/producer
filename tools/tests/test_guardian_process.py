"""Process lifetime: instance lock, status health, stop/start safety, log rotation, config reload, loop survival."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardian  # noqa: E402
from guardian import InstanceLock  # noqa: E402
from test_guardian import _creates, harness, make_config  # noqa: E402


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def test_instance_lock_is_exclusive_and_released(workdir):
    path = workdir / "guardian.lock"
    first = InstanceLock(path)
    assert first.acquire()
    assert InstanceLock.held(path) is True
    assert InstanceLock(path).acquire() is False
    first.release()
    assert InstanceLock.held(path) is False


def test_status_says_stopped_without_a_lock_even_if_a_stale_pid_file_exists(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    (workdir / "guardian.pid").write_text(json.dumps({"pid": os.getpid(), "token": "t"}), encoding="utf-8")
    assert guardian.main(["status"]) == 0
    assert "Guardian: stopped" in capsys.readouterr().out  # PID reuse cannot fake «running»: the lock decides


def test_status_flags_a_hung_guardian_whose_heartbeat_is_stale(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    lock = InstanceLock(workdir / "guardian.lock")
    assert lock.acquire()
    try:
        (workdir / "guardian.json").write_text(json.dumps(
            {"lastTick": "x", "lastTickEpoch": time.time() - 3600, "intervalSeconds": 60}), encoding="utf-8")
        assert guardian.main(["status"]) == 0
        assert "STALE" in capsys.readouterr().out
    finally:
        lock.release()


def test_status_shows_degraded_and_unverifiable_inventory(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    (workdir / "guardian.json").write_text(json.dumps(
        {"lastTick": "x", "degraded": True, "consecutiveErrors": 3, "lastError": "boom", "inventory": "unverifiable"}),
        encoding="utf-8")
    guardian.main(["status"])
    out = capsys.readouterr().out
    assert "DEGRADED" in out and "boom" in out and "UNVERIFIABLE" in out


def test_start_refuses_a_second_instance(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    lock = InstanceLock(workdir / "guardian.lock")
    assert lock.acquire()
    try:
        assert guardian.main(["start"]) == 0
        assert "already running" in capsys.readouterr().out
    finally:
        lock.release()


def test_supervise_exits_when_another_instance_holds_the_lock(workdir, monkeypatch):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    lock = InstanceLock(workdir / "guardian.lock")
    assert lock.acquire()
    try:
        assert guardian.main(["supervise"]) == 3
    finally:
        lock.release()


def test_stop_when_nothing_runs_is_clean(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    assert guardian.main(["stop"]) == 0
    assert "not running" in capsys.readouterr().out


def test_stop_refuses_to_kill_a_pid_that_does_not_match_the_running_instance(workdir, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_RUNTIME", str(workdir))
    monkeypatch.setattr(guardian.time, "sleep", lambda s: None)
    lock = InstanceLock(workdir / "guardian.lock")
    assert lock.acquire()
    killed = []
    monkeypatch.setattr(guardian.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **k: killed.append("taskkill"))
    try:
        (workdir / "guardian.pid").write_text(json.dumps({"pid": 999999, "token": "A"}), encoding="utf-8")
        (workdir / "guardian.json").write_text(json.dumps({"token": "B"}), encoding="utf-8")
        assert guardian.main(["stop"]) == 1
        assert killed == [] and "not killing anything" in capsys.readouterr().err
    finally:
        lock.release()


def test_log_is_rotated_and_bounded(workdir):
    config = make_config(log_max_bytes=400)
    g, fake, _ = harness(workdir, config)
    for index in range(80):
        g._action("probe", detail=f"line {index} " + "x" * 20)
    names = sorted(path.name for path in workdir.glob("guardian.log*"))
    assert "guardian.log.1" in names
    assert (workdir / "guardian.log").stat().st_size < 2000
    assert len(names) <= 3


def test_per_handle_tables_are_pruned_to_live_tabs(workdir):
    g, fake, _ = harness(workdir, make_config())
    fake.add("Producer", screen="idle composer")
    worker = fake.add("Worker-1", screen="waiting")
    g.tick(now=100.0)
    assert worker in g.changed_at
    fake.remove(worker)
    g.tick(now=200.0)
    assert worker not in g.changed_at and worker not in g.screens and worker not in g.nudged_at


def test_a_raising_tick_degrades_the_heartbeat_and_does_not_kill_the_loop(workdir, monkeypatch):
    g, fake, _ = harness(workdir, make_config())
    calls = {"n": 0}

    def explode(now=None):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(g, "tick", explode)
    assert guardian._supervise_loop(g, 5.0, once=True) == 0
    assert g.consecutive_errors == 1 and "boom" in g.last_error
    beat = json.loads(g.heartbeat_path.read_text(encoding="utf-8"))
    assert beat["degraded"] is True and beat["lastError"].startswith("RuntimeError")


def test_config_hot_reload_keeps_the_last_good_config_on_a_broken_edit(workdir):
    project = workdir / "proj"
    project.mkdir()
    toml = project / "producer.toml"
    toml.write_text('[guardian]\ninterval_seconds = 60\nseat_max_hours = 3\n[[producer_routes]]\nname = "a"\ncommand = "ca"\n',
                    encoding="utf-8")
    g, fake, _ = harness(workdir, guardian.load_config(project), project=project,
                         config_loader=lambda: guardian.load_config(project, strict=True))
    assert [r.name for r in g.config.routes] == ["a"]
    toml.write_text('[guardian]\ninterval_seconds = 60\n[[producer_routes]]\nname = "b"\ncommand = "cb"\n', encoding="utf-8")
    os.utime(toml, (time.time() + 10, time.time() + 10))
    g.tick(now=100.0)
    assert [r.name for r in g.config.routes] == ["b"]
    toml.write_text("[guardian\nbroken", encoding="utf-8")
    os.utime(toml, (time.time() + 20, time.time() + 20))
    g.tick(now=200.0)
    assert [r.name for r in g.config.routes] == ["b"], "a broken edit must not replace the working config with defaults"
    assert any(row["kind"] == "incident-config-invalid" for row in g.actions)


def test_service_command_prints_a_restart_recipe(capsys):
    assert guardian.main(["service"]) == 0
    out = capsys.readouterr().out
    assert "schtasks" in out and "systemctl --user" in out and "start" in out

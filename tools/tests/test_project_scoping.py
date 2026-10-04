"""Project isolation: the mailbox, the Run binding, the rotation alarm and the Orca CLI pin.

Two copies of the skeleton on one machine must never share a mailbox, a lock, a Run or an OS alarm
name; and a status line must never claim a clean state it could not check.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _copy_of_the_toolchain(root: Path, name: str):
    """Load producer_mailbox/paths/drain/producer as if `root` were a separate project copy."""
    root.mkdir(parents=True)
    (root / "producer.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    tools = root / "tools"
    tools.mkdir()
    for file in ("paths.py", "producer_mailbox.py", "orca_cli.py", "drain_mailbox.py",
                 "mailbox_cursor.py", "producer.py"):
        (tools / file).write_text((TOOLS / file).read_text(encoding="utf-8"), encoding="utf-8")
    names = ("paths", "producer_mailbox", "orca_cli", "drain_mailbox", "mailbox_cursor", "producer")
    saved_path = list(sys.path)
    saved_modules = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(tools))
    try:
        loaded = {m: __import__(m) for m in ("paths", "producer_mailbox", "drain_mailbox", "producer")}
    finally:
        sys.path[:] = saved_path
        for module in names:
            sys.modules.pop(module, None)
        sys.modules.update(saved_modules)
    return loaded


def test_each_project_gets_its_own_mailbox_directory_and_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCER_MAILBOX_DIR", raising=False)
    one = _copy_of_the_toolchain(tmp_path / "one", "one")
    two = _copy_of_the_toolchain(tmp_path / "two", "two")
    assert one["producer_mailbox"].mailbox_dir() != two["producer_mailbox"].mailbox_dir()
    assert one["producer_mailbox"].mailbox_dir() == tmp_path / "one" / ".runtime" / "mailbox"
    one["producer_mailbox"].ensure_mailbox_dir()
    two["producer_mailbox"].ensure_mailbox_dir()
    first = one["drain_mailbox"].single_instance()
    second = two["drain_mailbox"].single_instance()
    try:
        assert first is not None and second is not None  # one drainer per project, not per machine
        assert one["producer_mailbox"].mailbox_file("drain_mailbox.lock") !=             two["producer_mailbox"].mailbox_file("drain_mailbox.lock")
        assert one["drain_mailbox"].single_instance() is None  # but never two for the same project
    finally:
        for handle in (first, second):
            if handle:
                handle.close()


def test_env_override_moves_the_mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCER_MAILBOX_DIR", str(tmp_path / "elsewhere"))
    copy = _copy_of_the_toolchain(tmp_path / "p", "p")
    assert copy["producer_mailbox"].mailbox_dir() == tmp_path / "elsewhere"


def test_run_resolver_order_and_conflicts(tmp_path, monkeypatch):
    copy = _copy_of_the_toolchain(tmp_path / "p", "p")
    paths = copy["paths"]
    assert paths.resolve_run() == "" and paths.run_conflicts() == []
    assert paths.resolve_run("run_flag") == "run_flag"
    paths.RUNTIME.mkdir(parents=True)
    paths.CHECKPOINT.write_text(json.dumps({"activeRun": {"id": "run_checkpoint"}}), encoding="utf-8")
    assert paths.resolve_run() == "run_checkpoint"
    monkeypatch.setattr(paths, "configured_run", lambda: "run_toml")
    assert paths.resolve_run() == "run_toml"
    paths.SUPERVISOR.write_text(json.dumps({"activeRunId": "run_bound"}), encoding="utf-8")
    assert paths.resolve_run() == "run_bound"
    assert len(paths.run_conflicts()) == 3


def test_bind_run_is_the_binding_every_tool_follows(tmp_path, capsys):
    copy = _copy_of_the_toolchain(tmp_path / "p", "p")
    producer = copy["producer"]
    assert producer.main(["bind-run", "not-a-run"]) == 2
    assert producer.main(["bind-run", "run_abc123"]) == 0
    assert copy["paths"].resolve_run() == "run_abc123"
    assert producer.bound_run() == "run_abc123"
    assert copy["drain_mailbox"].current_run() == "run_abc123"
    assert "run_abc123" in capsys.readouterr().out


def test_rotation_alarm_name_differs_per_project(tmp_path):
    one = _copy_of_the_toolchain(tmp_path / "one", "same-name")
    two = _copy_of_the_toolchain(tmp_path / "two", "same-name")
    assert one["producer"].rotation_alarm_task() != two["producer"].rotation_alarm_task()
    assert one["producer"].rotation_alarm_task().startswith("ProducerRotate-same-name-")


def test_status_says_unverifiable_when_git_cannot_answer(tmp_path, capsys):
    copy = _copy_of_the_toolchain(tmp_path / "p", "p")  # a folder, not a git repository
    copy["producer"].main(["status"])
    out = capsys.readouterr().out
    assert "unverifiable" in out and "0 dirty" not in out


def test_rotation_target_refuses_an_unreadable_terminal_list(tmp_path, monkeypatch):
    copy = _copy_of_the_toolchain(tmp_path / "p", "p")
    producer = copy["producer"]
    copy["paths"].RUNTIME.mkdir(parents=True)
    copy["paths"].SUPERVISOR.write_text(json.dumps({"activeHandle": "term_1"}), encoding="utf-8")
    monkeypatch.setattr(producer, "_orca", lambda *a, **k: None)
    handle, detail = producer._rotation_target()
    assert handle is None and "unreadable" in detail


def _drain():
    return _load("drain_scoping_under_test", TOOLS / "drain_mailbox.py")


def test_drained_row_keeps_the_whole_message_and_a_redelivery_is_not_written_twice(tmp_path):
    drain = _drain()
    log = tmp_path / "actionable.jsonl"
    message = {"id": "msg_1", "type": "worker_done", "subject": "done", "body": "x" * 3000,
               "payload": {"taskId": "task_1", "dispatchId": "ctx_1", "outcome": "succeeded",
                           "reportPath": "work/agents/reports/a.md", "filesModified": ["a.md"]}}
    first = drain.append_actionable(log, "delivery_1", [message], run="run_1")
    again = drain.append_actionable(log, "delivery_1", [message, {"id": "msg_2", "type": "mystery"}],
                                    run="run_1")
    assert len(first) == 1 and [row["id"] for row in again] == ["msg_2"]
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in rows] == ["msg_1", "msg_2"]
    assert len(rows[0]["body"]) == 3000 and rows[0]["run"] == "run_1"
    assert rows[0]["message"]["payload"]["dispatchId"] == "ctx_1"


def test_a_failed_ack_is_logged_and_retried_soon(tmp_path):
    drain = _drain()
    calls, errors = [], []

    class Done:
        def __init__(self, stdout="", returncode=0, stderr=""):
            self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    claim = json.dumps({"ok": True, "result": {"deliveryId": "d1", "messages": [
        {"id": "m1", "type": "question", "subject": "q"}]}})

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return Done(claim) if "--wait" in cmd else Done("", 1, "boom")

    drain.LOG = str(tmp_path / "a.jsonl")
    drain._orca = lambda: "orca"
    pause = drain.drain_pass("run_x", {}, runner=runner, log_fn=lambda path, text: errors.append(text))
    assert pause > 2 and any("ack of d1" in line for line in errors)
    assert "--types" not in calls[0]  # unknown message types must wake the drainer too


def test_an_explicit_orca_pin_is_never_silently_replaced(monkeypatch, tmp_path):
    orca_cli = _load("orca_cli_under_test", TOOLS / "orca_cli.py")
    monkeypatch.setenv("ORCA_CLI_COMMAND", str(tmp_path / "no-such-orca"))
    with pytest.raises(orca_cli.OrcaCliError, match="unavailable"):
        orca_cli.resolve_cli()


def test_a_windows_cmd_shim_is_replaced_by_the_native_exe_beside_it(tmp_path):
    orca_cli = _load("orca_cli_native_under_test", TOOLS / "orca_cli.py")
    (tmp_path / "orca.cmd").write_text("@echo off", encoding="utf-8")
    assert orca_cli.prefer_native(str(tmp_path / "orca.cmd"), windows=True) == str(tmp_path / "orca.cmd")
    (tmp_path / "orca.exe").write_bytes(b"MZ")
    assert orca_cli.prefer_native(str(tmp_path / "orca.cmd"), windows=True) == str(tmp_path / "orca.exe")
    assert orca_cli.prefer_native(str(tmp_path / "orca.cmd"), windows=False) == str(tmp_path / "orca.cmd")


def test_importing_the_toolchain_never_leaves_a_strict_console_encoding():
    import io
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import paths; print('\\u2014 \\u0414 ok')" % str(TOOLS))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          env={**__import__("os").environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"})
    assert done.returncode == 0 and b"ok" in done.stdout

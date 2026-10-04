"""The Producer's mailbox log is read by cursor, and the drainer stamps monotonic lines.

The drainer and cursor tools are loaded from ``tools/``; the mailbox directory is redirected with
a real temp file so nothing here touches /var/tmp.
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


@pytest.fixture
def drain():
    return _load("drain_mailbox_under_test", TOOLS / "drain_mailbox.py")


@pytest.fixture
def cursor_tool():
    return _load("mailbox_cursor_under_test", TOOLS / "mailbox_cursor.py")


def _old_row(n: int) -> str:
    return json.dumps({"at": "23:01:43", "delivery": f"delivery_{n}", "id": f"msg_old{n}",
                       "type": "worker_done", "subject": f"old {n}", "body": ""}) + "\n"


def test_drained_rows_carry_a_monotonic_line_and_a_dated_stamp(drain, tmp_path):
    log = tmp_path / "actionable.jsonl"
    log.write_text(_old_row(1) + _old_row(2) + '{"torn": ', encoding="utf-8")
    rows = drain.append_actionable(log, "delivery_new", [
        {"id": "msg_a", "type": "worker_done", "subject": "done"},
        {"id": "msg_b", "type": "question", "subject": "which?"}])
    assert [row["line"] for row in rows] == [4, 5]
    lines = log.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[3])["line"] == 4 and json.loads(lines[4])["id"] == "msg_b"
    assert rows[0]["at"][:2] == "20" and "T" in rows[0]["at"]
    with log.open("a", encoding="utf-8") as handle:
        handle.write(_old_row(3))
    assert drain.append_actionable(log, "d", [{"id": "msg_c", "type": "worker_done"}])[0]["line"] == 7


@pytest.mark.parametrize("message, kept", [
    ({"type": "heartbeat"}, False),
    ({"type": "status", "subject": "Producer self-check"}, False),
    ({"type": "status", "subject": "🔴 Worker escalation: task_x"}, True),
    ({"type": "worker_done"}, True),
    ({"type": "question"}, True),
    ({"type": "escalation"}, True),
    ({"type": "decision_gate"}, True),
])
def test_the_drainer_filter(drain, message, kept):
    assert drain.is_actionable(message) is kept


def test_current_run_prefers_explicit_then_binding_then_config(tmp_path, monkeypatch, drain):
    paths = drain.paths
    monkeypatch.setattr(paths, "SUPERVISOR", tmp_path / "missing.json")
    monkeypatch.setattr(paths, "CHECKPOINT", tmp_path / "missing-checkpoint.json")
    assert drain.current_run("run_explicit") == "run_explicit"
    monkeypatch.setattr(paths, "configured_run", lambda: "run_config")
    assert drain.current_run() == "run_config"
    bound = tmp_path / "supervisor.json"
    bound.write_text(json.dumps({"activeRunId": "run_bound"}), encoding="utf-8")
    monkeypatch.setattr(paths, "SUPERVISOR", bound)
    assert drain.current_run() == "run_bound"  # bind-run outranks producer.toml
    monkeypatch.setattr(paths, "configured_run", lambda: "")
    monkeypatch.setattr(paths, "SUPERVISOR", tmp_path / "missing.json")
    assert drain.current_run() == ""  # never a hard-coded fallback


def test_no_cursor_is_never_guessed_and_writes_nothing(cursor_tool, tmp_path, capsys):
    log, cursor = tmp_path / "a.jsonl", tmp_path / "a.cursor"
    log.write_text(_old_row(1) + _old_row(2), encoding="utf-8")
    assert cursor_tool.main(["--log", str(log), "--cursor", str(cursor)]) == 2
    assert "NO CURSOR" in capsys.readouterr().out
    assert cursor_tool.main(["status", "--log", str(log), "--cursor", str(cursor)]) == 2
    assert not cursor.exists()


def test_new_lists_exactly_the_lines_after_the_cursor_and_ack_only_moves_forward(cursor_tool, tmp_path, capsys):
    log, cursor = tmp_path / "a.jsonl", tmp_path / "a.cursor"
    log.write_text("".join(_old_row(n) for n in range(1, 6)) + '{"torn', encoding="utf-8")
    args = ["--log", str(log), "--cursor", str(cursor)]
    assert cursor_tool.main(["ack", "3", *args]) == 0
    assert cursor.read_text(encoding="utf-8") == "3 msg_old3\n"
    capsys.readouterr()
    assert cursor_tool.main(["new", *args]) == 0
    out = capsys.readouterr().out
    assert "msg_old4" in out and "msg_old5" in out and "msg_old3" not in out
    assert "torn" not in out
    assert cursor_tool.main(["ack", "6", *args]) == 2
    assert cursor_tool.main(["ack", "2", *args]) == 2
    assert cursor.read_text(encoding="utf-8").startswith("3 ")
    assert cursor_tool.main(["ack", "2", "--force", *args]) == 0

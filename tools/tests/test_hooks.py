"""The two Producer hooks: they record what was submitted, they hold back a stop only in the one
safe case, and in every other case — including every failure — they stay out of the way."""
from __future__ import annotations

import json

import pytest

import hooks
import mailbox_cursor


@pytest.fixture
def project(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    monkeypatch.setattr(hooks, "OWNER_PAGE", tmp_path / "OWNER_LAST_MESSAGES.md")
    monkeypatch.setattr(hooks, "STATUS_FILE", runtime / "hooks-status.json")
    monkeypatch.setattr(hooks, "GUARD_FILE", runtime / "stop-guard.json")
    monkeypatch.setattr(hooks.paths, "RUNTIME", runtime)
    monkeypatch.setattr(hooks.paths, "SUPERVISOR", runtime / "producer-supervisor.json")
    monkeypatch.setattr(mailbox_cursor, "LOG", runtime / "actionable.jsonl")
    monkeypatch.setattr(mailbox_cursor, "CURSOR", runtime / "actionable.cursor")
    return tmp_path


# ------------------------------------------------------------------ owner-message
def test_a_submitted_prompt_is_appended_newest_last(project):
    assert hooks.append_owner_message("make the hero darker", "abcdef123456") is True
    assert hooks.append_owner_message("and bigger", "abcdef123456") is True
    text = hooks.OWNER_PAGE.read_text(encoding="utf-8")
    assert text.startswith("# Owner's last messages")
    assert text.index("make the hero darker") < text.index("and bigger")
    assert "session abcdef12" in text


@pytest.mark.parametrize("prompt", [
    "You are working inside Orca, a multi-agent IDE. You are a dispatched worker.",
    "Please carry out this task from my Orca coordinator by following the brief I pasted below.",
    "You are the Producer. Read work/agents/orca/START_PROMPT.md in full and resume.",
    "ROTATE NOW",
    "   ",
    "",
])
def test_automation_and_empty_prompts_are_skipped(project, prompt):
    assert hooks.append_owner_message(prompt) is False
    assert not hooks.OWNER_PAGE.exists()


def test_only_the_newest_entries_are_kept_and_long_prompts_are_cut(project):
    for number in range(hooks.KEEP + 5):
        hooks.append_owner_message(f"message number {number}")
    hooks.append_owner_message("x" * (hooks.MAX_CHARS + 500))
    text = hooks.OWNER_PAGE.read_text(encoding="utf-8")
    assert "message number 0\n" not in text and "message number 34" in text
    assert text.count("\n## ") == hooks.KEEP
    assert "…(cut)" in text


def test_the_hook_fails_open_and_records_the_error(project, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "read_payload", lambda: {"prompt": "hello"})

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hooks, "append_owner_message", boom)
    assert hooks.command_owner_message() == 0  # never blocks the owner's prompt
    status = json.loads(hooks.STATUS_FILE.read_text(encoding="utf-8"))
    assert "disk full" in status["owner-message"]["lastError"]["detail"]
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------ stop guard
def _arm(project, *, autonomy=True, handle="term_p", rows=2, cursor=0):
    runtime = project / ".runtime"
    (runtime / "autonomy.json").write_text(json.dumps({"enabled": autonomy}), encoding="utf-8")
    (runtime / "guardian-state.json").write_text(json.dumps({"binding": {"handle": handle}}), encoding="utf-8")
    log = [json.dumps({"id": f"msg_{i}", "type": "worker_done", "subject": "done"}) for i in range(rows)]
    mailbox_cursor.LOG.write_text("\n".join(log) + ("\n" if log else ""), encoding="utf-8")
    if cursor is not None:
        mailbox_cursor.CURSOR.write_text(f"{cursor} msg_x\n", encoding="utf-8")


def test_an_autonomous_producer_is_held_back_while_a_delivery_is_unhandled(project):
    _arm(project)
    decision = hooks.guard_decision({}, {"ORCA_TERMINAL_HANDLE": "term_p"})
    assert decision["decision"] == "block"
    assert "2 unhandled" in decision["reason"] and "mailbox_cursor.py ack 2" in decision["reason"]


@pytest.mark.parametrize("name, kwargs, env", [
    ("autonomy off", {"autonomy": False}, {"ORCA_TERMINAL_HANDLE": "term_p"}),
    ("a worker pane", {}, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
    ("no pane identity", {}, {}),
    ("nothing unhandled", {"cursor": 2}, {"ORCA_TERMINAL_HANDLE": "term_p"}),
    ("no cursor yet", {"cursor": None}, {"ORCA_TERMINAL_HANDLE": "term_p"}),
    ("empty queue", {"rows": 0}, {"ORCA_TERMINAL_HANDLE": "term_p"}),
])
def test_the_guard_lets_the_turn_end_everywhere_else(project, name, kwargs, env):
    _arm(project, **kwargs)
    assert hooks.guard_decision({}, env) is None, name


def test_the_guard_lets_go_after_a_few_blocks_so_it_can_never_loop(project):
    _arm(project)
    env = {"ORCA_TERMINAL_HANDLE": "term_p"}
    results = [hooks.guard_decision({}, env) for _ in range(hooks.MAX_BLOCKS + 2)]
    assert [r is not None for r in results] == [True] * hooks.MAX_BLOCKS + [False, False]
    status = json.loads(hooks.STATUS_FILE.read_text(encoding="utf-8"))
    assert "released after" in status["stop-guard"]["lastError"]["detail"]


def test_a_new_message_restarts_the_block_budget(project):
    _arm(project)
    env = {"ORCA_TERMINAL_HANDLE": "term_p"}
    for _ in range(hooks.MAX_BLOCKS + 1):
        hooks.guard_decision({}, env)
    _arm(project, rows=3)  # another Delivery arrived
    assert hooks.guard_decision({}, env) is not None


def test_a_broken_state_file_never_blocks(project, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "read_payload", lambda: {})
    (project / ".runtime" / "autonomy.json").write_text("{not json", encoding="utf-8")
    assert hooks.command_stop_guard() == 0 and capsys.readouterr().out == ""
    monkeypatch.setattr(hooks, "guard_decision", lambda *a, **k: 1 / 0)
    assert hooks.command_stop_guard() == 0 and capsys.readouterr().out == ""
    status = json.loads(hooks.STATUS_FILE.read_text(encoding="utf-8"))
    assert "ZeroDivisionError" in status["stop-guard"]["lastError"]["detail"]


def test_a_block_is_printed_as_the_json_claude_code_expects(project, monkeypatch, capsys):
    _arm(project)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_p")
    monkeypatch.setattr(hooks, "read_payload", lambda: {"stop_hook_active": False})
    assert hooks.command_stop_guard() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["decision"] == "block" and printed["reason"]


# ------------------------------------------------------------------ wiring
def test_the_shipped_settings_wire_both_hooks_to_project_relative_commands():
    wired = hooks.installed_hooks()
    assert wired == {"owner-message": True, "stop-guard": True}
    settings = json.loads((hooks.paths.PROJECT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for group in settings["hooks"].values() for entry in group for h in entry["hooks"]]
    assert all(c.startswith("python tools/") and ":" not in c.split()[2] for c in commands)  # no absolute path
    assert settings["outputStyle"] == "Concise"  # the earlier settings are kept

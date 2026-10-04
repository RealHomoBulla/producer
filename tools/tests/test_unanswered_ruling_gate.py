"""Integration tests for the ruling gate at the unanswered-question write path.

A synthetic project tree under ``tmp_path`` carries the OPEN.md register, so nothing here reads
the live tree.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ruling_gate  # noqa: E402
import unanswered  # noqa: E402


def _write_open(root: Path, body: str) -> None:
    path = root / "work" / "agents" / "registers" / "OPEN.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


OPEN_WITH_RULED_ROW = """# OPEN

| # | row | what it is |
|---|---|---|
| | 1.10 | ✅ РЕШЕНО 2026-09-07: делаем так |
| | 1.20 | 🔴 открыто, ждёт владельца |
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    state = tmp_path / "UNANSWERED_STATE.json"
    page = tmp_path / "БЕЗ_ОТВЕТА.md"
    project = tmp_path / "project"
    project.mkdir()
    _write_open(project, OPEN_WITH_RULED_ROW)
    monkeypatch.setattr(unanswered.paths, "owner_language", lambda: "ru")  # the assertions below are Russian
    monkeypatch.setattr(unanswered, "STATE", state)
    monkeypatch.setattr(unanswered, "PAGE", page)
    monkeypatch.setattr(unanswered, "PROJECT", project)
    return state, page, project


def read_state(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def test_ruled_section_refuses_registration(sandbox, capsys):
    state, page, _project = sandbox
    rc = unanswered.main(["add", "--question", "Что делаем по §1.10?", "--priority", "P1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert not state.exists()
    assert not page.exists()
    assert "Question refused" in out
    assert "FOUND" in out


def test_open_section_registers_question(sandbox):
    state, page, _project = sandbox
    rc = unanswered.main(["add", "--question", "Что делаем по §1.20?", "--priority", "P2"])
    assert rc == 0
    row = read_state(state)["rows"][0]
    assert row["ruling_gate"] == "NOT_FOUND"
    assert row["supersedes"] is False
    assert "§1.20" in page.read_text(encoding="utf-8")


def test_supersedes_requires_reason_and_records_it(sandbox, capsys):
    state, page, _project = sandbox
    rc_missing = unanswered.main(["add", "--question", "§1.10?", "--supersedes"])
    assert rc_missing == 2
    assert not state.exists()
    assert "requires" in capsys.readouterr().out

    reason = "owner explicitly changed the policy"
    rc = unanswered.main(["add", "--question", "§1.10?", "--supersedes", "--reason", reason])
    assert rc == 0
    row = read_state(state)["rows"][0]
    assert row["ruling_gate"] == "FOUND"
    assert row["supersedes"] is True
    assert reason in page.read_text(encoding="utf-8")


def test_unchecked_gate_is_visible_but_question_is_preserved(sandbox, monkeypatch, capsys):
    state, page, _project = sandbox
    unchecked = ruling_gate.GateResult(
        query_type="text", query="UNCHECKED", status="ERROR", permission_to_ask=False,
        matches=[], unreadable_sources=[("OPEN.md", "permission denied")], checked_sources=[],
        summary="ERROR / UNCHECKED: source unavailable")
    monkeypatch.setattr(unanswered.ruling_gate, "check_ruling", lambda **_kwargs: unchecked)
    rc = unanswered.main(["add", "--question", "UNCHECKED"])
    out = capsys.readouterr().out
    assert rc == 0
    row = read_state(state)["rows"][0]
    assert row["ruling_gate"] == "ERROR"
    assert "ERROR / UNCHECKED" in out
    assert "RULING GATE ERROR / UNCHECKED" in page.read_text(encoding="utf-8")


def test_close_stale_reads_differently_from_answered(sandbox, capsys):
    state, page, _project = sandbox
    unanswered.main(["add", "--question", "§1.20?", "--priority", "P2"])
    capsys.readouterr()
    assert unanswered.main(["close", "--id", "U001", "--stale", "--reason", "устарело"]) == 0
    row = read_state(state)["rows"][0]
    assert row["status"] == "stale"
    assert "неактуален" in page.read_text(encoding="utf-8")

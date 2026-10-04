"""The unanswered register: durable state, safe IDs, safe Markdown cells, the owner's language."""
from __future__ import annotations

import argparse
import json
import threading

import pytest

import owner_text
import unanswered


@pytest.fixture
def register(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(unanswered, "STATE", tmp_path / "state" / "UNANSWERED_STATE.json")
    monkeypatch.setattr(unanswered, "PAGE", tmp_path / "UNANSWERED.md")
    monkeypatch.setattr(unanswered, "PROJECT", project)
    return tmp_path


def _add(question: str, **extra) -> int:
    values = dict(question=question, blocks="", cost="", priority="P2", at=None, session=None,
                  supersedes=False, supersedes_reason=None)
    values.update(extra)
    return unanswered.command_add(argparse.Namespace(**values))


def test_ids_never_reuse_a_number_after_rows_are_removed_by_hand(register):
    assert _add("one") == 0 and _add("two") == 0
    state = unanswered.load()
    state["rows"] = [r for r in state["rows"] if r["id"] == "U002"]  # U001 deleted by hand
    unanswered.save(state)
    assert _add("three") == 0
    assert [r["id"] for r in unanswered.load()["rows"]] == ["U002", "U003"]


def test_a_corrupt_register_is_a_loud_error_and_is_left_alone(register, capsys):
    unanswered.STATE.parent.mkdir(parents=True)
    unanswered.STATE.write_text('{"rows": [', encoding="utf-8")
    assert unanswered.main(["list"]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert unanswered.STATE.read_text(encoding="utf-8") == '{"rows": ['


def test_pipes_and_newlines_cannot_break_the_owner_table(register):
    assert _add("a | b\nsecond line", blocks="x | y", cost="line1\nline2") == 0
    page = unanswered.PAGE.read_text(encoding="utf-8")
    data_row = next(line for line in page.splitlines() if line.startswith("| 🟡"))
    assert data_row.count("|") - data_row.count("\\|") == 7  # 6 columns, 7 separators
    assert "a \\| b second line" in data_row


def test_concurrent_adds_get_distinct_ids(register):
    threads = [threading.Thread(target=_add, args=(f"q{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = [r["id"] for r in unanswered.load()["rows"]]
    assert len(ids) == len(set(ids)) == 8


def test_the_page_follows_the_owner_language(register, monkeypatch):
    monkeypatch.setattr(unanswered.paths, "owner_language", lambda: "en")
    assert _add("Which colour for the hero?") == 0
    page = unanswered.PAGE.read_text(encoding="utf-8")
    assert "Waiting for you" in page and "Ждут тебя" not in page
    monkeypatch.setattr(unanswered.paths, "owner_language", lambda: "ru")
    unanswered.render()
    assert "Ждут тебя" in unanswered.PAGE.read_text(encoding="utf-8")


def test_both_languages_define_exactly_the_same_owner_strings():
    assert owner_text.TEXT["ru"].keys() == owner_text.TEXT["en"].keys()


def test_the_state_file_is_valid_json_after_every_write(register):
    for index in range(3):
        _add(f"q{index}")
        json.loads(unanswered.STATE.read_text(encoding="utf-8"))
    assert not list(unanswered.STATE.parent.glob("*.tmp"))

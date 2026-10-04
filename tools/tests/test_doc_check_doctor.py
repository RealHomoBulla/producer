"""doc_check finds dead references in the handbook; doctor says what a machine is missing."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import doc_check

TOOLS = Path(__file__).resolve().parents[1]


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_a_clean_tree_has_no_dead_references(tmp_path):
    root = _tree(tmp_path, {
        "AGENTS.md": "Run `python tools/digest.py list`; see `work/agents/orca/ROUTING.md` §Credentials.\n",
        "tools/digest.py": "",
        "work/agents/orca/ROUTING.md": "# Routing\n\n## Credentials\n\ntext\n\n## Probes and availability\n",
    })
    assert doc_check.check(root) == []


def test_a_missing_tool_file_and_heading_are_each_reported(tmp_path):
    root = _tree(tmp_path, {
        "AGENTS.md": ("Run `python tools/gone.py`; read `work/agents/orca/NOPE.md`; "
                      "see ROUTING.md §Keys.\n"),
        "work/agents/orca/ROUTING.md": "# Routing\n\n## Credentials\n",
    })
    problems = "\n".join(doc_check.check(root))
    assert "tools/gone.py does not exist" in problems
    assert "work/agents/orca/NOPE.md" in problems
    assert "ROUTING.md §Keys" in problems


def test_placeholders_runtime_paths_and_history_are_not_checked(tmp_path):
    root = _tree(tmp_path, {
        "AGENTS.md": ("Write `work/agents/reports/<topic>/YYYY_MM_DD_X.md`, `.runtime/briefs/x.md`, "
                      "`work/agents/**/*.md` and `tools/*.py`.\n"),
        "work/agents/reports/2026_01_01/OLD.md": "Ran `python tools/removed.py` back then.\n",
    })
    assert doc_check.check(root) == []


@pytest.mark.parametrize("pointer, headings, ok", [
    ("0a. Accounts", ["0a. accounts - on a new machine"], True),
    ("Probes", ["probes and availability"], True),
    ("Keys", ["credentials"], False),
    ("Roster gets the owner's roster", ["roster", "other"], True),
    ("7", ["7. canonical write-back"], True),
    ("9", ["7. canonical write-back"], False),
])
def test_heading_pointers_match_by_number_or_first_words(pointer, headings, ok):
    assert doc_check.heading_matches(pointer, headings) is ok


def test_the_shipped_handbook_has_no_dead_references():
    """The real tree: this is the gate that keeps README/AGENTS/START_PROMPT honest."""
    assert doc_check.check() == []


# ------------------------------------------------------------------ doctor
def _producer():
    spec = importlib.util.spec_from_file_location("producer_doctor_under_test", TOOLS / "producer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_doctor_reports_a_broken_config_as_fail_with_a_fix(tmp_path, monkeypatch, capsys):
    producer = _producer()
    monkeypatch.setattr(producer, "PROJECT", tmp_path)
    (tmp_path / "producer.toml").write_text("[project\nname = ", encoding="utf-8")
    rows = producer.doctor_checks()
    fail = [row for row in rows if row[0] == "FAIL" and "producer.toml" in row[1]]
    assert fail and fail[0][2]
    assert producer.command_doctor(None) == 1
    assert "NOT READY" in capsys.readouterr().out


def test_doctor_is_read_only_and_every_problem_row_names_its_fix(tmp_path, monkeypatch):
    producer = _producer()
    monkeypatch.setattr(producer, "PROJECT", tmp_path)
    (tmp_path / "producer.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    rows = producer.doctor_checks()
    assert all(fix for level, _what, fix in rows if level in ("FAIL", "WARN"))
    assert sorted(p.name for p in tmp_path.rglob("*")) == before  # it never writes anything

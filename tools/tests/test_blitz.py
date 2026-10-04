"""tools/blitz.py on temporary registers: next numbers, the cross-check verdicts, the table, the status."""
from __future__ import annotations

from pathlib import Path

import pytest

import blitz

TEMPLATE = """# BLITZ <N> — ready questions (<date>)

## Сверка прошлых блицев
| блиц · вопрос | ответ | где сейчас |
|---|---|---|

### Q1 · <short title>
**Problem:** text
"""

ACTIVE_1 = """# BLITZ 1 — active

## Q1 — asked 2026-10-01T10:00Z (Hero colour)
Options: a / b
Answer (2026-10-01T10:05Z): «a, but darker»

## Q2 — asked 2026-10-01T10:06Z (Footer links)
Options: a / b
Answer (2026-10-01T10:08Z): «b, in the footer | small»

## Q3 — asked 2026-10-01T10:09Z (Cookie banner)
Options: a / b
Answer (2026-10-01T10:11Z): «a»

## Q4 — asked 2026-10-01T10:12Z (Newsletter box)
Options: a / b

## Closure
All answers recorded.
"""

TODO = """# TODO

- [ ] FOOTER-LINKS-20261002 add the legal links to the footer (BLITZ1-Q2)
- [x] COOKIE-20261002 cookie banner shipped — Cookie banner ✅ (blitz 1 Q3)
"""

OPEN = """# OPEN

## §3 — Hero colour (2026-10-01)
- **Question:** which colour?
- **Ruling:** «a, but darker» (2026-10-02) → routed to TODO `HERO-COLOUR-20261002`.

## §4 — Newsletter box (2026-10-03)
- **Ruling:** «<his words verbatim>» (<date>)
"""


@pytest.fixture
def registers(tmp_path):
    folder = tmp_path / "registers"
    folder.mkdir()
    (folder / "BLITZ_READY_TEMPLATE.md").write_text(TEMPLATE, encoding="utf-8")
    (folder / "BLITZ1_ACTIVE.md").write_text(ACTIVE_1, encoding="utf-8")
    (folder / "TODO.md").write_text(TODO, encoding="utf-8")
    (folder / "OPEN.md").write_text(OPEN, encoding="utf-8")
    return folder


def _run(registers, *argv):
    return blitz.main(["--registers", str(registers), *argv])


def test_questions_and_answers_are_parsed_from_the_shipped_heading_styles(registers):
    questions = blitz.parse_questions(registers / "BLITZ1_ACTIVE.md", 1)
    assert [(q.number, q.title, bool(q.answer)) for q in questions] == [
        (1, "Hero colour", True), (2, "Footer links", True), (3, "Cookie banner", True), (4, "Newsletter box", False)]
    ready = registers / "BLITZ2_READY.md"
    ready.write_text("### Q1 · Colour of the buttons\n### Q2 · Font\n", encoding="utf-8")
    assert [q.title for q in blitz.parse_questions(ready, 2)] == ["Colour of the buttons", "Font"]


def test_each_answer_is_traced_to_done_queued_or_missing(registers):
    rows, unanswered = blitz.crosscheck(registers, 1, [])
    verdicts = {q.number: (v, where) for q, v, where in rows}
    assert verdicts[1][0] == "done" and verdicts[1][1].startswith("OPEN.md")  # a real ruling in OPEN
    assert verdicts[2][0] == "queued" and verdicts[2][1].startswith("TODO.md")  # an open TODO row
    assert verdicts[3][0] == "done" and verdicts[3][1].startswith("TODO.md")  # a closed TODO row
    assert [q.number for q in unanswered] == [4]


def test_an_answer_found_nowhere_is_missing_and_a_placeholder_ruling_is_not_a_trace(registers):
    (registers / "TODO.md").write_text("# TODO\n", encoding="utf-8")
    (registers / "OPEN.md").write_text(OPEN.split("## §4")[0].replace("**Ruling:**", "**Note:**") + "## §4 — Newsletter box\n"
                                       "- **Ruling:** «<his words verbatim>»\n", encoding="utf-8")
    rows, _ = blitz.crosscheck(registers, 1, [])
    verdicts = {q.number: v for q, v, _w in rows}
    assert verdicts[3] == "MISSING" and verdicts[2] == "MISSING"
    assert verdicts[1] == "queued"  # mentioned in OPEN but not ruled


def test_the_brief_counts_as_where_an_answer_went(registers, tmp_path):
    brief = tmp_path / "BRIEF.md"
    brief.write_text("# Brief\n\n## 9. Decisions\n- 2026-10-02 Cookie banner: a (blitz 1 Q3)\n", encoding="utf-8")
    (registers / "TODO.md").write_text("# TODO\n", encoding="utf-8")
    rows, _ = blitz.crosscheck(registers, 1, [brief])
    assert {q.number: v for q, v, _w in rows}[3] == "done"


def test_new_creates_the_next_ready_file_with_the_crosscheck_table(registers, capsys):
    assert _run(registers, "new") == 0
    ready = registers / "BLITZ2_READY.md"
    assert ready.exists() and not (registers / "BLITZ1_READY.md").exists()
    text = ready.read_text(encoding="utf-8")
    assert text.startswith("# BLITZ 2 — ready questions (20")
    assert "| BLITZ1 Q1 · Hero colour |" in text and "✅ done" in text
    assert "| BLITZ1 Q2 · Footer links |" in text and "🟡 queued" in text
    assert "b, in the footer \\| small" in text  # a pipe in an answer cannot break the table
    assert "⏳ unanswered" in text
    assert text.index("BLITZ1 Q1") < text.index("### Q1")  # the table sits above the questions


def test_new_never_overwrites_and_numbers_continue(registers, capsys):
    assert _run(registers, "new") == 0
    assert blitz.next_number(registers) == 3
    assert _run(registers, "new") == 0  # BLITZ3 now exists beside BLITZ2
    assert (registers / "BLITZ3_READY.md").exists() and blitz.next_number(registers) == 4
    code = _run(registers, "new", "--number", "2")
    assert code == 1 and "already exists" in capsys.readouterr().err


def test_new_exits_nonzero_when_an_answer_is_missing(registers, capsys):
    (registers / "TODO.md").write_text("# TODO\n", encoding="utf-8")
    (registers / "OPEN.md").write_text("# OPEN\n", encoding="utf-8")
    assert _run(registers, "new") == 1
    out = capsys.readouterr().out
    assert "MISSING: BLITZ1 Q1" in out and "before the next blitz" in out
    assert "🔴 MISSING" in (registers / "BLITZ2_READY.md").read_text(encoding="utf-8")


def test_crosscheck_defaults_to_the_blitz_before_the_newest_ready_and_can_write(registers, capsys):
    (registers / "BLITZ2_READY.md").write_text(TEMPLATE.replace("<N>", "2").replace("<date>", "x"), encoding="utf-8")
    code = _run(registers, "crosscheck", "--write")
    out = capsys.readouterr().out
    assert code == 0 and "BLITZ1: 3 answered, 0 MISSING, 1 unanswered" in out
    assert "table written into BLITZ2_READY.md" in out
    assert "✅ done" in (registers / "BLITZ2_READY.md").read_text(encoding="utf-8")


def test_crosscheck_with_no_active_file_is_a_clean_no_op(tmp_path, capsys):
    folder = tmp_path / "r"
    folder.mkdir()
    assert _run(folder, "crosscheck") == 0
    assert "nothing to cross-check" in capsys.readouterr().out


def test_status_counts_open_questions_and_names_the_active_blitz(registers, capsys):
    (registers / "BLITZ2_READY.md").write_text("### Q1 · One\n### Q2 · Two\n### Q3 · Three\n", encoding="utf-8")
    (registers / "BLITZ2_ACTIVE.md").write_text("## Q1 — asked x (One)\nAnswer (y): «yes»\n", encoding="utf-8")
    assert _run(registers, "status") == 0
    out = capsys.readouterr().out
    assert "BLITZ2_READY: 2 open question(s) of 3" in out
    assert "active blitz: BLITZ2" in out  # BLITZ1 has a closure section, BLITZ2 does not


def test_status_on_an_empty_folder_says_how_to_start(tmp_path, capsys):
    folder = tmp_path / "r"
    folder.mkdir()
    assert _run(folder, "status") == 0
    assert "blitz.py new" in capsys.readouterr().out

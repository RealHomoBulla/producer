"""The ruling gate must read the register shape THIS skeleton ships (work/agents/registers/OPEN.md):
integer `§N` sections, `- §N · status · title` index lines and an English `**Ruling:**` body — and
the Russian equivalents — plus answers the owner already submitted in a blitz file. A parser that
silently misses its own source format would grant permission to ask a question he already answered.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import ruling_gate

TEMPLATE_OPEN = """# OPEN — the only open-decisions register

## Live index (one line each: §N · status · short title · recommendation)

- §3 · ✅ ruled · Hero colour · blue
- §4 · 🔴 open · Pricing page layout · a

---

## §3 — Hero colour (2026-10-01)
- **Question:** which colour?
- **Recommendation:** blue — contrast.
- **Ruling:** «blue, but darker» (2026-10-02) → routed to TODO `HERO-COLOUR-20261002`.

## §4 — Pricing page layout (2026-10-03)
- **Question:** one column or three?
- **Recommendation:** a — simpler.
- **Ruling:** «<his words verbatim>» (<date>) → routed to TODO `<ROW-ID>`.
"""

RUSSIAN_OPEN = """# OPEN

## Живой индекс

- §7 · ✅ решено · Цвет кнопки
- §8 · 🔴 открыто · Шрифт

---

## §7 — Цвет кнопки (2026-10-01)
- **Рулинг владельца:** «зелёная» (2026-10-02).

## §8 — Шрифт (2026-10-03)
- **Вопрос:** какой?
"""


def _repo(tmp_path: Path, open_text: str, **extra: str) -> Path:
    registers = tmp_path / "work" / "agents" / "registers"
    registers.mkdir(parents=True)
    (registers / "OPEN.md").write_text(open_text, encoding="utf-8")
    for name, body in extra.items():
        (registers / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_a_closed_template_decision_is_found_so_it_is_not_asked_again(tmp_path):
    repo = _repo(tmp_path, TEMPLATE_OPEN)
    result = ruling_gate.check_ruling(section="§3", repo_path=repo)
    assert result.status == "FOUND" and result.permission_to_ask is False
    assert any("blue, but darker" in match.answer for match in result.matches)


def test_an_open_template_decision_is_not_found_and_may_be_asked(tmp_path):
    repo = _repo(tmp_path, TEMPLATE_OPEN)
    result = ruling_gate.check_ruling(section="§4", repo_path=repo)
    assert result.status == "NOT_FOUND" and result.permission_to_ask is True  # placeholder ≠ ruling


def test_a_text_query_naming_the_section_finds_the_ruling(tmp_path):
    repo = _repo(tmp_path, TEMPLATE_OPEN)
    result = ruling_gate.check_ruling(text="Should we revisit §3 about the colour?", repo_path=repo)
    assert result.status == "FOUND"


def test_integer_section_numbers_do_not_collide(tmp_path):
    repo = _repo(tmp_path, TEMPLATE_OPEN)
    assert ruling_gate.check_ruling(section="§30", repo_path=repo).status == "NOT_FOUND"
    assert ruling_gate.check_ruling(section="§1", repo_path=repo).status == "NOT_FOUND"


def test_the_russian_ruling_label_still_works(tmp_path):
    repo = _repo(tmp_path, RUSSIAN_OPEN)
    assert ruling_gate.check_ruling(section="§7", repo_path=repo).status == "FOUND"
    assert ruling_gate.check_ruling(section="§8", repo_path=repo).status == "NOT_FOUND"


def test_a_submitted_blitz_answer_counts_as_a_ruling(tmp_path):
    blitz = """# BLITZ 2 — active

### Q1 · Footer links (§12)
**Problem:** where do the legal links go?
**Answer:** b — «in the footer, small»

### Q2 · Newsletter (§13)
**Answer:**
"""
    repo = _repo(tmp_path, TEMPLATE_OPEN, **{"BLITZ2_ACTIVE.md": blitz})
    found = ruling_gate.check_ruling(section="§12", repo_path=repo)
    assert found.status == "FOUND" and "in the footer" in found.matches[0].answer
    assert "submitted answer" in found.matches[0].source
    assert ruling_gate.check_ruling(section="§13", repo_path=repo).status == "NOT_FOUND"  # unanswered


def test_report_wording_follows_the_owner_language(tmp_path, monkeypatch):
    repo = _repo(tmp_path, TEMPLATE_OPEN)
    result = ruling_gate.check_ruling(section="§3", repo_path=repo)
    monkeypatch.setattr(ruling_gate.paths, "owner_language", lambda: "en")
    english = ruling_gate.format_report(result)
    assert "RULINGS FOUND" in english and "НАЙДЕННЫЕ" not in english
    monkeypatch.setattr(ruling_gate.paths, "owner_language", lambda: "ru")
    assert "НАЙДЕННЫЕ РУЛИНГИ" in ruling_gate.format_report(result)


def test_a_missing_open_register_is_fail_closed(tmp_path):
    result = ruling_gate.check_ruling(section="§3", repo_path=tmp_path)
    assert result.status == "ERROR" and result.permission_to_ask is False


@pytest.mark.parametrize("text", ["blue", "", "<his words verbatim>"])
def test_placeholder_or_empty_ruling_text_is_never_a_ruling(text):
    line = f"- **Ruling:** {text}"
    content = ruling_gate.label_content(line, ruling_gate._RULING_LABELS)
    assert content is None or content == "blue"


def test_the_blitz_brief_answer_format_counts_too(tmp_path):
    blitz = """# BLITZ 3

## Q1 — asked 2026-10-04T10:00Z (Cookie banner, §20)
Options: a / b
Answer (2026-10-04T10:05Z): «a, short text»

## Q2 — asked 2026-10-04T10:06Z (Font, §21)
Options: a / b
"""
    repo = _repo(tmp_path, TEMPLATE_OPEN, **{"BLITZ3_ACTIVE.md": blitz})
    assert ruling_gate.check_ruling(section="§20", repo_path=repo).status == "FOUND"
    assert ruling_gate.check_ruling(section="§21", repo_path=repo).status == "NOT_FOUND"

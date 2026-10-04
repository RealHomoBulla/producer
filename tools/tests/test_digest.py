"""Regression tests for the owner digest lifecycle, product filter, language and timezone.

Every test redirects PAGE, archive, STATE and PROJECT into ``tmp_path``. Importing the tool
performs no writes, so neither the unread owner page nor live digest state is touched.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DEFAULT_SCRIPT = Path(__file__).resolve().parents[1] / "digest.py"
SCRIPT = Path(os.environ.get("DIGEST_SCRIPT_UNDER_TEST", DEFAULT_SCRIPT))
SPEC = importlib.util.spec_from_file_location("digest_under_test", SCRIPT)
assert SPEC and SPEC.loader
DIGEST = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DIGEST
SPEC.loader.exec_module(DIGEST)

# Five non-products, one genuine deliverable, shaped like a small web project's agent tree.
BRIEF = "work/agents/reports/reviews/CR-20260915-c4e4b2618.md"
VERDICT = "work/agents/reports/reviews/CR-20260915-eed0a78e9_REVIEW.md"
LIVING_PAGE = "work/agents/knowledge/frontend/LAYOUT_GRID.md"
OPS_DOC = "work/agents/orca/ROUTING.md"
OPS_BRIEF = "work/agents/orca/briefs/2026_09_13/ANIMATION_PASS.md"
DELIVERABLE = "work/agents/reports/2026_09_15_HERO_ANIMATION_SPEC.md"
DATED_REPORTS_REPORT = "work/agents/reports/2026_09_13/CONTRAST_AUDIT.md"
DATED_KNOWLEDGE_REPORT = "work/agents/knowledge/2026_09_15_SEO_REFRESH.md"
SERVICE_STATE = "work/agents/state/HANDOVER.md"
SERVICE_REGISTERS = "work/agents/registers/OPEN.md"
REPORTS_README = "work/agents/reports/README.md"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(DIGEST, "PAGE", tmp_path / "DIGEST.md")
    monkeypatch.setattr(DIGEST, "archive_dir", lambda: tmp_path / "archive" / "digests")
    monkeypatch.setattr(DIGEST, "STATE", tmp_path / "digest_state.json")
    monkeypatch.setattr(DIGEST, "PROJECT", tmp_path)
    return tmp_path


@pytest.fixture
def english(monkeypatch):
    monkeypatch.setattr(DIGEST.paths, "owner_language", lambda: "en")


@pytest.fixture
def russian(monkeypatch):
    monkeypatch.setattr(DIGEST.paths, "owner_language", lambda: "ru")


def _tree(tmp_path, monkeypatch):
    monkeypatch.setattr(DIGEST, "PROJECT", tmp_path)
    for rel in (BRIEF, VERDICT, LIVING_PAGE, OPS_DOC, OPS_BRIEF, DELIVERABLE,
                DATED_REPORTS_REPORT, DATED_KNOWLEDGE_REPORT, SERVICE_STATE, SERVICE_REGISTERS,
                REPORTS_README):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# product\n\nbody\n", encoding="utf-8")
    return tmp_path


def _product(path: Path, mtime: float, lines: int = 40):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line {i}" for i in range(lines)), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return {"path": path.relative_to(DIGEST.PROJECT).as_posix(), "mtime": mtime, "lines": lines}


# ------------------------------------------------------------------ product filter
def test_digest_filter_excludes_non_products(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    prods = {r["path"] for r in DIGEST.products(30)}
    for p in (BRIEF, VERDICT, LIVING_PAGE, OPS_DOC, OPS_BRIEF, REPORTS_README):
        assert p not in prods, p
    assert DELIVERABLE in prods


def test_dated_reports_stay_listed(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    prods = {r["path"] for r in DIGEST.products(30)}
    assert DATED_REPORTS_REPORT in prods
    assert DATED_KNOWLEDGE_REPORT in prods


def test_service_and_state_files_never_list(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    prods = {r["path"] for r in DIGEST.products(30)}
    assert SERVICE_STATE not in prods
    assert SERVICE_REGISTERS not in prods


def test_is_product_predicate_shapes():
    assert not DIGEST.is_product(BRIEF)
    assert not DIGEST.is_product(VERDICT)
    assert not DIGEST.is_product(LIVING_PAGE)
    assert not DIGEST.is_product(OPS_DOC)
    assert not DIGEST.is_product(REPORTS_README)
    assert DIGEST.is_product(DELIVERABLE)
    assert DIGEST.is_product(DATED_REPORTS_REPORT)
    # Non-agents paths are left watched rather than decided here.
    assert DIGEST.is_product("docs/PIPELINE.md")


def test_an_old_unreported_product_never_drops_out_of_the_list(sandbox, capsys):
    old = _product(sandbox / "work" / "agents" / "reports" / "ancient.md", time.time() - 90 * 86400)
    assert [r["path"] for r in DIGEST.products()] == [old["path"]]  # default: no time filter
    assert DIGEST.products(3) == []  # an explicit window still narrows the view
    assert DIGEST.main(["list"]) == 0
    assert old["path"] in capsys.readouterr().out


# ------------------------------------------------------------------ clear lifecycle
def test_clear_creates_the_archive_and_refuses_to_lose_the_page_when_it_cannot_write(
        sandbox, monkeypatch, capsys):
    DIGEST.PAGE.write_text("# Digest\n\nverdict\n", encoding="utf-8")
    blocker = sandbox / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(DIGEST, "archive_dir", lambda: blocker / "digests")
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [])
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 1
    assert DIGEST.PAGE.exists()  # the unread page stays until the copy is verified
    assert "NOT cleared" in capsys.readouterr().err


def test_first_clear_works_without_a_pre_made_archive_directory(sandbox, monkeypatch):
    DIGEST.PAGE.write_text("# Digest\n\nverdict\n", encoding="utf-8")
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [])
    assert not (sandbox / "archive").exists()
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    assert len(list((sandbox / "archive" / "digests").glob("*_DIGEST.md"))) == 1


def test_clear_never_marks_a_product_written_after_the_last_append(sandbox, monkeypatch, capsys):
    DIGEST.PAGE.write_text("# Digest\n\nreported: work/agents/old_verdict.md\n", encoding="utf-8")
    page_mtime = time.time() - 100
    os.utime(DIGEST.PAGE, (page_mtime, page_mtime))
    covered = _product(sandbox / "work" / "agents" / "old_verdict.md", page_mtime - 50)
    uncovered = _product(sandbox / "work" / "agents" / "verdict_he_never_saw.md", page_mtime + 50)
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [covered, uncovered])
    DIGEST.save({"reported": {}, "covered": {covered["path"]: {"at": "x", "mtime": covered["mtime"]}}})
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    reported = DIGEST.load()["reported"]
    assert covered["path"] in reported
    assert uncovered["path"] not in reported
    assert "1 product(s)" in capsys.readouterr().out


def test_clear_marks_only_what_a_section_covered_not_every_older_product(sandbox, monkeypatch):
    DIGEST.PAGE.write_text("# Digest\n\nsection\n", encoding="utf-8")
    page_mtime = time.time() - 100
    os.utime(DIGEST.PAGE, (page_mtime, page_mtime))
    told = _product(sandbox / "work" / "agents" / "told.md", page_mtime - 60)
    never_told = _product(sandbox / "work" / "agents" / "never_told_but_older.md", page_mtime - 80)
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [told, never_told])
    DIGEST.save({"reported": {}, "covered": {told["path"]: {"at": "x", "mtime": told["mtime"]}}})
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    reported = DIGEST.load()["reported"]
    assert list(reported) == [told["path"]]  # the older, uncovered one still waits to be reported


def test_a_product_edited_after_it_was_covered_stays_unreported(sandbox, monkeypatch):
    DIGEST.PAGE.write_text("# Digest\n\nsection\n", encoding="utf-8")
    page_mtime = time.time() - 100
    os.utime(DIGEST.PAGE, (page_mtime, page_mtime))
    row = _product(sandbox / "work" / "agents" / "report.md", page_mtime - 60)
    DIGEST.save({"reported": {}, "covered": {row["path"]: {"at": "x", "mtime": row["mtime"] - 30}}})
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [row])
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    assert DIGEST.load()["reported"] == {}


def test_clear_still_archives_the_page_rather_than_deleting_it(sandbox, monkeypatch):
    body = "# Digest\n\nverdict and what it means\n"
    DIGEST.PAGE.write_text(body, encoding="utf-8")
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [])
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    assert not DIGEST.PAGE.exists()
    archived = list((sandbox / "archive" / "digests").glob("*_DIGEST.md"))
    assert len(archived) == 1 and archived[0].read_text(encoding="utf-8") == body


def test_clear_on_an_absent_page_is_a_no_op(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [])
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    assert "no digest to clear" in capsys.readouterr().out


def test_clear_preserves_the_first_reported_at(sandbox, monkeypatch):
    DIGEST.PAGE.write_text("# Digest\n\nreported: work/agents/already_reported.md\n", encoding="utf-8")
    page_mtime = time.time() - 100
    os.utime(DIGEST.PAGE, (page_mtime, page_mtime))
    row = _product(sandbox / "work" / "agents" / "already_reported.md", page_mtime - 10, lines=41)
    first_reported_at = "2026-08-25T16:00:00+00:00"
    DIGEST.save({"reported": {row["path"]: {
        "mtime": row["mtime"] - 1, "at": first_reported_at, "lines": 40}}})
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [row])
    assert DIGEST.command_clear(argparse.Namespace(days=0)) == 0
    refreshed = DIGEST.load()["reported"][row["path"]]
    assert refreshed == {"mtime": row["mtime"], "at": first_reported_at, "lines": row["lines"]}


# ------------------------------------------------------------------ append / mark
def test_append_uses_one_monotonic_header(sandbox, monkeypatch, russian):
    DIGEST.PAGE.write_text("# Digest\n\n---\n\n### Добавлено 2026-10-25 02:59\n\nстарый вердикт\n",
                           encoding="utf-8")
    rollback = datetime(2026, 10, 25, 2, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(DIGEST, "_owner_now", lambda: rollback.astimezone(DIGEST.OWNER_TIME))
    assert DIGEST.command_append(argparse.Namespace(text="новый вердикт", covers=None, days=0)) == 0
    text = DIGEST.PAGE.read_text(encoding="utf-8")
    assert text.count("### Добавлено") == 2
    before = text
    duplicate = "### Добавлено 2026-10-25 03:00\n\nещё один вердикт"
    assert DIGEST.command_append(argparse.Namespace(text=duplicate, covers=None, days=0)) == 1
    assert DIGEST.PAGE.read_text(encoding="utf-8") == before


def test_mark_requires_digest_text_for_every_product(sandbox, monkeypatch, capsys):
    row = _product(sandbox / "work" / "agents" / "mods" / "verdict.md", time.time())
    monkeypatch.setattr(DIGEST, "products", lambda days=0: [row])
    args = argparse.Namespace(days=0, all=False, path=[row["path"]])
    DIGEST.PAGE.write_text("# Digest\n\n### Verdict\n\nonly a conclusion, no source\n", encoding="utf-8")
    assert DIGEST.command_mark(args) == 1
    assert DIGEST.load() == {"reported": {}}
    assert "REFUSED, no digest section covers product" in capsys.readouterr().out


def test_appended_section_cannot_carry_a_second_header(sandbox, capsys, russian):
    assert DIGEST.command_append(argparse.Namespace(
        text="текст\n### Добавлено 2026-10-04 10:00\nещё", covers=None, days=0)) == 1
    assert DIGEST.command_append(argparse.Namespace(
        text="text\n### Added 2026-10-04 10:00\nmore", covers=None, days=0)) == 1


def test_append_records_the_revision_it_covers(sandbox, russian):
    row = _product(sandbox / "work" / "agents" / "reports" / "r.md", time.time() - 500)
    assert DIGEST.command_append(argparse.Namespace(
        text="отчёт готов", covers=[row["path"]], days=0)) == 0
    assert DIGEST.load()["covered"][row["path"]]["mtime"] == row["mtime"]


# ------------------------------------------------------------------ owner language
def test_short_russian_section_passes(russian):
    assert DIGEST._owner_text_problem("**Страница готова** — проверка 11.10.") is None


def test_latin_section_refused_for_a_russian_owner(russian):
    assert "Russian" in DIGEST._owner_text_problem("Rebuilt the hero section, tests pass.")


def test_an_english_owner_can_write_an_english_digest(sandbox, english, capsys):
    text = "Hero animation shipped; contrast check passes on mobile."
    assert DIGEST._owner_text_problem(text) is None
    assert DIGEST.command_append(argparse.Namespace(text=text, covers=None, days=0)) == 0
    page = DIGEST.PAGE.read_text(encoding="utf-8")
    assert "# Digest" in page and "### Added " in page and text in page
    assert "Добавлено" not in page and "Дайджест" not in page


def test_a_cyrillic_section_is_refused_for_an_english_owner(english):
    assert "Cyrillic" in DIGEST._owner_text_problem("Всё готово, проверено вручную сегодня.")


def test_long_section_refused(russian):
    assert "chars" in DIGEST._owner_text_problem("очень " * 200)


# ------------------------------------------------------------------ timezone
@pytest.mark.parametrize("name, hours", [("UTC+02:00", 2), ("+0530", 5.5), ("GMT-5", -5), ("UTC", 0)])
def test_the_configured_timezone_is_used_not_a_hard_coded_one(name, hours, monkeypatch):
    monkeypatch.setattr(DIGEST.paths, "timezone_name", lambda: name)
    tz, warning = DIGEST.paths.owner_tzinfo()
    assert warning is None
    assert datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(tz).utcoffset() == timedelta(hours=hours)


def test_an_unknown_timezone_falls_back_to_utc_with_a_warning(monkeypatch):
    monkeypatch.setattr(DIGEST.paths, "timezone_name", lambda: "Nowhere/Land")
    tz, warning = DIGEST.paths.owner_tzinfo()
    assert tz is timezone.utc and "UTC" in warning and "tzdata" in warning


def test_the_digest_stamps_in_the_owner_timezone(monkeypatch):
    monkeypatch.setattr(DIGEST, "OWNER_TIME", timezone(timedelta(hours=-3)))
    stamp = DIGEST._owner_datetime(datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
    assert stamp.hour == 9


# ------------------------------------------------------------------ state safety
def test_a_corrupt_state_file_is_reported_not_reset(sandbox, capsys):
    DIGEST.STATE.write_text("{ not json", encoding="utf-8")
    assert DIGEST.main(["list"]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert DIGEST.STATE.read_text(encoding="utf-8") == "{ not json"  # left for recovery


def test_state_writes_are_atomic_and_leave_no_temp_file(sandbox):
    DIGEST.save({"reported": {"a": {"mtime": 1}}})
    assert DIGEST.load()["reported"]["a"]["mtime"] == 1
    assert not list(sandbox.glob("*.tmp"))

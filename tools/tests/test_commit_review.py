"""The cross-model commit review gate: attribution, the symmetric reviewer pick, and the registers.

Tests redirect every written artefact into ``tmp_path``. Git itself is read from the real
repository unless a test monkeypatches ``commit_review.commits``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import commit_review  # noqa: E402


@pytest.fixture(autouse=True)
def builtin_reviewer_families(monkeypatch):
    """After `setup.py` a project has its own `[review] reviewers`; these tests exercise the built-in list."""
    monkeypatch.setattr(commit_review.paths, "review_reviewers", lambda: ())


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(commit_review, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(commit_review, "PAGE", tmp_path / "COMMIT_JOURNAL.md")
    monkeypatch.setattr(commit_review, "POLISH", tmp_path / "POLISHING_TODO.md")
    monkeypatch.setattr(commit_review, "PROMPTS", tmp_path / "reports" / "reviews")
    reports = tmp_path / "REPORTS.md"
    reports.write_text("# REPORTS\n\n## Live register\n\n"
                       "| path | date | domain | status | destination |\n|---|---|---|---|---|\n"
                       "\n## Archived ledger\n", encoding="utf-8")
    monkeypatch.setattr(commit_review, "REPORTS", reports)
    return tmp_path


# ------------------------------------------------------------------ attribution
def test_a_trailer_maps_to_a_family_not_to_a_model():
    assert commit_review.family_of("Claude Opus 5 <noreply@anthropic.com>") == "claude-opus"
    assert commit_review.family_of("Claude Sonnet 5 <noreply@anthropic.com>") == "claude-sonnet"
    assert commit_review.family_of("Codex gpt-5.6-sol") == "codex-sol"
    assert commit_review.family_of("gpt-5.6-luna") == "codex-luna"
    assert commit_review.family_of("Gemini 3 Flash") == "gemini"
    assert commit_review.family_of("DeepSeek v4") == "deepseek"
    assert commit_review.family_of("") == "human"


def test_grouping_by_family_not_by_model():
    """Opus and Sonnet are different review families; a Claude trailer is still Claude."""
    assert commit_review.family_of("claude") == "claude-sonnet"


def test_untrailered_agent_message_marker_is_unattributed():
    hit = commit_review.classify_untrailered("abc", "Report: work/agents/economy/x.md", "")
    assert hit is not None and hit[0] == "unattributed"
    assert commit_review.classify_untrailered("abc", "an ordinary message", "") is None


# ------------------------------------------------------------------ reviewer pick
def test_the_reviewer_is_never_the_family_that_wrote_the_batch():
    luca = [{"family": "codex-luna"}] * 10
    family, _why = commit_review.pick_reviewer(luca)
    assert family == "claude-sonnet"
    claude = [{"family": "claude-sonnet"}] * 10
    family, _why = commit_review.pick_reviewer(claude)
    assert family == "codex-luna"


def test_a_mixed_batch_prefers_independence_then_the_smallest_stake():
    mixed = [{"family": "claude-sonnet"}] * 8 + [{"family": "codex-luna"}] * 2
    family, why = commit_review.pick_reviewer(mixed, override=None)
    assert family == "gemini", why
    all_four = ([{"family": "claude-sonnet"}] * 5 + [{"family": "codex-luna"}] * 3
                + [{"family": "gemini"}] * 3 + [{"family": "deepseek"}] * 1)
    family, why = commit_review.pick_reviewer(all_four)  # the gate is never skipped
    assert family == "deepseek" and why.startswith("SAME-FAMILY (weaker)")
    with pytest.raises(commit_review.NoIndependentReviewer, match="UNMET"):
        commit_review.pick_reviewer(all_four, require_independent=True)


def test_all_independent_families_are_considered_before_any_author_family():
    rows = [{"family": "claude-sonnet"}] * 10
    assert commit_review.independent_reviewer_families(rows) == ("codex-luna", "gemini", "deepseek")
    with pytest.raises(ValueError, match="authored"):
        commit_review.pick_reviewer(rows, override="claude-sonnet")


def test_verdict_refuses_a_concrete_same_family_reviewer(sandbox, capsys):
    batch_id = "CR-same-family-guard"
    commit_review.save_state({"batches": [{
        "id": batch_id, "status": "pending", "reviewer": "codex-luna",
        "author_families": ["claude-sonnet"], "authors": ["Claude Sonnet 5"],
        "commits": ["a" * 40], "shorts": ["aaaaaaa"], "findings": []}]})
    args = argparse.Namespace(batch=batch_id, status="approved", reviewer="claude-sonnet-5",
                              finding=None, fixed_by=None, note="refuse", force=True)
    assert commit_review._reviewer_family(args.reviewer) == "claude-sonnet"
    assert commit_review.command_verdict(args) == 1
    assert "same-family" in capsys.readouterr().out
    assert commit_review.load_state()["batches"][0]["status"] == "pending"


def test_failed_family_is_recorded_then_falls_back_without_reusing_an_author(sandbox):
    commit_review.save_state({"batches": [{
        "id": "CR-fallback", "status": "pending", "reviewer": "codex-luna",
        "authors": ["Claude Sonnet 5"], "author_families": ["claude-sonnet"],
        "commits": ["a"], "shorts": ["aaaaaaa"], "findings": []}]})
    assert commit_review.fallback_after_launch_failure("CR-fallback", "rate limit") == "gemini"
    row = commit_review.load_state()["batches"][0]
    assert row["reviewer"] == "gemini"
    assert row["launch_failures"][-1]["family"] == "codex-luna"
    assert row["reviewer"] not in row["author_families"]


# ------------------------------------------------------------------ queue / baseline
def test_the_owners_own_commits_are_counted_but_never_queued(sandbox, monkeypatch):
    rows = [
        {"sha": "a" * 40, "short": "aaaaaaa", "at": "2026-08-25T10:00:00Z", "author": "owner",
         "subject": "his own commit", "model": "(none)", "family": "human"},
        {"sha": "b" * 40, "short": "bbbbbbb", "at": "2026-08-25T11:00:00Z", "author": "owner",
         "subject": "an agent commit", "model": "Claude Opus 5", "family": "claude-opus"},
    ]
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: rows)
    waiting = commit_review.unreviewed({"batches": []})
    assert [w["short"] for w in waiting] == ["bbbbbbb"]
    commit_review.render_page()
    page = commit_review.PAGE.read_text(encoding="utf-8")
    assert "aaaaaaa" in page


def test_the_baseline_is_a_stated_decision(sandbox, monkeypatch):
    rows = [{"sha": c * 40, "short": c * 7, "at": "2026-08-25T10:00:00Z", "author": "owner",
             "subject": f"commit {c}", "model": "Claude Opus 5", "family": "claude-opus"}
            for c in "abcd"]
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: rows)
    state = {"batches": [], "baseline": {"sha": "b" * 40, "short": "bbbbbbb",
                                         "at": "2026-08-25T13:00:00Z", "why": "starts here"}}
    commit_review.save_state(state)
    assert [w["short"] for w in commit_review.unreviewed(state)] == ["ccccccc", "ddddddd"]
    commit_review.render_page()
    assert "Baseline" in commit_review.PAGE.read_text(encoding="utf-8")


# ------------------------------------------------------------------ registers
def test_findings_land_in_a_register_and_approval_does_not(sandbox, monkeypatch):
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: [])
    commit_review.save_state({"batches": [
        {"id": "CR-TEST-1", "opened_at": "x", "commits": ["a" * 40], "shorts": ["aaaaaaa"],
         "authors": ["Claude Opus 5"], "author_families": ["claude-opus"], "reviewer": "codex-luna",
         "reviewer_why": "test", "status": "pending", "findings": []}]})
    commit_review.command_verdict(argparse.Namespace(
        batch="CR-TEST-1", status="findings", reviewer="gpt-5.6-sol",
        finding=["LIVE — tools/x.py:10 claims a measurement the diff does not support"],
        fixed_by=None, note=None, force=True))
    register = commit_review.POLISH.read_text(encoding="utf-8")
    assert "gpt-5.6-sol" in register and "tools/x.py:10" in register


def test_blank_register_is_created_with_header(sandbox, monkeypatch):
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: [])
    commit_review.save_state({"batches": [
        {"id": "CR-TEST-2", "opened_at": "x", "commits": ["b" * 40], "shorts": ["bbbbbbb"],
         "authors": ["Claude Opus 5"], "author_families": ["claude-opus"], "reviewer": "codex-luna",
         "reviewer_why": "test", "status": "pending", "findings": []}]})
    commit_review.command_verdict(argparse.Namespace(
        batch="CR-TEST-2", status="findings", reviewer="gpt-5.6-sol",
        finding=["LIVE — tools/y.py:3 broken"], fixed_by=None, note=None, force=True))
    assert "Polishing TODO" in commit_review.POLISH.read_text(encoding="utf-8")


# ------------------------------------------------------------------ verdict parser
def test_verdict_parser_reads_plain_findings_and_stops_at_clean_label(tmp_path):
    doc = tmp_path / "CR-x_REVIEW.md"
    doc.write_text(
        "VERDICT: findings\n\nFINDINGS\n"
        "LIVE — tools/a.py:1 broken\n"
        "LIVE — tools/b.py:2 also broken\n"
        "CHECKED AND CLEAN (do not redo)\n"
        "range verified\n", encoding="utf-8")
    verdict, findings = commit_review._parse_review_artifact(doc)
    assert verdict == "findings"
    assert findings == ["LIVE — tools/a.py:1 broken", "LIVE — tools/b.py:2 also broken"]


def test_verdict_parser_handles_empty_block(tmp_path):
    doc = tmp_path / "CR-y_REVIEW.md"
    doc.write_text("VERDICT: approved\n\nFINDINGS\nNone. No live defect.\n", encoding="utf-8")
    verdict, findings = commit_review._parse_review_artifact(doc)
    assert verdict == "approved" and findings == []


def test_verdict_refuses_approved_with_live_finding(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: [])
    commit_review.save_state({"batches": [
        {"id": "CR-TEST-3", "opened_at": "x", "commits": ["c" * 40], "shorts": ["ccccccc"],
         "authors": ["Claude Opus 5"], "author_families": ["claude-opus"], "reviewer": "codex-luna",
         "reviewer_why": "test", "status": "pending", "findings": []}]})
    rc = commit_review.command_verdict(argparse.Namespace(
        batch="CR-TEST-3", status="approved", reviewer="gpt-5.6-sol",
        finding=["LIVE — tools/z.py:9 real defect"], fixed_by=None, note=None, force=True))
    assert rc == 1
    assert "refusing approved" in capsys.readouterr().out


def test_registered_reports_row_is_idempotent(sandbox):
    path = commit_review.PROMPTS / "CR-abc.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("brief", encoding="utf-8")
    assert commit_review._register_report(path, date="2026-01-01", domain="d", status="s",
                                          destination="x") == "added"
    assert commit_review._register_report(path, date="2026-01-01", domain="d", status="s",
                                          destination="x") == "exists"


# ------------------------------------------------------------------ T10: gate, hook, registers
def _mixed_rows():
    return [{"sha": f"{i:040d}", "short": f"{i:07d}", "family": fam, "model": fam, "at": "2026-01-01T00:00:00",
             "subject": "x"} for i, fam in enumerate(["claude-sonnet", "codex-luna", "gemini", "deepseek"] * 3)]


def test_open_falls_back_to_a_same_family_fresh_session_review_and_marks_it(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(commit_review, "unreviewed", lambda state: _mixed_rows())
    monkeypatch.setattr(commit_review, "_range", lambda rows: "first..last")
    monkeypatch.setattr(commit_review, "PROJECT", sandbox)
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: [])
    args = argparse.Namespace(size=10, force=False, reviewer=None, require_independent=False)
    assert commit_review.command_open(args) == 0
    out = capsys.readouterr().out
    assert "SAME-FAMILY (weaker)" in out and "FRESH session" in out
    batch = commit_review.load_state()["batches"][0]
    assert batch["same_family"] is True and batch["status"] == "pending"
    brief = (commit_review.PROMPTS / f"{batch['id']}.md").read_text(encoding="utf-8")
    assert "Same-family review (weaker)" in brief and "FRESH session" in brief


def test_require_independent_refuses_instead_of_falling_back(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(commit_review, "unreviewed", lambda state: _mixed_rows())
    args = argparse.Namespace(size=10, force=False, reviewer=None, require_independent=True)
    assert commit_review.command_open(args) == 1
    assert "UNMET" in capsys.readouterr().out
    assert commit_review.load_state()["batches"] == []


def test_the_journal_and_the_verdict_mark_a_same_family_review(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(commit_review, "commits", lambda limit=400: [])
    commit_review.PROMPTS.mkdir(parents=True, exist_ok=True)
    (commit_review.PROMPTS / "CR-weak_REVIEW.md").write_text(
        "VERDICT: approved\n\nFINDINGS\nNone.\n", encoding="utf-8")
    commit_review.save_state({"batches": [{
        "id": "CR-weak", "opened_at": "x", "status": "pending", "reviewer": "claude-sonnet",
        "author_families": ["claude-sonnet"], "authors": ["Claude Sonnet"], "same_family": True,
        "same_family_accepted": True, "commits": ["a" * 40], "shorts": ["aaaaaaa"], "findings": []}]})
    args = argparse.Namespace(batch="CR-weak", status="approved", reviewer="claude-sonnet-5-5",
                              finding=None, fixed_by=None, note="fresh session", force=True)
    assert commit_review.command_verdict(args) == 0
    assert "same-family (weaker)" in capsys.readouterr().out
    page = commit_review.PAGE.read_text(encoding="utf-8")
    assert "(same-family, weaker)" in page and "fresh session of" in page
    assert "same-family (weaker) commit review" in commit_review.REPORTS.read_text(encoding="utf-8")


def test_one_subscription_claude_batches_review_each_other_then_fall_back(monkeypatch):
    monkeypatch.setattr(commit_review.paths, "review_reviewers", lambda: ("claude-opus", "claude-sonnet"))
    by_sonnet = [{"family": "claude-sonnet"}] * 10
    by_opus = [{"family": "claude-opus"}] * 10
    both = [{"family": "claude-sonnet"}] * 6 + [{"family": "claude-opus"}] * 4
    assert commit_review.pick_reviewer(by_sonnet)[0] == "claude-opus"
    assert commit_review.pick_reviewer(by_opus)[0] == "claude-sonnet"
    family, why = commit_review.pick_reviewer(both)
    assert family == "claude-opus" and why.startswith("SAME-FAMILY (weaker)")


def test_a_same_family_review_is_allowed_only_when_it_was_accepted_at_open(sandbox):
    commit_review.save_state({"batches": [{
        "id": "CR-same-accepted", "status": "pending", "reviewer": "deepseek",
        "author_families": ["deepseek", "claude-sonnet"], "authors": ["x"], "same_family_accepted": True,
        "commits": ["a" * 40], "shorts": ["aaaaaaa"], "findings": []}]})
    args = argparse.Namespace(batch="CR-same-accepted", status="approved", reviewer="deepseek",
                              finding=None, fixed_by=None, note="weaker", force=True)
    assert commit_review.command_verdict(args) == 0


def test_the_reviewer_families_come_from_the_project_config(monkeypatch):
    monkeypatch.setattr(commit_review.paths, "review_reviewers", lambda: ("claude-opus",))
    sonnet = [{"family": "claude-sonnet"}] * 10
    assert commit_review.pick_reviewer(sonnet)[0] == "claude-opus"  # solo-claude: no Codex needed
    assert commit_review.independent_reviewer_families([{"family": "claude-opus"}]) == ()


def test_the_launch_hint_is_a_real_orca_command_not_a_placeholder():
    hint = commit_review.launch_hint("claude-opus", "work/agents/reports/reviews/CR-1.md")
    assert "orca orchestration worker-start" in hint and "--agent claude --model claude-opus-5-5" in hint
    assert "producer.py worker" not in hint


@pytest.mark.parametrize("message, env, refused", [
    ("fix thing", {"ORCA_TERMINAL_HANDLE": "term_1"}, True),
    ("fix thing\n\nCo-Authored-By: Claude <noreply@anthropic.com>", {"CLAUDECODE": "1"}, False),
    ("fix thing\n\nAuthored-By: human", {"ORCA_TERMINAL_HANDLE": "term_1"}, False),
    ("fix thing", {}, False),  # a person in a plain terminal is not forced to add anything
    ("Merge branch 'x'", {"CLAUDECODE": "1"}, False),
])
def test_an_agent_commit_must_carry_authorship(message, env, refused):
    assert (commit_review.authorship_problem(message, env) is not None) is refused


def test_polishing_uses_the_shipped_bullet_shape_and_does_not_duplicate(sandbox):
    commit_review.POLISH.write_text(
        "# Polishing TODO — findings of commit reviews and small cleanups\n\n"
        "Row shape: `- [ ] L<n> (<review id>, <date>) <file>:<line> — <finding> → <fix>`; done rows get `[x]`.\n",
        encoding="utf-8")
    batch = {"id": "CR-1", "reviewer": "deepseek", "closed_at": "2026-10-04T10:00:00+00:00",
             "findings": ["LIVE — tools/x.py:3 broken | pipe", "LIVE — tools/y.py:9 slow"],
             "fixed_since": ["FIXED BY abc1234 — tools/z.py:1 typo"]}
    commit_review.append_polishing(batch)
    commit_review.append_polishing(batch)  # idempotent
    text = commit_review.POLISH.read_text(encoding="utf-8")
    assert "- [ ] L2 (CR-1, 2026-10-04, deepseek) LIVE — tools/x.py:3 broken | pipe" in text or \
        "- [ ] L1 (CR-1, 2026-10-04, deepseek) LIVE — tools/x.py:3 broken | pipe" in text
    assert "- [ ] L" in text and "- [x] L" in text and "already fixed" in text
    assert text.count("tools/x.py:3") == 1 and "| batch |" not in text


def test_an_old_table_register_keeps_getting_table_rows(sandbox):
    commit_review.POLISH.write_text("# P\n\n| batch | reviewer | finding | status |\n|---|---|---|---|\n",
                                    encoding="utf-8")
    commit_review.append_polishing({"id": "CR-2", "reviewer": "r", "findings": ["a | b"], "fixed_since": []})
    assert "| CR-2 | r | a \\| b | open |" in commit_review.POLISH.read_text(encoding="utf-8")


def test_the_reports_row_names_one_canonical_repo_path(sandbox, monkeypatch):
    monkeypatch.setattr(commit_review, "PROJECT", sandbox)
    path = commit_review.PROMPTS / "CR-canon.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("brief", encoding="utf-8")
    assert commit_review._register_report(path, date="2026-01-01", domain="a | b", status="s",
                                          destination="x") == "added"
    row = commit_review.REPORTS.read_text(encoding="utf-8")
    assert "| reports/reviews/CR-canon.md |" in row and "a \\| b" in row

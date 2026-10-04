"""Contract tests for the tiered context index.

The one property that matters: **every abstract and every lead is a sentence the author actually
wrote**, quoted with the line it came from.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "context_index.py"
SPEC = importlib.util.spec_from_file_location("context_index", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["context_index"] = MODULE
SPEC.loader.exec_module(MODULE)


SAMPLE = """# The pricing page converts worse than the landing page

Some framing that is not the point.

**Verdict first:** the long pricing page converts less as it gets longer, and the mobile funnel is the proof.

## What the numbers say

The short page converts at 4.1 percent while the long page converts at 1.9 percent.

```
## not a heading, this is fenced
```

### The exception

The enterprise form is the single page that breaks it.

## What to do

Nothing until the owner rules on §1.105.
"""


def _document(tmp_path: Path, text: str = SAMPLE, name: str = "sample.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        return MODULE._read_document(path)
    finally:
        MODULE.ROOT = original_root


def test_the_abstract_is_the_documents_own_verdict_line_quoted_verbatim(tmp_path):
    document = _document(tmp_path)
    assert document.title == "The pricing page converts worse than the landing page"
    assert document.abstract.startswith("Verdict first: the long pricing page converts less")
    source = (tmp_path / "sample.md").read_text(encoding="utf-8").split("\n")
    quoted = source[document.abstract_line - 1]
    assert "the long pricing page converts less" in quoted


def test_a_document_without_a_marked_verdict_falls_back_to_its_first_prose_line(tmp_path):
    document = _document(tmp_path, "# Plain\n\nFirst real sentence. Second one.\n", "plain.md")
    assert document.abstract == "First real sentence."
    assert document.abstract_line == 3


def test_sections_carry_real_line_ranges_and_skip_fenced_headings(tmp_path):
    document = _document(tmp_path)
    titles = [section.title for section in document.sections]
    assert titles == ["What the numbers say", "The exception", "What to do"]
    assert "not a heading" not in " ".join(titles)
    numbers = document.sections
    for earlier, later in zip(numbers, numbers[1:]):
        assert earlier.end_line == later.line - 1
        assert earlier.line < earlier.end_line
    assert numbers[0].lead.startswith("The short page converts")
    assert all(section.tokens > 0 for section in numbers)


def test_ranking_prefers_a_path_or_title_hit_and_discounts_cold_files():
    hot = {"path": "work/agents/reports/hero_animation.md", "title": "Hero animation",
           "abstract": "", "cold": False, "sections": []}
    cold = dict(hot, path="work/archive/hero_animation.md", cold=True)
    body_only = {"path": "work/agents/notes.md", "title": "Notes",
                 "abstract": "a hero animation showed up here once", "cold": False, "sections": []}
    terms = MODULE._terms("animation")
    assert MODULE._score(hot, terms) > MODULE._score(body_only, terms)
    assert MODULE._score(cold, terms) < MODULE._score(hot, terms)


def test_the_index_never_invents_text(tmp_path):
    document = _document(tmp_path)
    source = (tmp_path / "sample.md").read_text(encoding="utf-8")
    for fragment in [document.abstract, *(section.lead for section in document.sections)]:
        if not fragment:
            continue
        core = fragment.rstrip("…").split(":")[-1].strip()[:40]
        assert core in " ".join(source.split()), fragment


def test_a_same_length_edit_makes_the_index_stale(tmp_path, monkeypatch, capsys):
    """Size alone cannot see a reversed conclusion written in the same number of bytes."""
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "INDEX_PATH", tmp_path / "idx" / "index.json")
    monkeypatch.setattr(MODULE, "SCAN_FILES", ())
    page = tmp_path / "work" / "decision.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Decision\n\nVerdict: ship the animation now.\n", encoding="utf-8")
    MODULE.build_index()
    assert MODULE.command_fresh(None) == 0
    page.write_text("# Decision\n\nVerdict: drop the animation now.\n", encoding="utf-8")  # same length
    assert len(page.read_text(encoding="utf-8")) == len("# Decision\n\nVerdict: ship the animation now.\n")
    assert MODULE.command_fresh(None) == 1
    assert "1 changed" in capsys.readouterr().out

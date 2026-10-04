"""knowledge_gate.verdict: a product commit needs its knowledge write-back.

The gate is generic and OFF until ``producer.toml [knowledge] product_globs`` is set, so these
tests set the globs on the in-memory module.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_gate as kg  # noqa: E402

PRODUCT = "config/app.toml"
PAGE = "work/agents/knowledge/guides/TOME.md"


def _globs(*globs):
    kg.paths.product_globs = lambda: tuple(globs)


def test_gate_off_by_default_refuses_nothing():
    _globs()
    assert kg.verdict([PRODUCT], "change") == []


def test_product_alone_is_refused():
    _globs("config/**")
    assert kg.verdict([PRODUCT], "fix config") != []


def test_product_with_knowledge_page_passes():
    _globs("config/**")
    assert kg.verdict([PRODUCT, PAGE], "fix config") == []


def test_trailer_passes():
    _globs("config/**")
    assert kg.verdict([PRODUCT], "fix\n\nKnowledge: n/a — pure revert") == []
    assert kg.verdict([PRODUCT], "fix\n\nKnowledge: work/agents/knowledge/guides/TOME.md") == []


def test_non_product_paths_are_not_gated():
    _globs("config/**", "src/**", "**/*.md")
    assert kg.verdict(["work/agents/registers/TODO.md", "work/БРИФ.md"], "x") == []
    assert kg.verdict(["src/tests/test_app.py", "src/app_test.js", "tests/e2e.py"], "x") == []
    assert not kg.is_product(".runtime/notes.md") and not kg.is_product(".claude/skills/x/SKILL.md")


def test_markdown_deliverables_are_gated_when_the_owner_configures_them():
    # A documentation / research project: its product IS markdown.
    _globs("docs/**", "chapters/*.md")
    assert kg.is_product("docs/guide/install.md") and kg.is_product("chapters/01.md")
    assert kg.verdict(["chapters/01.md"], "write chapter") != []
    assert kg.verdict(["chapters/01.md", PAGE], "write chapter") == []


def test_markdown_outside_the_configured_globs_is_still_not_a_product():
    _globs("src/**")
    assert not kg.is_product("README.md") and not kg.is_product("docs/guide.md")

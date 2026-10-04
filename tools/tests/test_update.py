"""tools/update.py on real temporary repositories: it only ever fast-forwards a clean tree."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import update

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True)
    return done.stdout


@pytest.fixture
def pair(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    mine = tmp_path / "mine"
    _git(tmp_path, "clone", "-q", str(origin), str(mine))
    (mine / "a.txt").write_text("one\n", encoding="utf-8")
    _git(mine, "add", "a.txt")
    _git(mine, "commit", "-q", "-m", "first")
    _git(mine, "push", "-q", "origin", "HEAD:main")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    monkeypatch.setattr(update.paths, "PROJECT", mine)
    return mine, other


def _push_from_other(other: Path, name: str = "b.txt") -> None:
    (other / name).write_text("two\n", encoding="utf-8")
    _git(other, "add", name)
    _git(other, "commit", "-q", "-m", "second")
    _git(other, "push", "-q", "origin", "HEAD:main")


def test_it_fast_forwards_a_clean_tree(pair, capsys):
    mine, other = pair
    _push_from_other(other)
    assert update.main(["--check"]) == 0 and "1 commit(s) behind" in capsys.readouterr().out
    assert not (mine / "b.txt").exists()  # --check changes nothing
    assert update.main([]) == 0 and "pulled 1 commit(s)" in capsys.readouterr().out
    assert (mine / "b.txt").exists()
    assert update.main([]) == 0 and "up to date" in capsys.readouterr().out


def test_it_refuses_over_tracked_and_untracked_work_and_changes_nothing(pair, capsys):
    mine, other = pair
    _push_from_other(other)
    (mine / "notes.txt").write_text("my draft\n", encoding="utf-8")  # untracked counts too
    assert update.main([]) == 1
    assert "uncommitted" in capsys.readouterr().err
    assert not (mine / "b.txt").exists() and (mine / "notes.txt").read_text(encoding="utf-8") == "my draft\n"
    (mine / "notes.txt").unlink()
    (mine / "a.txt").write_text("edited\n", encoding="utf-8")  # tracked edit
    assert update.main([]) == 1
    assert (mine / "a.txt").read_text(encoding="utf-8") == "edited\n"


def test_a_diverged_history_is_reported_not_merged(pair, capsys):
    mine, other = pair
    _push_from_other(other)
    (mine / "c.txt").write_text("mine\n", encoding="utf-8")
    _git(mine, "add", "c.txt")
    _git(mine, "commit", "-q", "-m", "mine")
    before = _git(mine, "rev-parse", "HEAD")
    assert update.main([]) == 1
    assert "diverged" in capsys.readouterr().err
    assert _git(mine, "rev-parse", "HEAD") == before


def test_no_origin_says_how_to_make_one(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q")
    monkeypatch.setattr(update.paths, "PROJECT", repo)
    assert update.main([]) == 2
    assert "gh repo create" in capsys.readouterr().err


def test_outside_a_repository_it_does_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(update.paths, "PROJECT", tmp_path)
    assert update.main([]) == 2
    assert "not a git repository" in capsys.readouterr().err

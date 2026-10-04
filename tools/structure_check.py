#!/usr/bin/env python3
"""Check that work/agents/knowledge/STRUCTURE.md names every tracked folder and top-level file.

AGENTS.md «File structure» makes STRUCTURE.md the single map of the repository: a folder that exists in git but not on the
page is an undocumented folder, and a path on the page that no longer exists is a stale line. Exit 0 = in sync, 1 = drift
(each missing/stale path printed), 2 = not a git checkout. Stdlib only; reads `git ls-files`, never the working tree, so
gitignored scratch never counts.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "work" / "agents" / "knowledge" / "STRUCTURE.md"
# Documented on the page but never tracked by git, on purpose.
UNTRACKED_OK = {".runtime/"}
# Topic folders under these parents are created freely; the parent's own line documents them.
FREE_PARENTS = ("work/agents/reports/",)
BULLET = re.compile(r"^- `([^`]+)`", flags=re.M)


class NotAGitRepo(RuntimeError):
    """`git ls-files` failed: the directory is not a git checkout (a copied folder or a ZIP)."""


def tracked_paths(repo: Path = REPO) -> set[str]:
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"], cwd=repo,
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise NotAGitRepo((out.stderr or out.stdout or "git ls-files failed").strip()[:200])
    paths: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.strip().split("/")
        if len(parts) == 1:
            paths.add(parts[0])
        for i in range(1, len(parts)):
            paths.add("/".join(parts[:i]) + "/")
    return {p for p in paths if not any(p.startswith(fp) and p != fp for fp in FREE_PARENTS)}


def documented_paths(page: Path = PAGE) -> set[str]:
    return set(BULLET.findall(page.read_text(encoding="utf-8")))


def check(repo: Path = REPO, page: Path = PAGE) -> tuple[list[str], list[str]]:
    tracked = tracked_paths(repo)
    documented = documented_paths(page)
    missing = sorted(tracked - documented)
    stale = sorted(p for p in documented - tracked if p not in UNTRACKED_OK)
    return missing, stale


def main() -> int:
    try:
        missing, stale = check()
    except NotAGitRepo as exc:
        print(f"structure: not a git repository ({exc}) - run `git init` (or `python tools/setup.py`, which does it)")
        return 2
    for p in missing:
        print(f"MISSING from STRUCTURE.md: {p}")
    for p in stale:
        print(f"STALE in STRUCTURE.md (not tracked): {p}")
    if missing or stale:
        print(f"structure: DRIFT ({len(missing)} missing, {len(stale)} stale) - fix work/agents/knowledge/STRUCTURE.md")
        return 1
    print("structure: in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""update.py — bring YOUR project's checkout up to date from YOUR remote, and nothing more.

    python tools/update.py            # fast-forward the current branch from `origin`
    python tools/update.py --check    # say what would change, change nothing

Safe by construction: it refuses when the working tree has tracked OR untracked changes (it would
never overwrite or reshuffle your work), uses `git pull --ff-only` (a diverged history is reported,
never merged or rebased for you), never runs `checkout -- <path>`, `reset` or `clean`, and does
not touch the `template` remote.

Two different things are called «updating»:
  * your project (this tool): `origin` = your own repository, e.g. pulling your other machine's commits;
  * the skeleton's own improvements: `git fetch template`, then read `git log HEAD..template/main`
    and merge by hand — template changes can touch files you have already rewritten.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import paths


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=paths.PROJECT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def dirty_paths() -> list[str]:
    done = git("status", "--porcelain", "--untracked-files=all")
    return [line for line in done.stdout.splitlines() if line.strip()] if done.returncode == 0 else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="show what would change; change nothing")
    args = parser.parse_args(argv)
    paths.console_safe()
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("not a git repository: nothing to update", file=sys.stderr)
        return 2
    remote = git("remote", "get-url", "origin")
    if remote.returncode != 0:
        print("no `origin` remote: create your own repository first (setup.py prints how; "
              "`gh repo create <name> --private --source . --remote origin --push`)", file=sys.stderr)
        return 2
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = dirty_paths()
    if dirty:
        print(f"{len(dirty)} uncommitted path(s): commit or stash them first, then run this again "
              "(nothing was changed):", file=sys.stderr)
        for line in dirty[:10]:
            print(f"  {line}", file=sys.stderr)
        return 1
    fetched = git("fetch", "origin")
    if fetched.returncode != 0:
        print(f"fetch failed: {(fetched.stderr or fetched.stdout).strip()[:200]}", file=sys.stderr)
        return 1
    behind = git("rev-list", "--count", f"HEAD..origin/{branch}")
    count = int(behind.stdout.strip() or 0) if behind.returncode == 0 else 0
    if args.check:
        print(f"{branch}: {count} commit(s) behind origin/{branch}")
        return 0
    if count == 0:
        print(f"{branch} is up to date with origin/{branch}")
        return 0
    pulled = git("pull", "--ff-only", "origin", branch)
    if pulled.returncode != 0:
        print("could not fast-forward (your history and origin have diverged). Nothing was merged; "
              "decide how to combine them yourself:\n" + (pulled.stderr or pulled.stdout).strip()[:300], file=sys.stderr)
        return 1
    print(f"{branch}: pulled {count} commit(s) from origin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

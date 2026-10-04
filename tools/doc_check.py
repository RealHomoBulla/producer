#!/usr/bin/env python3
"""doc_check.py — every command and file an agent-facing page names must exist.

A handbook that points at a tool that was renamed, a file that was never written or a heading
that was reworded sends the next agent down a dead end, and nobody notices until a night run
stalls on it. This reads the tracked Markdown pages and reports:

  * `python tools/<name>.py` that names a tool that is not in `tools/`;
  * a backticked repo path (`work/...`, `tools/...`, `.claude/...`, a root file) that does not exist
    (paths with placeholders such as `<topic>`, `*`, `YYYY`, `{...}` and `.runtime/` are skipped);
  * `PAGE.md §Heading` / `PAGE.md «Heading»` pointers whose heading is not in that page;
  * (`--run-help`) a referenced tool whose `--help` does not exit 0.

    python tools/doc_check.py              # exit 1 and a list when something is dead
    python tools/doc_check.py --run-help   # also run `--help` of every referenced tool

Text under `.claude/skills/graphify/` (a third-party skill) and `work/agents/reports/` (dated,
historical) is not checked: history is allowed to name what is gone.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import paths

ROOT = paths.PROJECT
SKIP_PARTS = (".claude/skills/graphify/", "work/agents/reports/", ".runtime/", "tools/templates/")
TOOL_RE = re.compile(r"python3?\s+(tools/[A-Za-z0-9_]+\.py)")
TICK_RE = re.compile(r"`([^`\n]+)`")
PATH_START = ("work/", "tools/", ".claude/", ".github/")
ROOT_FILES = {"AGENTS.md", "README.md", "README.en.md", "INVENTORY.md", "CLAUDE.md", "LICENSE",
              "producer.toml", ".gitignore"}
PLACEHOLDER = re.compile(r"[<>*{}…]|YYYY|\.\.\.|\bN\b|<n>|NNN")
HEADING_PTR = re.compile(r"`?([A-Za-z0-9_./-]+\.md)`?\s*(?:§|«)\s*([^»`\n)]+?)\s*(?:»|`|\)|$|[.,;])")


def markdown_pages(root: Path = ROOT) -> list[Path]:
    pages = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if any(part in rel + ("/" if path.is_dir() else "") for part in SKIP_PARTS):
            continue
        if any(seg in (".git", "node_modules", "__pycache__") for seg in path.parts):
            continue
        pages.append(path)
    return pages


def headings(page: Path) -> list[str]:
    out = []
    for line in page.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if match:
            out.append(re.sub(r"[`*_]", "", match.group(1)).lower())
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def heading_matches(wanted: str, page_headings: list[str]) -> bool:
    """A pointer names a heading by its number (`§0a`) or by its first words (`§Probes`).

    Only the opening of the pointer is compared, because prose often runs on after it.
    """
    words = _words(wanted)
    if not words:
        return True
    heads = [_words(h) for h in page_headings]
    if re.fullmatch(r"\d+[a-z]?", words[0]):
        return any(h and h[0] == words[0] for h in heads)
    key = words[:2] if len(words) >= 2 else words
    for h in heads:
        joined = " ".join(h)
        if " ".join(key) in joined or " ".join(words[:1]) == (h[0] if h else ""):
            return True
    return False


def alias_exists(candidate: str, root: Path) -> bool:
    """`work/BRIEF.md` counts when the Russian `work/БРИФ.md` is there (and the other way round):
    owner-page names follow the owner's language (`paths.PAGE_NAMES`)."""
    path = Path(candidate)
    for kind in paths.PAGE_NAMES["ru"]:
        names = {table[kind] for table in paths.PAGE_NAMES.values()}
        if path.name in names:
            return any((root / path.parent / name).exists() for name in names)
    return False


def find_page(name: str, root: Path) -> Path | None:
    """A `NAME.md` pointer: the file at that repo path, else the unique file with that name."""
    direct = root / name
    if direct.is_file():
        return direct
    matches = [p for p in root.rglob(Path(name).name) if p.is_file() and ".git" not in p.parts]
    return matches[0] if len(matches) == 1 else None


def check(root: Path = ROOT, run_help: bool = False) -> list[str]:
    problems: list[str] = []
    tools_seen: set[str] = set()
    for page in markdown_pages(root):
        rel = page.relative_to(root).as_posix()
        text = page.read_text(encoding="utf-8", errors="replace")
        for match in TOOL_RE.finditer(text):
            tool = match.group(1)
            tools_seen.add(tool)
            if not (root / tool).is_file():
                problems.append(f"{rel}: names `python {tool}` but {tool} does not exist")
        for match in TICK_RE.finditer(text):
            token = match.group(1).strip()
            first = token.split()[0] if token.split() else ""
            candidate = first.rstrip(".,;:")
            if not (candidate.startswith(PATH_START) or candidate in ROOT_FILES):
                continue
            if PLACEHOLDER.search(candidate) or candidate.endswith("/") and False:
                continue
            target = root / candidate.rstrip("/")
            if not target.exists() and not alias_exists(candidate, root):
                problems.append(f"{rel}: names `{candidate}` which does not exist")
        for match in HEADING_PTR.finditer(text):
            name, wanted = match.group(1), match.group(2).strip().lower()
            target = find_page(name, root)
            if target is None or target.suffix != ".md":
                continue  # a missing page is reported by the path check above
            if not heading_matches(wanted, headings(target)):
                problems.append(f"{rel}: points at {name} §{match.group(2).strip()} but that heading is not there")
    if run_help:
        for tool in sorted(tools_seen):
            if not (root / tool).is_file():
                continue
            done = subprocess.run([sys.executable, str(root / tool), "--help"], capture_output=True,
                                  text=True, timeout=60, encoding="utf-8", errors="replace", cwd=root)
            if done.returncode != 0:
                problems.append(f"`python {tool} --help` exits {done.returncode}: "
                                f"{(done.stderr or done.stdout).strip()[:120]}")
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-help", action="store_true", help="also run --help of every referenced tool")
    args = parser.parse_args(argv)
    paths.console_safe()
    problems = check(run_help=args.run_help)
    if not problems:
        print("doc_check: every named command, file and heading exists")
        return 0
    print(f"doc_check: {len(problems)} dead reference(s)")
    for line in problems:
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

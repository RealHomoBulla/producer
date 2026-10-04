#!/usr/bin/env python3
"""Knowledge gate: a product change cannot be committed without its knowledge write-back.

A discovery, a changed behaviour or a ruling that is not written down dies with the session that
found it. This gate makes the write-back part of the commit.

Rules, checked by the git `commit-msg` hook on the STAGED set (so `commit --only` is judged by
what it commits):
  1. A commit that stages a path matching a glob in `producer.toml [knowledge] product_globs`
     must also stage a page under `work/agents/knowledge/` or `work/systems/`, or carry a trailer
     line `Knowledge: <page path>` (updated in an earlier commit of the same change) or
     `Knowledge: n/a — <reason>`.
  2. Empty `product_globs` (the default) means the gate is OFF and nothing is refused.

Commands:
  knowledge_gate.py install          write .git/hooks/commit-msg (idempotent): this gate, then the
                                     commit-review authorship check (agent commits need a trailer)
  knowledge_gate.py status           one line: hook installed? + product commits in the last 7 days with no knowledge
  knowledge_gate.py debt [--days N]  list those commits (sha, date, subject)
  knowledge_gate.py check-msg FILE   the hook body (exit 1 = commit refused)
Bypassing with --no-verify is forbidden.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import paths

ROOT = paths.PROJECT
KNOWLEDGE_PREFIXES = ("work/agents/knowledge/", "work/systems/")
TRAILER = re.compile(r"^Knowledge:\s*\S", re.M)
HOOK_MARK = "# knowledge_gate"


def _git(*args: str) -> str:
    return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


# Never a product, whatever the globs say: the skeleton's own working memory, runtime state and
# tests. Everything else - Markdown and prose included - is a product when a configured glob
# matches it, so a documentation or research project's real deliverables are gated too.
SERVICE_PREFIXES = ("work/", ".runtime/", ".claude/", ".git/")
TEST_NAME = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.[a-z]+$)")


def is_service(path: str) -> bool:
    return path.startswith(SERVICE_PREFIXES) or bool(TEST_NAME.search(path))


def is_product(path: str) -> bool:
    if is_service(path):
        return False
    return paths.matches_product(path)


def is_knowledge(path: str) -> bool:
    return path.startswith(KNOWLEDGE_PREFIXES)


def verdict(paths_list: list[str], message: str) -> list[str]:
    """Return the refusal reasons (empty = allowed)."""
    if not paths.product_globs():
        return []  # gate off
    product = [p for p in paths_list if is_product(p)]
    reasons = []
    if product and not any(is_knowledge(p) for p in paths_list) and not TRAILER.search(message):
        reasons.append("product change without knowledge: " + ", ".join(product[:5])
                       + (" …" if len(product) > 5 else "")
                       + "\n  → stage the owning page under work/agents/knowledge/ (fact + command"
                       + " + date) or work/systems/, or add a trailer line"
                       + "\n    «Knowledge: <page>» / «Knowledge: n/a — <reason>»")
    return reasons


def check_msg(msg_file: str) -> int:
    message = Path(msg_file).read_text(encoding="utf-8", errors="replace")
    changed = [p for p in _git("diff", "--cached", "--name-only").splitlines() if p]
    reasons = verdict(changed, message)
    if reasons:
        sys.stderr.write("\nKNOWLEDGE GATE refused this commit:\n- " + "\n- ".join(reasons) + "\n\n")
        return 1
    return 0


def _hook_path() -> Path:
    hooks = Path(_git("rev-parse", "--git-path", "hooks").strip() or ".git/hooks")
    return (hooks if hooks.is_absolute() else ROOT / hooks) / "commit-msg"


def install() -> int:
    hook = _hook_path()
    body = ("#!/bin/sh\n" + HOOK_MARK + " — commit-msg gates: knowledge write-back, then authorship\n"
            "PY=python; command -v python >/dev/null 2>&1 || PY=python3  # Windows: python; POSIX: python3\n"
            'ROOT="$(git rev-parse --show-toplevel)"\n'
            '"$PY" "$ROOT/tools/knowledge_gate.py" check-msg "$1" || exit 1\n'
            '"$PY" "$ROOT/tools/commit_review.py" check-msg "$1" || exit 1\n')
    if hook.exists() and HOOK_MARK not in hook.read_text(encoding="utf-8", errors="replace"):
        print(f"refusing: {hook} exists and is not ours — merge by hand")
        return 1
    hook.write_text(body, encoding="utf-8", newline="\n")
    try:
        hook.chmod(0o755)
    except OSError:
        pass
    print(f"installed {hook}")
    return 0


def debt(days: int) -> list[str]:
    out = []
    log = _git("log", f"--since={days} days ago", "--format=%x00%h%x09%ad%x09%s%n%b",
               "--date=short", "--name-only")
    for block in log.split("\x00")[1:]:
        head, _, rest = block.partition("\n")
        lines = rest.splitlines()
        changed = [line for line in lines if "/" in line or line.endswith((".py", ".json", ".toml"))]
        body = "\n".join(line for line in lines if line not in changed)
        if any(is_product(p) for p in changed) and not any(is_knowledge(p) for p in changed) \
                and not TRAILER.search(body):
            out.append(head)
    return out


def status() -> int:
    hook = _hook_path()
    installed = hook.exists() and HOOK_MARK in hook.read_text(encoding="utf-8", errors="replace")
    rows = debt(7)
    print(f"knowledge gate: hook {'installed' if installed else 'MISSING (knowledge_gate.py install)'}"
          f" · product_globs {'off' if not paths.product_globs() else len(paths.product_globs())}"
          f" · 7-day product commits without knowledge: {len(rows)}")
    return 0 if installed else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "check-msg" and len(argv) == 2:
        return check_msg(argv[1])
    if cmd == "install":
        return install()
    if cmd == "status":
        return status()
    if cmd == "debt":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
        for row in debt(days):
            print(row)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# Project structure — the file tree and what each part is for

**The single map of this repository.** Every tracked folder and top-level file appears here with one line on its purpose.
Adding, moving or removing a folder (or a top-level file) updates this page **in the same commit**;
`python tools/structure_check.py` fails when the tree and this page disagree.

Format: one bullet per path, `` `path/` — purpose``. Folders end with `/`. Keep it short; details live in the folder's own pages.

## Root

- `AGENTS.md` — the handbook every agent reads first
- `CLAUDE.md` — pointer to AGENTS.md for Claude Code
- `README.md` — the human entry: install, connect accounts, quick start
- `INVENTORY.md` — where the skeleton came from, what it carries and what it left
- `LICENSE` — MIT
- `producer.sh` — everyday commands on Linux/macOS/Git Bash (`./producer.sh help`)
- `producer.cmd` — the same on Windows (`producer help`)
- `requirements.txt` — runtime dependencies of the toolchain (only `tzdata` on Windows)
- `requirements-dev.txt` — test dependencies (`pytest`)
- `producer.toml` — project settings: name, language, timezone, keys path, Orca Run, Guardian, roster, knowledge globs
- `.gitattributes` — line endings: normalised text, LF for `*.sh`, CRLF for `*.cmd`
- `.gitignore` — keeps `.runtime/`, caches and any `*.env` out of git

## Agent tooling

- `.github/` — GitHub configuration
- `.github/workflows/` — CI: doc check, structure check and the tool tests on Windows and Linux
- `.claude/` — Claude Code project settings (`settings.json`: output style and the two optional hooks) and skills
- `.claude/skills/` — skills every Claude seat loads
- `.claude/skills/writing-for-agents/` — how to write pages, briefs and skills agents can use
- `.claude/skills/docs-checkup/` — the heavy documentation audit, only on the word «чекап» / `checkup`
- `.claude/skills/work-recall/` — find things in `work/` without reading blind
- `tools/` — Producer toolchain: paths, digest, unanswered, commit review, mailbox, context index, knowledge gate, status, Guardian (load limits, Remote Control opt-in), worker launcher/closer/reaper (`worker.py`), setup, structure check, doc check, Claude hooks, dev server, safe update, blitz queue and cross-check (`blitz.py`)
- `tools/tests/` — pytest for every tool (`python -m pytest -q tools/tests`)
- `tools/templates/` — owner-page seed templates per language (setup.py --lang en swaps the Russian seeds for these)
- `tools/templates/en/` — the English seed pages

## Working memory

- `work/` — the owner's inbox: brief, roadmap, digest, unanswered questions, checklist
- `work/systems/` — one page per part of the product, owner's language, rewritten in place
- `work/agents/` — everything agents produce; `WORKLOG.md` is the append-only journal
- `work/agents/state/` — HANDOVER, owner's last messages, SELF_CHECK (promises made in chat), tool state files
- `work/agents/registers/` — TODO, OPEN, REPORTS, far drawer, polishing, blitz READY/ACTIVE files
- `work/agents/orca/` — how the Producer, Workers and Guardian operate; models and routing
- `work/agents/knowledge/` — current facts about the product, one page per domain (this page included)
- `work/agents/reports/` — dated Worker reports, one subfolder per topic (topic folders need no line here)

## Not tracked

- `.runtime/` — briefs, checkpoints, Run binding, this project's mailbox (`.runtime/mailbox/`), cursor, Guardian heartbeat and log, Worker launch ledger `worker-launches.jsonl` (gitignored)

The user's keys live in `~/.config/producer/keys.env`, outside the repository.

## Product

_(added at kickoff: the product's own folders — e.g. `site/`, `src/`, `public/` — each with one line)_

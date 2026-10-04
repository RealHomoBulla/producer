# Inventory — where this skeleton came from, what it carries, what it left

Source: the author's own large project, where this process was developed (2026-08…10). The process was generalised for any
project; the product-specific content stayed behind. **«Generalised» is a claim to keep checking, not a fact:** a few test
fixtures and dated measurements still come from that work (marked below), and `python tools/doc_check.py` keeps the handbook
honest about the files and commands it names.

## Carried (generalised)

| here | from | what changed |
|---|---|---|
| `AGENTS.md` | the source handbook | laws kept: open decisions, repo = master, verify, burn quota to 100 %, commit freely + `--only`, cross-family review every 10th commit (now with a same-family fallback), product-is-a-file, reports→decisions, short questions a/b/c, composer text ≠ owner, subagents OK, climb the ladder, knowledge follows change, keys outside git. New for sharing: **language rule** (owner's language to the owner, English internally), imported-advice intake, change-impact lines, checkable docs. Dropped: every product-specific law |
| `work/agents/orca/START_PROMPT.md` | the source bootstrap | bootstrap/continuity/mailbox/acceptance/write-back loop; Guardian check, promises (`SELF_CHECK.md`), optional hooks |
| `work/agents/orca/DISPATCH.md` | the source dispatch page | brief template, zones, **worked example of parallel zones**, **risk gates G0–G3**, weaker-seat addendum, blitz brief, commit-review brief |
| `work/agents/orca/OWNER_SHORTHANDS.md` | the source shorthands | блиц / дайджест / лимиты / ночная вахта / стоп / чекап …, with English equivalents |
| `work/agents/orca/AUTONOMY.md`, `knowledge/GUARDIAN.md` | the source watchdog notes | rewritten for the small Guardian |
| `work/agents/orca/ROUTING.md` | routing history + model calibration | routes, probes, credentials; the author's preferences are now an empty «Your preferences» slot, the measurements are dated calibration |
| `work/agents/knowledge/MODEL_ADVICE.md`, `tools/presets.toml`, `tools/usage.py` | the author's quota/route practice | presets (solo-claude, claude-max-100, claude-plus-go, budget-free), advice, one-screen quota |
| `work/agents/orca/MACHINE_TRAPS.md` | the source traps | generic shell/git/Windows/Orca traps + a measured-per-machine «Load» policy |
| `work/agents/registers/*`, `state/*`, `work/*.md` | the live registers | empty templates in the same shape; English seed pages in `tools/templates/en/` |
| `.claude/skills/writing-for-agents`, `work-recall`, `docs-checkup` | the source skills | examples and source-specific rules removed; `writing-for-agents` keeps its own `LICENSE` |
| graphify skill | a third-party skill | **not shipped** (licence not verified): install it yourself if you want a code map |
| `.claude/settings.json` | the source's local settings | `outputStyle: Concise`, `promptSuggestionEnabled: false`, and two optional fail-open hooks (owner messages, stop guard) |
| `tools/` | the source toolchain, reduced | stdlib-only: `paths`, `digest`, `unanswered`, `ruling_gate`, `commit_review`, `knowledge_gate`, `context_index`, `blitz`, `producer` (status, bind-run, checkpoint, rotation alarm, **doctor**), `drain_mailbox`/`mailbox_cursor` (per-project mailbox), `orca_cli`, `guardian`, `worker`, `setup`, `usage`, `hooks`, `serve`, `doc_check`, `structure_check`; shared `state_io`, `owner_text`; tests in `tools/tests/` |
| keys | — | **no key is in this repository.** Each user keeps their own in the file `[paths] keys_file` names (default `~/.config/producer/keys.env`, outside git); provider CLIs keep their own logins. Setup: README «Подключи свои аккаунты» |

Fixtures that still echo the source and are harmless (they assert behaviour, not facts): the timezone `Europe/Moscow` in a few
setup/config tests, and Russian owner-page wording in the digest/unanswered tests (the Russian owner is a supported language).

## Left behind on purpose

- Everything about the source product: its economy, pricing and generation tools, game checklist, server/client sync, workstation
  transfer, the large knowledge tree and its law history.
- Dated reports and rosters' history (only the distilled verdicts went into `ROUTING.md` and `MODEL_ADVICE.md`).
- The source supervisor's special cases (quota calendars, fixed load numbers, per-machine bans): replaced by configuration.

## Not carried yet

1. **Compaction hooks** (checkpoint around a context compaction) and a context meter — the two shipped hooks cover owner
   messages and the stop guard only.
2. **Source-specific launchers** (`claude-worker`, `go-worker`, `agy-seat` …): `tools/worker.py` launches through Orca's
   `worker-start` with a receipt instead.
3. A published, tested Orca version range: the tools verify the CLI at runtime (`producer.py doctor`) but do not pin a version.

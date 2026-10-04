---
name: docs-checkup
description: "THE HEAVY, ONCE-A-DAY-OR-TWO documentation audit: owner pages, knowledge pages and STRUCTURE.md against the live project. Trigger ONLY on the explicit word «чекап» / «чекап доков» / `checkup` / `docs checkup`. It runs a read-only audit wave, rebuilds stale owner pages from live truth, recovers requests that were never written down, routes every finding to its owner register and verifies itself with a fresh, adversarial session. DO NOT run it for «обнови доки», «поправь доки», «допиши в доки», «актуализируй страницу» or any ordinary request to update documentation — that is a light in-place edit (§-1) and happens many times a day."
---

# docs-checkup — the heavy pass, and only on the word «чекап» / `checkup`

## §-1. First: is this actually a checkup? Usually it is not.

The owner calls the heavy audit by one word so that everyday doc edits stay cheap. He updates docs many times a day and runs
the checkup **once a day or two**.

| he says | what to do |
|---|---|
| «чекап», «чекап доков», `checkup`, `docs checkup` | **this page, in full** |
| «обнови доки», «поправь доки», «допиши это в доки», «актуализируй эту страницу», "update the docs" | **light mode:** open the one or two pages the change touches, edit them in place, run `python tools/doc_check.py`, stop. No Run, no Workers, no repo-wide grep |
| anything ambiguous | assume **light mode** and say in one line that the heavy checkup exists if he wants it |

Light mode still obeys the standing rules (the register owns decisions, no live number quoted from a page, the owner's inbox
keeps only its fixed pages). **What follows is the heavy pass. Do not start it without the word.**

## 0. The three rules that save the most effort

1. **Do not read the big pages yourself.** A read-only Worker audits `OPEN.md`, `TODO.md`, `WORKLOG.md` and returns a
   line-addressed table; you read the table. Reading them yourself is expensive and leaves no artefact.
2. **Rewrite from live truth, never patch the old text.** A stale page is a diary; editing its sentences keeps the structure that
   made it stale. Get the audit, then rewrite the page from `HANDOVER.md` + the live `OPEN.md` index + the top block of
   `TODO.md` + the files themselves.
3. **Never store runtime state on an owner page** («the server is running», cache dates, counts, quota). It rots within hours.
   Point at the command that says it (`python tools/producer.py status`, `python tools/usage.py`).

## 1. The audit wave — read-only Workers (gate G0), dispatched together

One Run, one Task per row, disjoint zones (`DISPATCH.md`). With **one Claude subscription** run A and B first (two seats) and C
afterwards, or do C yourself.

| Worker | job | report |
|---|---|---|
| **A** owner pages | every owner page (`work/*.md` root pages in the owner's language, every `work/systems/*.md`) vs the live files: stale, decided-but-still-asked, wrong number/command | `work/agents/reports/docs_audit/<date>_OWNER_PAGES_AUDIT.md` |
| **B** knowledge + structure | every `work/agents/knowledge/` page and `STRUCTURE.md` vs the real tree and commands: `python tools/structure_check.py`, `python tools/doc_check.py --run-help`, `python tools/context_index.py fresh`; each fact still carries its proving command | `…_KNOWLEDGE_STRUCTURE_AUDIT.md` |
| **C** unrecorded requests | things the owner asked that exist nowhere: `work/agents/state/OWNER_LAST_MESSAGES.md`, `HANDOVER.md`, `WORKLOG.md`, the chat logs the machine keeps (e.g. `~/.claude/projects`), `state/SELF_CHECK.md` promises with no TODO row | `…_UNRECORDED_REQUESTS.md` |

Tell every Worker: **it runs in the real checkout with full read access; the tree may be dirty with other people's changes —
that is expected and is not a failure; verify your own write with a path-scoped `git status --porcelain -- <paths>`.** C greps
for intent markers first (`надо`, `нужно`, `не забудь`, `идея`, `хочу`, `можно ли`, `запиши`, `позже`, `отложи`, `убери`,
`добавь`, `верни`, `TODO`, `later`, "remember to"), caps matches per file, never dumps a transcript, and must list the terms it
searched: **a bounded regex proves absence only for the strings it searched** — run a second pass over an older window.

Page list: take it from `AGENTS.md` («`work/` layout» table) at run time, never from memory; anything in the `work/` root that
is not one of the fixed owner pages is itself a finding.

### The defect classes that matter (ask for exactly these)

- **A1 action-changing stale** — the page tells him to do something already done, reversed or now wrong. *The whole point.*
- **A2 decided-but-still-asked** — `OPEN.md` closed it, a page still asks.
- **A3 wrong number or command** — including a live number that should be a command instead.
- **B1 superseded detail** → move to a dated report. **B2 redundant** → name the one page that should own it.
- **C1 answered checklist row** → say where it went, so nothing looks silently deleted.

## 2. Producer write-back — after the audits land, not before

One turn, each owner exactly once:

| finding | goes to |
|---|---|
| needs the owner's word | `OPEN.md` row + its index line (and the next `BLITZ<N>_READY.md` question) |
| work, no decision needed | `TODO.md` top block |
| must be checked by hand | `work/ЧЕКЛИСТ.md` (`CHECKLIST.md`), with a retest item for any fix |
| current truth changed | `HANDOVER.md` + the matching `work/systems/` page + the `knowledge/` page |
| an unrecorded request | a `TODO.md` row or `ДАЛЬНИЙ_ЯЩИК.md` line, quoted with its date |
| history | append `work/agents/WORKLOG.md` |
| detail | a thematic report under `work/agents/reports/` |

Numbering: the next free `OPEN` row is the maximum of the **live index at the top** plus one, not the last line of the file.
Root-inbox hygiene: only the fixed owner pages live in `work/`; anything dated, any report → `work/agents/reports/<topic>/`.
**Move, never delete** (git keeps the history, but a page nobody can find is lost).

## 3. Commands

```
# READ-ONLY and safe
python tools/doc_check.py --run-help      # every command/file/heading the docs name exists
python tools/structure_check.py           # STRUCTURE.md vs git ls-files
python tools/context_index.py fresh       # is the search index current? (build if not)
python tools/digest.py list               # products the owner has not been told about
python tools/unanswered.py list           # questions waiting on him
python tools/commit_review.py status      # is a tenth-commit review due?
python tools/producer.py status           # Run, drainer, mailbox, digest, git
# WRITES
python tools/context_index.py build       # after the pages are rewritten
```

⚠️ Use `git -c core.fsmonitor=false` for status/add/commit when a stale fsmonitor daemon hides changes, and commit exact paths
with `git commit --only` (the index is shared).

## 4. Order that avoids rework

1. Run the read-only commands first and keep their output: they are the evidence.
2. Dispatch the audit wave; read the tables, not the pages.
3. Rewrite the stale owner pages from live truth; route every finding (§2).
4. `python tools/structure_check.py`, `python tools/doc_check.py`, `python tools/context_index.py build`.
5. Verify (§5), then commit. Commit authority is the standing one (`AGENTS.md` law 6); the checkup does not add to it.

## 5. Verification — one adversarial session against your own rewrite

Non-negotiable and cheap relative to what it catches. Use a **fresh session** that did not do the rewrite (a different family
if you have one; otherwise a fresh Sonnet/Opus session — the same-family fallback of the commit review, and say so). Give it
the primary artefacts, forbid citing a rewritten page as evidence for itself, and make it check at minimum:

- every removed checklist row is answered somewhere, or was an **open row that got lost** (worst case);
- a shrunken digest or page lost nothing still open — enumerate the old text's open items and locate each;
- every internal link and named command resolves (`python tools/doc_check.py --run-help`), including pages moved this session;
- no conflict markers (`<<<<<<<`) anywhere; new `OPEN` rows do not duplicate old ones and their numbers are right;
- the «unrecorded request» claims really are absent (`git show <previous>:<file>`), searched with more than one wording;
- re-run every gate and compare against what `HANDOVER.md` now claims.

## 6. Done means

- Owner pages current; zero A1 defects left; the `work/` root holds only the fixed pages.
- Every finding has exactly one owner (register row, checklist row, report or move).
- `doc_check`, `structure_check` and `context_index fresh` all clean — or failing **only** on rows that are open owner decisions
  (say which).
- `HANDOVER.md` rewritten in place, `WORKLOG.md` appended, and one short report to the owner **in his language**: what changed,
  what the verifier refuted, and the questions that need his word.

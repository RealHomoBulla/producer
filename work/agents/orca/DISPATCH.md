# Dispatch — writing a Worker brief, placing Tasks, launching seats

**Open this page before you write a Task spec, choose a worktree, split zones between Workers or launch a seat.**
Which model/effort per seat and the exact launch command lines: [`ROUTING.md`](ROUTING.md).

## Brief template

Write the brief to `.runtime/briefs/<YYYY_MM_DD>/<NAME>.md`; the Orca Task spec is one line:
`<NAME>. Repo root <path>. Read .runtime/briefs/<date>/<NAME>.md IN FULL and execute it exactly.`

Every brief carries, in this order:

1. **Product path FIRST** (law 6b): one named file under `work/agents/reports/<topic>/YYYY_MM_DD_<SEAT>_<SUBJECT>.md`;
   "append after every established fact, before the next command; negative results too".
2. **Context**: the owner's words verbatim with the date, the TODO/OPEN row id, what is already known (with paths).
3. **Action verb and exact deliverable.** The Worker reports and recommends; it never rules an owner decision.
4. **Zone**: the exact paths it may write; everything else is "read freely, never write". Registers (`TODO/OPEN/REPORTS`,
   checklist, HANDOVER) belong to the Producer — a Worker proposes cells in its report.
5. **Shared gate**: `none`, or the one deploy / migration / build / test-server gate it owns.
6. **Verification**: exact commands and expected exit codes; ask it to **predict the number** you will see when you re-run the
   key check. A tool named in the brief is marked **read-the-source** or **run-it** (some `--help` flags write).
7. **Ground rules**: commit only its paths with `git commit --only -m "…" -- <paths>`; no `--amend/reset/checkout` on dirty
   files; absence claims carry the listing; never "normalise" data to make a checker pass — report the conflict.
8. **Finish**: report with the **exact command line in your injected preamble** — `orca orchestration send --type worker_done
   --task-id <task> --dispatch-id <dispatch> --outcome succeeded|failed --subject … --body "<3 sentences: did / found / left>"`
   (plus the preamble's `--from` and capability arguments). Never invent or guess those arguments; there is no `worker-done`
   subcommand. If Orca rejects the report (no capability), print `DELIVERY-FALLBACK: <product path> <commit sha>`.
9. **Gate and impact**: the risk gate (G0–G3, below) and, when it edits a shared contract, the four impact lines.

Specs over 4 096 characters are refused by `worker-start` — keep the spec a pointer to the brief file.

## Batch shape and placement

- Group tasks only when they share a code zone, prerequisites and verification; ≤ 2 measurement-heavy items per Worker.
- Each Worker has one unambiguous owner zone. A follow-up that widens a zone is a new zone claim.
- **The git index is shared.** Nothing stays staged across a tool call; commit exact paths with `--only`; check
  `git diff --numstat -- <path>` for other seats' hunks. "Nothing to commit" while you changed something → `git log -3`:
  someone committed your work.
- **Split diagnostic work by symptom, keep the repair single-owner.**
- During an active wave the Producer does not edit a Worker's files; it integrates after acceptance.
- Independent edits that may collide go to separate Orca worktrees; otherwise the main checkout.
- **Generated or binary artifacts** (a built bundle, a compiled asset, an exported image set) get their own zone and a brief
  that names the ordered inputs, the regeneration command, the source/licence of anything imported and a check on the real
  output files. Where one algorithm exists in two places (client and server, two languages) add a parity test.

## Worked example — one site, three Workers in parallel, no shared files

Objective «marketing site for a smart-home company». The two-Worker limit of one Claude subscription is respected: two run
now, the third waits as a Task.

1. **The Producer writes the seams first** and commits them: `site/index.html` with a placeholder per section and animation
   hooks (`data-anim="hero"`), `site/css/tokens.css` (colours, spacing), `site/content/<section>.md` with one key per text
   slot. These three files are the contract between the zones.
2. **Each file has exactly ONE owner.** Zones in the briefs:

   | Worker | writes (owns) | reads only | product report |
   |---|---|---|---|
   | Layout | `site/index.html`, `site/css/layout.css`, `site/css/tokens.css` | `site/content/`, `site/js/` | `reports/site/YYYY_MM_DD_LAYOUT.md` |
   | Animations | `site/js/animations.js`, `site/css/animations.css` | `site/index.html` (hooks only) | `reports/site/YYYY_MM_DD_ANIMATIONS.md` |
   | Texts | `site/content/*.md` | the brief, `БРИФ.md` | `reports/site/YYYY_MM_DD_TEXTS.md` |

3. **A need that crosses a zone is a message, not an edit**: Animations needs a new hook → it asks the Producer (`orca
   orchestration send --type question`), the Producer assigns the one-line change to Layout.
4. **Commit by exact paths**: `git commit --only -m "…" -- site/js/animations.js site/css/animations.css`. A Worker never runs
   `git add -A` and never touches a path outside its zone.
5. **Integrate after acceptance, as the Producer**: open the page, run the combined check, fix seams in a new commit. A Worker
   does not edit another Worker's output while the wave is live.
6. **With one subscription:** start Layout + Animations first, queue Texts (a Haiku seat is enough) for the first free slot;
   the three Tasks exist from the start so nothing is forgotten.

## Risk gates — decide the gate before you dispatch

| gate | examples | rule |
|---|---|---|
| **G0 read-only** | reading, auditing, measuring, a review | any Worker; no zone; the report is the product |
| **G1 reversible local edit** | editing files in its own zone, a local commit | the Worker commits its zone; the Producer verifies the diff and re-runs the key check before acknowledging |
| **G2 shared or hard to undo** | a shared contract or file format, a dependency/build change, a data migration, mass renames, deleting files | the brief names the contract and the **rollback**; the Producer reads the diff before it lands on `main` (or the work stays in a separate worktree) |
| **G3 outward-facing or irreversible** | publishing or deploying, pushing, sending messages/email, spending money, touching credentials or access, destroying data | **never delegated to a Worker.** The Producer does it only after the owner says so for THIS action; an earlier approval for something else does not extend to it |

Evidence scales with the gate: G0 a file; G1 the diff + one re-run check; G2 + the rollback tried or reasoned; G3 + the owner's
words with the date. A scope the owner already granted (brief §6, a standing law) is respected, not re-asked. A Worker's
completion is a claim: a missing verification, an unanswered Worker question or an accepted-but-unrouted report stays visible in
HANDOVER until it is closed.

## The commit-review brief (every tenth commit)

`python tools/commit_review.py open` writes the reviewer's brief for you (path printed) and names the launch recipe; the
reviewer is **read-only** (gate G0) and writes one answer file with a `VERDICT:` line. Choosing the reviewer:

1. **A different family is better** (Codex, Gemini, DeepSeek … whichever `[review] reviewers` lists and the probes show
   alive). Never the family that wrote the batch if another exists.
2. **One subscription:** Opus and Sonnet are separate families and review each other.
3. **Nothing independent left:** `open` falls back by itself to a **same-family review in a FRESH session** — a new tab with
   none of the authors' context, a different model of the family if possible, else the same model. The brief says so, the
   journal and the verdict mark it «same-family (weaker)». That is the minimum; the gate is never skipped, and it is not a
   reason to stop looking for a second family.

## Change-impact lines — in every brief that edits a shared contract

The Worker reports these four lines (the Producer checks them against `grep`): **contract touched** (function, file format,
config key, page structure) · **direct consumers** (the grep) · **tests that cover the consumer path** · **not covered**
(checked by hand, or not at all). A check that was not run is never reported as a pass.

## Launching a seat

1. `orca orchestration task-create --run <run> --task-title <NAME> --spec "<one line>" --json` → task id.
2. **Reap first, then launch with proof** (same turn — a wave without the reaper piles up tabs and RAM):
   `python tools/worker.py reap --apply` closes this Run's settled Worker tabs, then
   `python tools/worker.py launch --task <task> --route <roster name> [--product <report path>]`.
   It refuses when the machine is over a `producer.toml [limits]` limit («Machine busy … close finished tabs, do not open
   new» — close what is finished, do not open new), creates the tab with the `[[roster]]` command from `ROUTING.md`, waits
   for the TUI, runs `dispatch --inject` and **proves receipt**: the brief is not stuck in the composer
   (`[Pasted Content N chars]`) and the screen is busy, the task id is in a fresh Claude transcript, or `--product` changed. A
   stuck paste gets one Enter, a paste that never arrived gets one short re-injection; otherwise it prints
   `LAUNCH FAILED: <reason>` (exit 2) and leaves the tab open — read it (`orca terminal read --terminal <h> --screen`), fix or
   close it, re-run. A banner or `dispatched` is not proof. Outcomes are logged in `.runtime/worker-launches.jsonl`.
3. While waiting, watch report-file mtimes: a Worker hit by «Connection lost mid-response» idles silently — wake it with
   `orca terminal send --terminal <h> --text "continue" --enter`.
4. **After the delivery** (verified, routed, acknowledged): `python tools/worker.py close --terminal <h>` — it closes only a
   settled Worker (task completed/failed, or `--product` committed and the tab idle), never the Producer, Blitz or your own
   tab; then refill the seat if the roster says so and the machine allows.

Doing it by hand (`orca terminal create` → wait → `dispatch --run … --task … --to <h> --inject`) is the fallback when
`worker.py` cannot run; then you own the receipt check yourself. `worker-start` only works for agents Orca recognises;
`dispatch --inject` works for any TUI. A Worker tab that opened empty has no dispatch capability: it reports by commit and a
`status` message, you close it with `worker.py close --product <path>` and settle its Dispatch yourself.
Details, flags and failure modes: `work/agents/knowledge/GUARDIAN.md` §7a–7c.

Cheap models for mechanical listings, strong ones for judgement; a cross-family model attacks a design (adversarial review).

## Weaker-seat addendum — paste it into the brief of a free or small model

```text
WEAKER-SEAT ADDENDUM — known failure modes; obey each:
A1. COMMIT YOUR PRODUCT YOURSELF, by exact path. Say the commit id in your final message (`git log -1 --format=%h -- <product path>`); a product that is not committed is not delivered.
A2. Report with the exact command in your preamble. If Orca rejects it (no capability), do not retry: print `DELIVERY-FALLBACK: <product path> <commit id>` and stop.
A3. NEVER change data just to make a checker in the brief pass. If a check disagrees with the data, the checker is suspect: stop, prove the real format, write the conflict in the product.
A4. Stay inside the owned zone to the letter: if a tool rewrites files outside it, restore them and report it; ask before widening.
A5. Every claim of absence carries its listing; every number carries the command that produced it. Your work is reviewed by another model.
```

**Free-model briefs** demand `git commit -m "…"` (without `-m` an editor holds `.git/index.lock`).
**Codex tabs after a limit** show a model-switch dialog: send `3`, Enter, then the text, then Enter separately.

## Blitz Worker brief (owner decision session — see the «блиц» row of `OWNER_SHORTHANDS.md`)

```text
# BLITZ #<N> — interactive owner decision blitz (tab `Blitz`)
You are the blitz Worker. The owner answers here, at his own pace. Talk to him in his language (producer.toml owner_language).
Text rule: each question = 2–4 short lines of problem (plain words, numbers only when they decide), then options a / b / c
one line each with its consequence, then your recommendation with one reason. No paths, ids, shas. Read the source BEFORE asking.
Queue: work/agents/registers/BLITZ<N>_READY.md (made by `python tools/blitz.py new`; its «Сверка» table is the cross-check of the last blitz), Q1… in order; skip any already answered (grep OPEN.md and OWNER_LAST_MESSAGES.md).
Your ONE writable file: work/agents/registers/BLITZ<N>_ACTIVE.md — per question `## Qn — asked <UTC> (<title>)`, `Options: …`,
`Answer (<UTC>): «<verbatim>»`; commit after each answer with `git commit --only`. ONE question at a time.
His «сделай …» here = a request to the Producer: record it verbatim under the current question. No other change while open.
On «закрыть блиц»: closure section (every answer verbatim, every unanswered Q), commit, worker-done (or DELIVERY-FALLBACK). You never route.
```

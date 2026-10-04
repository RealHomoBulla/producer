# Autonomy, night watch, rotation

**Open this page when the owner says «автономно» / «ночная вахта», when a seat must rotate, or before he goes to sleep.**

## 1. Persistent autonomy

- Autonomy ON means: keep working across handoffs until he says «стоп автономно». A question or status request from him is not
  a stop signal — answer it and resume. A declined tool call is not a stop signal either.
- ⚖️ **A turn that answers him also acts** — dispatch, accept or write back in the same turn; a busy DAG masks an idle
  coordinator.
- Under autonomy the wave is **his last stated roster** (`producer.toml` `[[roster]]`): compare per seat, launch/close to fix drift,
  replace each settled Worker with the same seat; report a seat you cannot fill — never substitute silently.
- Owner away: safe work, then bounded work; no owner-gated decisions (they wait in `OPEN.md` / the next blitz READY file).
- Quota: burn every window to 100 % (law 4a); when one route is exhausted, move to the next live one in `ROUTING.md`.

## 2. Watchdog (Guardian)

The skeleton carries a small Guardian: `tools/guardian.py` — one long-lived process, stdlib-only, for any project and any agent
CLI. Full how-to with recipes: [`GUARDIAN.md`](../knowledge/GUARDIAN.md). It is NOT the source project's ~24k-line
`orca_orchestrator.py`.

**Autonomy OFF** (`python tools/guardian.py start` on a fresh clone): if no Producer tab of THIS project exists it opens exactly
one (the first route in `[[producer_routes]]`, no permission-bypass flags) and types the bootstrap prompt; with an empty
`work/БРИФ.md` the prompt says to run the kickoff first (`START_PROMPT.md` §0a accounts, §0 brief). It does nothing else, and it
does not relaunch a Producer the owner closed.

**Autonomy ON** adds the night behaviours, every `interval_seconds`:

1. **Ownership.** Acts only on tabs of this project's worktree and on the one Producer pane it bound (handle + incarnation,
   kept in `.runtime/guardian-state.json`). The owner's `Blitz` tab, other projects' tabs and any foreign Producer are never
   adopted, typed into or closed. An unreadable terminal list is «unverifiable»: nothing is launched or closed.
2. **Relaunch a dead Producer** (missing for `absent_confirm_ticks` good reads, or the agent left a surviving shell) on the
   best live route, with the same bootstrap prompt plus «previous seat died; resume from HANDOVER». The brief is confirmed on a
   fresh screen; an unconfirmed launch is retried and then reported, never counted as a launch.
3. **Nudge an idle Producer** (screen unchanged `idle_nudge_minutes`) — only at a safe prompt: not while busy (unless frozen for
   `busy_stuck_minutes`), not over a composer draft. The same nudge names roster seats short of `count` and idle Workers.
   The Guardian never launches Workers itself.
4. **Rotate the seat** at `seat_max_hours`: types `ROTATE NOW`; the successor is launched only once HANDOVER is rewritten **and
   committed** (and the checkpoint refreshed), and the old tab is closed only after the successor confirmed its brief. A stalled
   rotation is a visible incident; nothing is hard-closed.
5. **Routes.** `[[producer_routes]]` is an ordered list of Producer launch lines. A limit message on the Producer screen, or a
   dead Producer whose route probe fails, hands the seat to the next live route (brief: «previous seat hit its limit; resume
   from HANDOVER»); a recovered higher route (≥ 30 min, probe ok) is returned to at the next rotation.
   `python tools/guardian.py routes` lists them with probe status; `route --primary <name>` reorders.
6. **Alarm.** A limit message on the Producer or any Worker: the reset time is parsed (`resets 7pm`, `try again at …`,
   `in 2h 15m`; fallback retry every 30 min), the seat is marked sleeping in `.runtime/guardian.json`, and at reset + 2 min it is
   woken (`continue — the limit reset; re-read HANDOVER and resume`; a dead Producer tab is relaunched). With no other route this
   alarm IS the fallback.
7. **Known dialogs** on agent panes: Codex model switch → the «keep current» option (never a blind digit), «Connection lost» →
   `continue`, both with bounded retries and backoff.

```
python tools/guardian.py autonomy on --objective "build the site, work all night"   # .runtime/autonomy.json
python tools/guardian.py start        # detached supervise loop; OS lock = single instance; readiness handshake
python tools/guardian.py status       # running / STALE / DEGRADED, route, sleeping seats, incidents, last actions
python tools/guardian.py routes       # Producer routes + probe status
python tools/guardian.py stop         # graceful, then verified kill
python tools/guardian.py autonomy off # night behaviours stop; the Producer finishes its turn
```

**Stopping autonomy** («стоп автономно») = `python tools/guardian.py autonomy off` — without it the Guardian keeps nudging and
relaunching. Every action is rate-limited and appended to `.runtime/guardian.log` (rotated); the heartbeat is
`.runtime/guardian.json`. The Producer still owns its own write-back: the Guardian only types `ROTATE NOW` / nudges / wake lines
and re-launches.

The old manual rules still hold when no Guardian is running:
- The Producer keeps its own rotation clock (`START_PROMPT.md` §1.5) and hands over by itself.
- Before the owner sleeps: HANDOVER `# NOW` current, checkpoint saved, drainer alive, rotation alarm armed, and a line in
  HANDOVER saying how to restart the Producer if the tab dies.
- On Windows, any long-lived helper must be started detached; a process started from a tool's shell can die with that shell.
  `guardian.py start` detaches for you, but test survival in a disposable workspace before trusting a whole night.

## 3. Night watch checklist (before he sleeps)

1. Roster for the night set in `producer.toml` `[[roster]]` (his words and the date as a comment above the seat).
2. TODO top block holds enough executable rows for the night (no owner-gated ones).
3. Drainer alive (`python tools/producer.py status`), `python tools/guardian.py status` says `running` (not STALE/DEGRADED),
   `guardian.py routes` shows at least one live route, HANDOVER committed.
4. Machine limits: keep load and RAM inside `MACHINE_TRAPS.md` §Load; close finished tabs at once.
5. Morning: the digest holds every product of the night in short Russian; open questions are in the next blitz READY file.

## 4. Rotation

- Seat age: Claude +3 h, other routes +2 h (`START_PROMPT.md` §1.5), or ~90 % context, whichever first.
- Order: finish accepted write-back → HANDOVER `# NOW` (gen N+1 note, Run, live Workers → products, cursor, next action) →
  checkpoint → **commit** (the Guardian waits for the committed HANDOVER) → start the successor (Guardian, or the owner) → the
  old seat stops dispatching and ends its turn; the Guardian closes it only after the successor confirmed its brief.
- The successor appends the old `## Previous generation` to `WORKLOG.md`, demotes the inherited NOW to `## Previous
  generation`, writes a fresh NOW, restarts the drainer from its own shell.

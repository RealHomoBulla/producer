# Guardian — how to run it, change it, and fix it

**Open this page when the owner (or a friend) says «добавь бекап-продюсера <модель>», «поменяй ростер», «перезапусти гардиана»,
or when the Guardian does something odd.** Every recipe is: exact steps → the exact `producer.toml` snippet → the command that
proves it worked. The tool is `tools/guardian.py`; its settings are `producer.toml` `[guardian]`, `[[producer_routes]]`,
`[[roster]]`. Nothing here needs more than Python 3.12 and the Orca CLI.

## What it does (one paragraph)

`python tools/guardian.py start` opens **one** Producer tab in Orca if this project has none — even with autonomy OFF; with an
empty `work/БРИФ.md` the bootstrap prompt says «do the kickoff first» (`START_PROMPT.md` §0a accounts, then §0 brief). With
autonomy ON it also keeps the night going: nudges an idle Producer, names missing roster seats, rotates the seat at
`seat_max_hours`, hands the seat to the next **route** when a limit message shows, and — when no other route exists — parks the
seat and **wakes it at the reset time** (the alarm). It only touches tabs of THIS project's worktree, never the owner's `Blitz`
tab, and never closes a Producer before its `HANDOVER` is committed and the successor has the brief.

## 1. Start, stop, status

| goal | command | proof |
|---|---|---|
| start (idempotent, single instance) | `python tools/guardian.py start` | prints `guardian started: pid N` or `already running` |
| night mode on / off | `python tools/guardian.py autonomy on --objective "…"` / `autonomy off` | `python tools/guardian.py autonomy status` |
| health | `python tools/guardian.py status` | `Guardian: running`; `STALE` = alive but not ticking; `DEGRADED` = tick errors; `terminal list: UNVERIFIABLE` = Orca unreachable (nothing is touched) |
| stop | `python tools/guardian.py stop` | `guardian stopped` (graceful first, verified kill second; refuses to kill a pid that is not the running instance) |
| restart after a crash/reboot | `python tools/guardian.py service` prints a Task Scheduler / systemd recipe (`start` every 5 min; the OS lock makes it safe) | `status` after the next tick |

Files in `.runtime/` (gitignored): `guardian.log` (rotates to `.1`, `.2`), `guardian.json` (heartbeat: route, sleeping seats,
incidents), `guardian-state.json` (the bound Producer pane, rotation phase, route limits — it survives restarts),
`guardian.lock`, `guardian.pid`, `autonomy.json`.

## 2. Add a backup Producer route — worked example: Codex Sol as route 2

1. Open `producer.toml`. Routes are tried **top to bottom**; position = priority.
2. Paste this block **between** the `claude-opus` and `codex-luna` blocks (that makes it route 2):

```toml
[[producer_routes]]
name = "codex-sol"
title = "Producer Codex Sol"
agent = "codex"
command = "codex -m gpt-6.1-sol -c model_reasoning_effort=medium"
limit_patterns = ['usage limit', 'try again at', 'rate limit', '\b429\b']
probe = "codex --version"
```

3. Verify: `python tools/guardian.py routes` — the new route is listed second with `probe: alive`. A running Guardian reloads
   `producer.toml` by itself (a broken edit is refused and the last good config stays; see `status`).

Rules for any new route: `name` unique; `command` = the exact launch line from `ROUTING.md` (never put a key in it — use
`keys = true` and put the key in `~/.config/producer/keys.env`); `agent` = `claude` / `codex` / `opencode` (what Orca reports for
the pane; a surviving shell without it counts as a dead Producer); `limit_patterns` = regexes for that CLI's limit message;
`probe` = a command whose exit code 0 means «alive» (liveness, not quota). A route that needs env variables
(`ANTHROPIC_BASE_URL=…`, `$OPENCODE_API_KEY`) must set `keys = true` and, on Windows, `shell_windows = "git-bash"`.
`args_unattended = "--dangerously-skip-permissions"` is appended **only while autonomy is ON** — delete that line if you want
permission prompts to stay (an unattended Producer then stops at the first prompt).

## 3. Reorder or disable a route

```
python tools/guardian.py route --primary codex-luna     # move to the front (comments and other sections are kept)
python tools/guardian.py route --disable opencode-free  # enabled = false
python tools/guardian.py route --enable  opencode-free
python tools/guardian.py routes                         # proof (add --no-probe to skip the probes)
```

The live Producer is not moved at once: the new order applies at the next rotation, limit or death. To move it now, run
`autonomy on`, wait for `ROTATE NOW`, or close the Producer tab with autonomy OFF and run `start`.

## 4. Add or fix a limit pattern

1. Get the exact text the CLI shows: `orca terminal read --terminal <handle> --screen --json` (or look at the tab).
2. Add a regex (single quotes in TOML = no escaping) to that route's `limit_patterns`: `limit_patterns = ['usage limit', 'plan limit']`.
3. Check it matches: `python -c "import re,sys; print(bool(re.search(r'plan limit', sys.stdin.read(), re.I)))" < screen.txt`.
4. A pattern is matched on the **last 12 lines** of the Producer screen, only when the Producer is not busy, and on
   `limit_confirm_ticks` (default 2) ticks in a row — so a Producer *talking about* a rate limit does not trigger a switch.

## 5. One Claude subscription (the friend's setup)

One $20 Claude plan = **one 5-hour window shared by every seat** (Producer and all Workers). Extra seats do not add quota — they
drain the same window faster. So: keep the roster small (**Producer + 2 Workers**) and let the alarm carry the night.

```
python tools/guardian.py preset single-claude            # print it
python tools/guardian.py preset single-claude --apply    # replace [[producer_routes]] + [[roster]] in producer.toml
```

That gives one route (`claude-sonnet`: Producer Sonnet 5.5, medium) and two roster seats (Sonnet 1, Haiku 1). When Claude shows
`5-hour limit reached ∙ resets 7pm`, the Guardian marks the seat sleeping (`status` shows `sleeping: … wakes <time>`) and, at
reset + 2 min, types `continue — the limit reset; re-read HANDOVER and resume` into the Producer (or relaunches it with the
bootstrap if the tab died), then nudges sleeping Workers the same way. If the reset time cannot be read it retries every 30 min.
Never give up the night: the alarm is the fallback when no other route is configured.

Alarm settings (`[guardian]`): `wake_delay_seconds = 120`, `sleep_retry_minutes = 30`, `timezone` (defaults to `[project]
timezone`; used when the message names no zone). Understood messages: `resets 7pm`, `resets at 7:30 AM (Europe/Berlin)`,
`try again at Oct 7th, 2026 4:03 PM`, `resets in 2h 15m`, ISO timestamps.

## 6. Add, change or switch off a roster seat

```toml
[[roster]]
name = "haiku"            # shown in nudges
title_prefix = "Haiku"    # live tabs whose title contains this count toward the seat
count = 1                 # 0 = switched off (not «wanted 1»)
command = "claude --model claude-haiku-4-5-20251001"
```

The Guardian only counts and reminds; the Producer launches the seat. Proof: with autonomy ON and a seat short, the next
nudge names it (`GUARDIAN: … Roster short: haiku 0/1`). Seats are counted among this project's agent panes only.

## 7. Change idle and rotation thresholds (`[guardian]`)

| key | default | meaning |
|---|---|---|
| `interval_seconds` | 60 | tick period (min 5) |
| `idle_nudge_minutes` | 10 | Producer screen unchanged this long → nudge (never while it shows a spinner / `esc to interrupt`) |
| `worker_idle_minutes` | 15 | a Worker unchanged this long → named in the nudge |
| `busy_stuck_minutes` | 30 | a «busy» screen frozen this long is nudged anyway |
| `seat_max_hours` | 3 | seat age → `ROTATE NOW`; the successor starts only after HANDOVER is committed |
| `rotate_ask_minutes` / `rotate_max_asks` | 15 / 3 | re-ask rate; after the last ask a visible incident, never a hard close |
| `absent_confirm_ticks` | 2 | a missing Producer must be missing this many good reads in a row |
| `recover_minutes` | 30 | a limited route is retried after this (or at its parsed reset time) |
| `ignore_titles` | `["Blitz"]` | the owner's own tabs: never counted, typed into or closed |

Proof: `python tools/guardian.py status` shows `config:` lines for any value it had to correct.

## 7a. Machine limits — the brake on Worker tabs (`[limits]`)

```toml
[limits]
max_workers = 4          # live Worker tabs of this project; Producer / Blitz / the owner's tabs are not counted
min_free_ram_gb = 2.0    # available RAM below this -> busy
max_load = 85            # whole-machine CPU % above this -> busy      (0 = that limit is off)
```

The numbers belong to THIS machine — measure first: `python tools/worker.py load` prints the limits, the live reading
(Windows: kernel available RAM + CPU load from CIM through PowerShell, no `wmic`; Linux: `MemAvailable` and two `/proc/stat`
samples) and `ok` / `BUSY - <reasons>`. A signal that cannot be read is a note (`incident-machine-unmeasured`), never a block.

What happens above a limit:

- `python tools/worker.py launch …` **refuses** and opens no tab (exit 3).
- The Guardian's nudge to the Producer says **«Machine busy (free RAM 1.2 GB < 2 GB) — close finished tabs, do not open new.»**
  RAM/CPU pressure alone triggers that nudge (at most once per `idle_nudge_minutes`); sitting exactly at `max_workers` is not
  a problem, so it only suppresses «Roster short» and stays quiet. `status` shows `machine: BUSY - …`.
- The Producer's own seat is never blocked: a dead Producer is always relaunched, whatever the load.

Proof: `python -m pytest -q tools/tests/test_guardian_limits_rc.py -p no:cacheprovider --basetemp <tmp>`.

## 7b. Remote Control for the owner's tabs (`[remote_control]`, off by default)

```toml
[remote_control]
enabled = true
titles = ["Producer", "Blitz"]     # a tab whose title is, or starts with, one of these; the bound Producer always counts
```

With `enabled = true` the Guardian types `/remote-control` + Enter **once** into each such Claude Code tab and records it in
`guardian-state.json` (`status` lists `remote control: <handle> … typed|on|unverified|skipped-api-route`). The owner then sees
that session in the Claude app (Code tab → the running session) and can drive it from the phone. Rules it keeps:

- For the Producer it types before the bootstrap brief (a working Producer is never at a free prompt later); every other
  matching tab, including the owner's Blitz, is handled at the next tick that finds a free composer. It works with autonomy OFF.
- **Once per pane incarnation.** On a connected pane a second `/remote-control` opens the Disconnect menu over the owner's
  chat, so it is never repeated. A status bar that shows background work (`1 shell, 1 monitor`) hides `/rc`: that is
  «unknown», not «off», and nothing is typed. Text in the composer, a busy screen or a non-Claude tab is left alone.
- `typed` becomes `on` when the status bar ends with `/rc`. If it never does within `verify_seconds` (default 120) the
  state is `unverified` and `incident-remote-control-unverified` tells the owner to type `/remote-control` himself.
- It needs the owner's claude.ai login: a Producer route with `keys = true` (API key / proxy) is `skipped-api-route`.

## 7c. Worker tabs with proof: `tools/worker.py`

The Guardian only counts and reminds; the Producer launches. `worker.py` is the one way to do it (DISPATCH.md §Launching a seat):

| goal | command | what it guarantees |
|---|---|---|
| open a Worker on a Task | `python tools/worker.py launch --task <id> --route <roster name> [--product <path>]` | refuses over a `[limits]` limit; creates the tab with the `[[roster]]` command (`keys`, `shell_windows` honoured); `dispatch --inject`; then **proves receipt** — the brief is not in the composer (`[Pasted Content …]`) AND the screen is busy / the task id is in a fresh Claude transcript / `--product` changed / the screen changed. A stuck paste gets one Enter, a paste that never came gets one short re-injection, then `LAUNCH FAILED: <reason>` (exit 2) and the tab stays open. Every outcome is a line in `.runtime/worker-launches.jsonl`. |
| close one finished Worker | `python tools/worker.py close --terminal <h> [--product <path>]` | only a tab whose Orca task is completed / failed / cancelled, or whose `--product` is committed, clean and idle; always `--tab`. Refuses the bound Producer, `producer-supervisor.json`'s `activeHandle`, any tab that created tasks in the Run, Blitz and Producer titles, `ignore_titles`, and the tab it is run from. |
| list / close all finished | `python tools/worker.py reap [--apply]` | dry by default; every close goes through the same checks. Run it in the same turn you open new Workers, and refill the seat after a delivery. |
| what do the limits say | `python tools/worker.py load` | exit 0 ok, 3 busy |

A tab with no Orca task assigned is never reaped (title alone proves nothing). A Worker that opened empty has no dispatch
capability and cannot call `worker_done`: close it with `--product`, and settle its Dispatch by hand.
Proof: `python -m pytest -q tools/tests/test_worker.py -p no:cacheprovider --basetemp <tmp>`.

## 8. Read the log

`.runtime/guardian.log`, one line per action: `<time> <kind> <handle> <detail>`.

| kind | meaning |
|---|---|
| `launch` / `bootstrap-verified` / `bootstrap-unverified` | a Producer tab opened / its brief was seen on a fresh screen / not yet |
| `adopt` | an existing single titled pane of this project became the Producer |
| `nudge`, `dialog-*` | a reminder / a known dialog fixed (bounded retries) |
| `rotate-now`, `rotate-close` | seat age reached / old seat closed after successor + committed HANDOVER |
| `remote-control-typed`, `remote-control-on` | `/remote-control` typed once into an opted-in tab / its status bar then showed `/rc` |
| `limit`, `sleep`, `wake`, `route-recovered` | limit seen, seat parked until reset, woken, higher route usable again |
| `incident-*` | something the Guardian will not guess about — see §9 |

## 9. When the Guardian misbehaves

| symptom | do |
|---|---|
| `status`: `terminal list: UNVERIFIABLE` | Orca is closed/unreachable. Nothing is touched by design. Open Orca; it recovers by itself. |
| `incident-ambiguous-producer` | two tabs of this project have «Producer» in the title. Rename or close one; the Guardian adopts the single one. |
| `incident-bootstrap-failed` | the brief never showed in the tab. Type the brief into it yourself (`bootstrap_prompt` in `[guardian]`), then `autonomy status`. |
| `incident-rotation-stalled` / `incident-close-deferred` | the old Producer did not commit HANDOVER, so nothing was closed. Ask it to write and commit HANDOVER (or do it), the Guardian continues. |
| `incident-no-live-route` | every route is limited, probe-failed or disabled. `python tools/guardian.py routes`; fix a key or wait for the reset. |
| `incident-remote-control-unverified` | `/remote-control` was typed but `/rc` never showed. Open that tab and type `/remote-control` yourself; the Guardian never retypes it. |
| `incident-machine-unmeasured` | the RAM/CPU reading failed (PowerShell CIM or `/proc`); limits on that signal do nothing until it reads. Run `python tools/worker.py load` to see the error. |
| `status`: `STALE` | process alive but hung: `stop`, then `start`. |
| `stop` says it cannot stop | the pid file does not match the running instance; find the `python … guardian.py supervise` process in the OS task list and end it, then `start`. |
| it keeps relaunching / wrong tab bound | `autonomy off`, `stop`, delete `.runtime/guardian-state.json` (forgets the bound pane; the next `start` re-adopts a single titled pane or opens one), `start`. |
| autonomy OFF and the Producer was closed by the owner | by design it is **not** relaunched (autonomy on = relaunch). Run `start` after closing everything to get a fresh one only if no Producer was ever bound — otherwise delete `guardian-state.json` as above. |

## 10. Test a change with the fake Orca

```
python -m pytest -q tools/tests/test_guardian*.py tools/tests/test_worker.py -p no:cacheprovider --basetemp <a temp dir of yours>
```

`tools/tests/test_guardian.py` holds the fake Orca (`FakeOrca`: terminal list/read/create/send/close, rows with
`worktreePath`, `incarnationId`, `agentIdentity`; text only reaches the transcript on Enter) and `harness(...)`, which builds a
`Guardian` with a fake clock, fake probes and a fake git. A new behaviour = one test: build the harness, `fake.add("Producer",
screen="…")`, call `guardian_obj.tick(now=…)` at chosen moments, assert on `fake.calls`. Never test against the real Orca and
never start a live `supervise` loop to try a change — it would type into real tabs.

## 11. What is not proven

- That a detached Guardian survives the launching tool's shell closing on Windows (the author lost a night to this once).
  `start` leaves the tool's job object when allowed and falls back to plain detached otherwise; test it in a disposable
  workspace before trusting a whole night, and use `guardian.py service` for restart-after-crash.
- Remote Control detection reads Claude Code's status bar (`/rc` at its end, `bypass permissions` / `for shortcuts` line, the
  `Disconnect this session` menu) as measured by the skeleton author in 2026-10; a Claude Code release that changes that text
  makes the state `unverified` (reported, never retyped) until `rc_is_on` in `tools/guardian.py` is updated.
- The receipt proof reads the composer draft that `orca terminal read` reports; a host that never reports `draft` falls back
  to the last four screen lines for the `[Pasted …]` marker.
- Quota meters: routes are judged by a probe (liveness) and by the limit message on screen, not by a live percentage.

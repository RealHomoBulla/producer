# Model routing

This page records generic launch forms, quality calibration, cost shape, and credential locations. Availability changes: probe the chosen route before launching and read its visible model identity after startup. Do not treat a listed route or an old quota reading as proof that it works now.

## Your preferences

_Empty until the owner states them (his words + date). Typical lines: which models are allowed or banned, default effort per
seat, whether a free model may be a reviewer, how many seats this machine's RAM/CPU can carry. Nothing below is a rule until it
is written here: the tables describe what exists and what was measured, not what you may use._

The one default that is NOT a preference: **never pass a permission-bypass flag unless the owner chose it** (`python
tools/setup.py --unattended`, or `args_unattended` in `producer.toml`). Launch forms below omit those flags on purpose.

## Routes, launch forms, and measured fit

**Read the verdicts as dated calibration from one account (2026-10), not as policy.** Re-probe on yours before relying on
them. Lines with `ENV=value cmd` need a POSIX shell: on Windows run them in Git Bash (`shell_windows = "git-bash"` in a
Producer route) or translate to `$env:ENV="value"; cmd` in PowerShell. Variables from the keys file must be **exported** to be
seen by child processes (`set -a; . "$KEYS"; set +a`).

Commands below are generic CLI forms; use the current Orca task/terminal binding workflow around them. Always pass an explicit model and provider-supported effort.

| Route | Launch form | Measured fit and limits |
|---|---|---|
| Claude Opus 5.5 | `claude --model claude-opus-5-5 --effort high` | Owner-approved Producer family; valued for synthesis, code, and design. Worker default is medium; high is for an authorized depth gate. Claude has separate rolling 5-hour and weekly windows. |
| Claude Sonnet 5.5 | `claude --model claude-sonnet-5-5 --effort medium` | Bounded implementation, cross-checks, and blitz work. Use the full model id; the short `sonnet` alias may resolve to an older model. Shares Claude's 5-hour and weekly windows. |
| Codex Luna Max | `codex -m gpt-6-luna -c model_reasoning_effort=max` | Strong commit gate and also a capable general Worker. Use a different model family to review its code. Codex has separate 5-hour and weekly windows; its subscription behaves like a 1× plan, so long Sol runs consume the short window quickly. |
| Codex Sol 6.1 | `codex -m gpt-6.1-sol -c model_reasoning_effort=medium` | Owner-authorized for selected demanding work; use medium by default because it burns the shared 5-hour Codex window quickly. Never change this to `gpt-6-sol`. |
| OpenCode free models | `opencode --model opencode/space-bunny-free` (also `opencode run -m <model> "Reply with exactly: ok"`) | Space Bunny was measured at 23/25 claims with no invented identifiers and did well on bounded evidence, bytecode reading, and small code/test tasks. Known misses: may not commit, worker completion can lack capability, follows a flawed checker literally, and can stall on long loops. Muse 1.3 and MiMo 2.6 were calibrated as review-capable; Muse 1.2, Nemotron Ultra, and Big Pickle missed known review defects and are mechanical-only. |
| OpenCode Go: DeepSeek 4.1 Flash | `CLAUDE_CODE_SIMPLE=1 env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL=https://opencode.ai/zen/go ANTHROPIC_AUTH_TOKEN="$OPENCODE_API_KEY" claude --model opencode-go/deepseek-v4.1-flash --effort medium` | Owner describes DeepSeek as an excellent Worker; use bounded implementation/evidence and the current approved review gate. DeepSeek as a native CLI Producer is avoided. Go has a rolling 5-hour window plus a monthly ceiling. |
| OpenCode Go: Muse Contributor 1.3 | `OPENCODE_API_KEY=… opencode --model opencode-go/muse-spark-1.3-contributor` | Current owner routing uses Muse as a commit-review option when Codex/Claude capacity is low. Keep it on the native OpenCode Go route; do not silently switch harnesses. Go has a rolling 5-hour window plus a monthly ceiling. |
| AGY / Gemini 3.8 Flash High | `agy --model gemini-3.8-flash-high --effort high --mode accept-edits --print "<prompt>" --output-format json --print-timeout 4m` | Fast lookups, surface sweeps, and image generation. A measured commit-review sweep found issues in only 6/52 batches (12%); never use it as a reviewer or source of facts. No reliable quota meter; inspect the provider UI and run a smoke probe. AGY is not the separate `gemini` CLI. |
| OpenRouter / DeepSeek 4.1 Flash | `CLAUDE_CODE_SIMPLE=1 env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL=https://openrouter.ai/api ANTHROPIC_AUTH_TOKEN="$OPENROUTER_API_KEY" CLAUDE_CODE_MAX_CONTEXT_TOKENS=1048576 claude --model deepseek/deepseek-v4.1-flash --effort medium` | Paid direct API route for bounded work. OpenRouter accepts the Anthropic protocol; use the API's full model id. This route has a per-key spending cap, so check remaining credit first and stop at zero. OpenCode does not provide an `openrouter` model provider. |
| Command Code | `COMMAND_CODE_API_KEY=… cmd --model deepseek/deepseek-v4.1-flash` (add `--trust --skip-onboarding --no-auto-update` for an unattended seat) | Separate small shared budget, not interchangeable with OpenCode Go. On Windows `cmd` is the system shell, NOT Command Code: check `where cmd` and `cmd --version` before counting this seat. Historically intermittent; use only after a live probe. Claude models must never run through this CLI. |
| DeepSeek platform direct | `CLAUDE_CODE_SIMPLE=1 env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" claude --model deepseek-flash --effort medium` | A separate API balance and route from OpenCode Go and OpenRouter. The provider lists its allowed flash tier as `deepseek-flash`; balance can reach zero. Probe with `curl -s https://api.deepseek.com/user/balance -H "Authorization: Bearer $DEEPSEEK_API_KEY"` and follow with a small inference smoke test. The exact client/harness must match the current route configuration. |

### Weaker and free seats

Free and small models are good at bounded evidence gathering and mechanical edits and miss things on judgement. The safeguards
(commit your own product, never bend data to satisfy a checker, stay in the zone, cite the command behind every number) are the
**weaker-seat addendum** in [`DISPATCH.md`](DISPATCH.md) — paste it into those briefs.

## Probes and availability

- **OpenCode free:** `opencode run -m opencode/<id> "Reply with exactly: ok"`. A successful answer proves that model route at that moment. On a limit error, try the other configured account only if it is a separately authorized account; re-probe hourly after every configured free route is limited.
- **OpenCode Go:** `curl -sS https://opencode.ai/zen/go/v1/messages -H "x-api-key: $OPENCODE_API_KEY" -H "anthropic-version: 2023-06-01" -H "x-opencode-session: probe" -H "content-type: application/json" -d '{"model":"deepseek-v4.1-flash","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'`. HTTP 200 proves a response; 429 identifies the window; 400 `MissingSessionID` means authentication was accepted but the probe omitted session state.
- **AGY:** `agy --model gemini-3.8-flash-high --output-format json --print="ok" </dev/null`. A returned response proves liveness; a binary-present status or countdown does not prove quota.
- **OpenRouter:** `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/models`. For usable model access, send a small chat completion to `/api/v1/chat/completions` and require an answer, not merely HTTP success.
- **Command Code / direct API:** run the provider's one-line smoke prompt with the intended model and inspect the response. A configured key or a status badge is not a probe.
- **All routes:** check the live quota tool and the actual terminal/model identity before counting a seat. A visible 429 overrides a stale meter. Never print a credential while checking it.

## Cost and quota shape (snapshot as of 2026-10-04; re-probe)

- Claude and Codex each have distinct 5-hour and weekly windows. Use the provider's live meter; model effort changes burn rate. Claude weekly capacity was being conserved, while the Codex 5-hour window is known to drain quickly under Sol.
- OpenCode Go has a rolling 5-hour window and a monthly window. The short window can clear before the monthly one; 429 response metadata names the limiting window. On 2026-10-04 the owner reported OpenCode capacity plentiful and Claude/Codex capacity low; no exact percentage is durable.
- Space Bunny's free period was reported extended through 2026-10-05. Re-probe on and after that date. Free endpoints publish no dependable quota count; learn a limit by probing and re-probe hourly after all configured free models are limited.
- OpenRouter and DeepSeek direct are balance-based; OpenRouter also had a per-key spend cap. Command Code uses a small shared budget. AGY's remaining quota is visible only through its provider UI. Read current usage before choosing work.

## Producer routes (what the Guardian launches)

`producer.toml` `[[producer_routes]]` is the **ordered** list of ways to run the Producer; the Guardian uses the first live one
and moves down the list on a limit message or a dead seat (details: `AUTONOMY.md` §2, recipes: `knowledge/GUARDIAN.md`). Each
route copies a launch form from the table above — keep them identical so this page and the config never drift.

| route name | table row it copies | Producer fit |
|---|---|---|
| `claude-opus` | Claude Opus 5.5 | the author's default Producer |
| `codex-luna` | Codex Luna Max | different family and window; first fallback |
| `claude-opencode-go-deepseek` | OpenCode Go: DeepSeek 4.1 Flash | Claude Code re-pointed at OpenCode Go (not the native OpenCode CLI) |
| `claude-openrouter-deepseek` | OpenRouter / DeepSeek 4.1 Flash | Claude Code re-pointed at OpenRouter; balance-based |
| `opencode-free` | OpenCode free models | last resort; free tier limits are unpublished, so it can vanish |

A native OpenCode DeepSeek is **not** a Producer route (owner preference above); the free route above is the only native
OpenCode Producer, and only as the last resort. Keys are read from `[paths] keys_file` (see Credentials) by routes with
`keys = true` and by probes; they are never in `producer.toml`. A friend's account may have only one of these: routes whose
probe fails are skipped automatically.

**One Claude subscription only** (`python tools/guardian.py preset single-claude --apply`): the Producer and every Worker are
Claude Code seats on the same account and **share one 5-hour window** (and one weekly window). More seats do not add quota — the
window empties faster. Run the Producer (Sonnet 5.5, medium) plus **at most two** Workers (Sonnet / Haiku), and let the
Guardian's wake-up alarm carry the night: on `5-hour limit reached ∙ resets 7pm` the seat sleeps and is woken at reset + 2 min.

## Roster

The owner's worker roster lives in **`producer.toml` `[[roster]]`** — one home: the Guardian counts those seats and
`tools/setup.py` proposes a default. Record his wording and the date in a comment above the seat. This page keeps no second roster.

## Credentials

Secrets remain outside the repository and never appear in logs, reports, screenshots, or chat.

- Shared provider variables live in `~/.config/producer/keys.env`, file mode `600`, outside version control. Source it in the process that needs the route: `. ~/.config/producer/keys.env`.
- Variable names used by routes: `OPENROUTER_API_KEY`, `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`, `COMMAND_CODE_API_KEY`, and `COMMANDCODE_API_KEY`. Never store or echo their values here.
- Provider-owned stores stay in their normal locations: Codex `~/.codex/`, Claude `~/.claude/`, Gemini/AGY `~/.gemini/`, and GitHub CLI `~/.config/gh/`. Authenticate through each provider's own tool.
- A key in the process environment does not prove it reaches a new terminal. Verify the route with a probe, without printing the value.







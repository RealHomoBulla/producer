# Model advice — what we measured, who fits which job

**Our measured experience, as of 2026-10. Re-probe before you rely on any line here** — access,
limits and quality change weekly, and a route that was dead yesterday can be alive today (law 4).
This page answers two questions: *which model do I give this job to?* and *where do I buy more
usage when a window runs out?* Pricing below is the published list price; quota readings are
snapshots, never a promise.

Sources: `work/agents/orca/ROUTING.md` (this skeleton) and the author's measurement notes from the large project where this
process was developed (calibration tables, roster rulings, routing incidents, 2026-08…10).

## Quick table

| model / route | good at | verdict | cost |
|---|---|---|---|
| Claude Opus 5.5 | synthesis, code, design; the Producer | **Producer**, Worker (code/design) | subscription |
| Claude Sonnet 5.5 | bounded implementation, cross-checks, blitz | **Worker**, Producer (one subscription) | subscription |
| Claude Haiku 4.5 | cheap light work | Worker (light) | subscription |
| Codex Luna Max | commit gate + general Worker | **reviewer/gate**, Worker | subscription |
| Codex Sol 6.1 | demanding set-piece work | Worker (owner-named only), medium | subscription |
| DeepSeek 4.1 Flash on OpenCode Go | bulk evidence + implementation | **Worker (bulk)**, gate reviewer | usage / monthly |
| DeepSeek 4.1 Flash on OpenRouter | bounded work, balance-metered | Worker | per-token, per-key cap |
| Space Bunny (free) | report-only, bytecode, small code+test | **Worker (free)**, second reviewer | free |
| Muse 1.3 contributor (free) | reviews | reviewer | free |
| MiMo 2.6 flash (free) | reviews | reviewer | free |
| AGY / Gemini 3.8 Flash High | lookups, surface sweeps, images | mechanical only, **never a reviewer** | free-ish, no meter |
| Command Code | small shared budget | Worker only after a probe | small shared budget |
| Astra, Fable | large planning (gated) | **per-request only** | expensive |
| Kiro | — | **never** | — |

## Subscription models

### Claude Opus 5.5 — the default Producer
- **Good at:** synthesis across many findings, real code and design work, holding a large plan together. The owner of the pack this was measured on twice said he was very happy with it as Producer and as a code/design Worker.
- **Weak at:** burn rate. A full 5-hour window drops fast at `high`; the Claude *weekly* window is the scarce one, so keep it for the Producer and a few unique hard tasks.
- **Cost / limits:** subscription; separate rolling 5-hour and weekly windows, shared with every other Claude seat on the account (Sonnet, Haiku, Fable). Effort changes the burn, not the meter shape.
- **Verdict:** **Producer** (default), Worker on code/design. Worker and Producer default to **medium** effort; `high` only on the owner's word or an authorised depth gate.

### Claude Sonnet 5.5 — the workhorse
- **Good at:** bounded implementation, cross-checks, blitz work; also a fine Producer when there is only one subscription.
- **Weak at:** sharing the same window as Opus — more Sonnet tabs do not add quota.
- **Cost / limits:** subscription; same windows as Opus. Use the full model id; a bare `sonnet` alias can resolve to an older model.
- **Verdict:** **Worker** (default), blitz Worker, or Producer in the one-subscription preset.

### Claude Haiku 4.5 — the light seat
- **Good at:** cheap light tasks beside a Sonnet/Opus seat.
- **Verdict:** Worker (light). Same windows as the rest of the account.

### Codex Luna Max (`gpt-6-luna`) — the commit gate
- **Good at:** commit review / adversarial gate, and ordinary bounded Worker tasks. Codex is a different model family from Claude, so it catches what Claude misses.
- **Weak at:** the Codex 5-hour window drains quickly, and it is shared with any Sol seat — a long Sol run can starve the gate.
- **Cost / limits:** subscription (behaves like a 1× plan); separate 5-hour and weekly windows. Gate priority: keep one Luna-max gate running; do not grow Sol seats while the 5-hour window is tight.
- **Verdict:** **reviewer/gate** + Worker. Review written by Luna is cross-checked by a non-Codex family.

### Codex Sol 6.1 (`gpt-6.1-sol`) — demanding work, by request
- **Good at:** heavy set-piece work when the task really needs depth.
- **Weak at:** eats the shared Codex 5-hour window; the account can reject it. `gpt-6-sol` (without `.1`) is **never** used — it is much weaker and burns the limit as fast.
- **Cost / limits:** subscription, medium effort by default; `high` only on the owner's word. If the requested model is unavailable, do **not** silently substitute a neighbouring version or family — say so and ask.
- **Verdict:** Worker, owner-named only.

## Pay-per-use and gateway models

### DeepSeek 4.1 Flash on OpenCode Go — the bulk worker
- **Good at:** bounded implementation and evidence work, in volume. The owner describes it as an excellent Worker.
- **Weak at:** as a **native** OpenCode Producer its streaming output floods the chat and buries the owner's own messages. DeepSeek = **4.1 Flash only**; no `deepseek-v4-pro`.
- **Cost / limits:** rolling 5-hour window plus a monthly ceiling; the 429 body names the limiting window (`5 hour` clears by itself, `monthly` waits for the month). Go's capacity is usually plentiful compared with Claude/Codex.
- **Verdict:** **Worker (bulk)**; also a commit-gate reviewer (`commit_review.py open --reviewer deepseek`). Producer only through the Claude Code harness (`claude-opencode-go-deepseek` route), never native OpenCode, and only on the owner's direction.

### DeepSeek 4.1 Flash on OpenRouter — standalone balance
- **Good at:** bounded work when OpenCode Go is down. OpenRouter accepts the Anthropic protocol, so the Claude Code harness can point at it.
- **Weak at:** per-key spending cap — check remaining credit first and stop at zero. There is no `openrouter` provider inside OpenCode itself.
- **Cost / limits:** balance-based; `GET /api/v1/auth/key` gives `limit_remaining`.
- **Verdict:** Worker; a separate account and balance from OpenCode Go.

### DeepSeek platform direct — separate balance
- **Good at:** the same model on its own balance, as a fallback route.
- **Weak at:** balance can reach zero (a `402 Insufficient Balance` is a real dead channel, not a probe bug); look-alike ids differ per provider.
- **Verdict:** Worker / Producer fallback only after a live balance probe.

### Command Code — small shared budget
- **Good at:** extra Worker capacity when alive.
- **Weak at:** historically intermittent; a separate small shared budget, not interchangeable with OpenCode Go. Claude models must **never** run through this CLI.
- **Verdict:** Worker only after a live probe. Treat as dead until a probe answers.

## Free lanes (probe before every launch; limits are unpublished)

Free endpoints publish no dependable quota count — a probe is the only proof. The free pool is per
account/device, not per key. All down → re-probe hourly.

### Space Bunny (`opencode/space-bunny-free`) — best free id
- **Good at:** report-only work, evidence, bytecode reading, small code/test tasks; in a blind calibration it scored 23/25 claims with no invented identifiers.
- **Weak at:** may not commit, can return a worker completion without the capability flag, bends data to satisfy a flawed checker, and stalls on long loops. Best free id, but not a judgement seat.
- **Verdict:** **Worker (free)** and a *second* gate reviewer beside Luna. Judgement and pool/price writes stay on Opus.

### Muse 1.3 contributor (free) and MiMo 2.6 flash (free)
- **Good at:** commit-gate review (`--reviewer meta|mimo`).
- **Weak at:** a MiMo tab was once seen to vanish leaving an orphan.
- **Verdict:** **reviewer**; they only answer inside native OpenCode (`opencode run`), never through curl or the Claude harness (403 FreeTierError). Muse has no paid-benchmark price; report-only reviews.

### Nemotron, big-pickle and the rest
- `nemotron-3-ultra-free`, `big-pickle`, `muse-spark-1.2-contributor-free` **missed every known review defect** — mechanical listing only, never a reviewer. `ling` / `nemotron-lightning` are fast mechanical enumerators.

### AGY / Gemini 3.8 Flash High — lookups only
- **Good at:** fast lookups, surface sweeps, image generation.
- **Weak at:** a measured commit-review sweep found issues in only 6/52 batches (12%); the owner ruled Gemini too weak for review. It has **no reliable quota meter**, and "the binary exists" is not "quota available".
- **Verdict:** mechanical extraction / cross-checks only, **never a reviewer and never a source of facts**. AGY is not the separate `gemini` CLI.

## Gated and banned

- **Astra and Fable — per request only.** They are not ordinary Worker seats. The one standing exception: a genuinely large planning-shaped task that *corrects* other agents, limits read first, at most one such dispatch per ~2 hours. Astra is currently refused outright (too expensive). Fable burns the shared Claude 5-hour and weekly windows and has its own extra ceiling.
- **Kiro — never**, in any role.
- **Never substitute a model or family silently.** If the requested model is unavailable, say so and ask; a neighbouring version is worse than a seat down.

## Where to get more usage — ranked by value for money

1. **DeepSeek 4.1 Flash on OpenCode Go** — strong bulk Worker, generous rolling window, cheap per
   use; run it through the Claude Code harness for a Producer, native OpenCode for a Worker.
2. **Free OpenCode lanes (Space Bunny first, then Muse / MiMo)** — $0 and genuinely useful while the
   free period lasts; probe first, expect them to vanish without warning, keep judgement work off them.
3. **Claude Pro ($20) + OpenCode Go** — Claude for production and review, DeepSeek for the bulk; the
   cheapest paid combination that still has two independent providers.
4. **Codex Plus ($20)** — worth it mainly for the Luna-max commit gate and as a different family from
   Claude; a Sol seat is a bonus, not the reason to buy it.
5. **Claude Max ($100)** — buy it when Claude *is* the whole team (Producer + several seats on one
   account); it raises the ceiling of the shared windows, not the number of windows.
6. **OpenRouter / DeepSeek direct** — buy a small balance as a failover when you do not want a
   second subscription; use the spend meter and stop at zero.
7. **AGY / Gemini** — free-ish and fine for lookups and images; do not plan capacity around it, and
   never route review or facts through it.

## Pick a preset

Ready-made route order + roster for each shape of account live in [`tools/presets.toml`](../../../tools/presets.toml):

- `solo-claude` — one Claude subscription ($20 Pro); Producer Sonnet + Sonnet and Haiku Workers, alarm carries the night.
- `claude-max-100` — our setup: Claude Max $100 (Producer Opus high, Sonnet Workers) + Codex Plus $20 (Luna gate / Sol by request) + OpenCode Go (DeepSeek bulk) + free Space Bunny / Muse / MiMo.
- `claude-plus-go` — Claude Pro $20 + OpenCode Go: Claude produces and reviews, DeepSeek does the bulk.
- `budget-free` — free OpenCode models + AGY only; honest about their limits.

Apply one with `python tools/setup.py --preset <name>` (or copy the `producer_routes` / `roster`
blocks into `producer.toml` by hand — they use the same keys the Guardian reads). After any change,
probe the routes and read `python tools/usage.py` before you count a seat.

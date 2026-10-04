# Owner shorthands — a phrase that means "do the whole thing, now"

**Open this page when the owner says one of the phrases below.** He says them in his own language (Russian by default; an
English owner says the English equivalent in the first column), mid-sentence, expecting the work to happen rather than a
question back. Each has exactly one procedure. Ambiguous in context → do the procedure and say what you did, in his language.
Add a row whenever he introduces a new one (his words + date).

| he says | do |
|---|---|
| «подключи мои аккаунты», «настрой ключи» / "connect my accounts" | `START_PROMPT.md` §0a: setup.py → he logs in himself → he pastes key values into his keys file (`[paths] keys_file`) → probe every route → roster in `producer.toml` |
| «добавь бекап-продюсера <модель>», «поменяй ростер», «перезапусти гардиана» / "add a backup producer", "change the roster", "restart the guardian" | `work/agents/knowledge/GUARDIAN.md` §1–7: the exact steps, the `producer.toml` snippet and the command that proves each change. The Guardian only counts and reminds; the Producer launches the seat |
| **«новый проект», «старт», «вот задача: …»** / "new project", "start" (empty brief) | the kickoff, `START_PROMPT.md` §0: brief questions one at a time → `work/БРИФ.md` (`BRIEF.md`) → first plan → first Run |
| «обнови бриф», «поменялось: …» / "update the brief" | rewrite the touched sections of `work/БРИФ.md` in place, date in the status line, check `TODO.md` rows it affects |
| `usage`, «лимиты», «квоты», «какие модели доступны» / "limits", "quota" | run `python tools/usage.py` (read-only; `--json` for the machine form; the probes behind it are in `ROUTING.md` §Probes and availability) and answer in his language, short: which routes are alive, what % of each window is left, when it resets |
| «посоветуй где взять ещё usage», «какие модели брать» / "where can I get more usage", "which models" | read `work/agents/knowledge/MODEL_ADVICE.md` (which model for which job, where to buy more capacity — dated, re-probe before relying) and answer in two or three lines for HIS accounts; if he wants a different setup, show `python tools/setup.py --list-presets` |
| **«блиц»** / "blitz" | a decision session **in its own Worker tab**, never in the Producer chat: create one Task, launch a FRESH Worker (Claude Sonnet medium by default) titled `Blitz`, put it on Remote Control (`/remote-control` in its composer) and give him the link. Brief = `DISPATCH.md` §Blitz. Queue = `registers/BLITZ<N>_READY.md`, written by the Producer **as questions appear**, each with enough context for the blitz Worker to answer follow-ups. **Before the blitz opens run `python tools/blitz.py new`** (creates `BLITZ<N>_READY.md` with the next N and writes the «Сверка прошлых блицев» table from the previous blitz's answers: done / queued / MISSING); a MISSING row is routed to `TODO.md` / `OPEN.md` / the brief first — the tool exits 1 until none is left. `blitz.py crosscheck` re-checks an existing file, `blitz.py status` shows the open questions and the active blitz |
| **«закрыть блиц»** / "close the blitz" | typed in the blitz tab: the blitz Worker writes its closure and sends worker_done. The Producer routes it like any Delivery (law 6c): rulings → `OPEN.md`, work → `TODO.md` (accepted ideas at the END of the queue), checks → `ЧЕКЛИСТ.md`; then closes the tab |
| **«дайджест»** / "digest" | an immediate re-check of finished products he has not heard about: `python tools/digest.py list`, read each, append a short verdict in his language, say it in chat, mark it (`START_PROMPT.md` §7) |
| **«прочитал дайджест»** / "read the digest" | `python tools/digest.py clear` (archives the page, marks exactly what it covered) |
| **«что там по отчётам»**, «какие решения требуются» / "what about the reports" | walk every unrouted report and land its content in the registers **in the same turn** — he wants it routed, not summarised |
| **«автономно», «ночная вахта»** / "work all night" | persistent autonomous work: prove the Guardian is alive first (`python tools/guardian.py status`; if it is down, one line to him and offer to start it — `AUTONOMY.md`), then `python tools/guardian.py autonomy on --objective "…"`, fill the seats of his last stated roster, work the TODO top block, keep the digest current for the morning |
| **«стоп автономно», «stop autonomous»** | **first** `python tools/guardian.py autonomy off` (otherwise the Guardian keeps nudging and relaunching), then no new Tasks; supervise every live Worker to a safe settlement; write HANDOVER |
| «закрой воркеров», «убери терминалы» / "close the workers" | close settled Workers only (accepted, report committed), `--tab` always, never your own tab or the Blitz tab |
| «прогони чеклист» / "run the checklist" | take what he marked in `work/ЧЕКЛИСТ.md` (`CHECKLIST.md`), act on it; the file keeps a «Куда ушло» table so nothing looks deleted |
| **«чекап»**, «чекап доков» / "checkup" (only these words) | the heavy audit: the `docs-checkup` skill (`.claude/skills/docs-checkup/SKILL.md`), in full — once a day or two |
| «обнови доки», «поправь доки» / "update the docs" | the light case: edit only the one or two pages the change touches, in place, then run `python tools/doc_check.py` |
| an idea thrown mid-work («а давай ещё…») | one dated verbatim line in `registers/ДАЛЬНИЙ_ЯЩИК.md`, answer «записал в дальний ящик», keep working the near queue; promote only on his word |
| «сделай» inside a blitz | a request to the Producer: the blitz Worker records it; the Producer routes it at closure |

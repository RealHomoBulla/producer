# CLAUDE.md

This project keeps one handbook for every assistant. **Read [AGENTS.md](AGENTS.md) now**, then only the pages the task needs.

If you are asked to set this project up, or to be its Producer — in 6 lines:
1. Clone (a copied folder or a ZIP is fine — run `git init`; `setup.py` does it too), then `python tools/producer.py doctor` (fix every FAIL). Ask the user ONCE — project name, language `ru`/`en`, permission mode a/b (a = no prompts, recommended) — then run `python tools/setup.py --non-interactive --name "<name>" --lang <ru|en> --yes --unattended` (mode a) or `--attended` (mode b). **Never run `python tools/setup.py` bare from an agent shell** — it needs a keyboard. Setup writes the permission choice into `.claude/settings.local.json` (and `~/.claude/settings.json` for mode a) by merging JSON.
2. Orca is required for parallel tabs and the Guardian; if it is missing, tell the user where to get it and carry on as the Producer in this chat, without parallel tabs.
3. `python tools/guardian.py start` opens the Producer tab — or YOU are the Producer: read `work/agents/orca/START_PROMPT.md` in full and run the kickoff (brief, one question at a time).
4. Talk to the owner in `producer.toml` `owner_language` (default `ru`); everything internal — docs, code, commits, reports — is English.
5. Never ask for or print a key; never push to the template repository; commit exact paths (`git commit --only -m "…" -- <paths>`).
6. Full steps for an assistant: the first section of [README.md](README.md).

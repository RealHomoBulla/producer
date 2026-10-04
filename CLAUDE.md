# CLAUDE.md

This project keeps one handbook for every assistant. **Read [AGENTS.md](AGENTS.md) now**, then only the pages the task needs.

If you are asked to set this project up, or to be its Producer — in 6 lines:
1. Clone, then `python tools/producer.py doctor` (fix every FAIL) and `python tools/setup.py` (ask the user: name, language `ru`/`en`, unattended overnight yes/no).
2. Orca is required for parallel tabs and the Guardian; if it is missing, tell the user where to get it and carry on as the Producer in this chat, without parallel tabs.
3. `python tools/guardian.py start` opens the Producer tab — or YOU are the Producer: read `work/agents/orca/START_PROMPT.md` in full and run the kickoff (brief, one question at a time).
4. Talk to the owner in `producer.toml` `owner_language` (default `ru`); everything internal — docs, code, commits, reports — is English.
5. Never ask for or print a key; never push to the template repository; commit exact paths (`git commit --only -m "…" -- <paths>`).
6. Full steps for an assistant: the first section of [README.md](README.md).

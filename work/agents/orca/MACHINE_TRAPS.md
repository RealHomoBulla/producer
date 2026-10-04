# Machine traps

Each entry is symptom → cause → fix. These are cross-platform operating notes; provider limits and live Orca state still need a fresh check.

## Git

- `git status` omits edits or new files → a stale `core.fsmonitor` daemon reports an old snapshot → use `git -c core.fsmonitor=false status`, and keep that option on add/commit commands.
- A scoped `git commit --only -- <path>` includes another agent's unstaged edits in the same file → `--only` stages the whole working-tree file → inspect `git -c core.fsmonitor=false diff --numstat -- <path>` before committing; split or wait if counts exceed your changes.
- Shared history appears to move underneath a commit → another seat committed on the same branch → add a new corrective commit; never amend, reset, force-push, or rewrite shared history.

## Shells and processes

- `pkill -f` or `pgrep -f` kills or finds the command currently running the check → the pattern appears in the shell's own command line → match by exact executable and inspect its working directory; signal a verified PID.
- A Git Bash slash command arrives as a Windows path → MSYS converts leading `/` arguments → send it from PowerShell or set `MSYS_NO_PATHCONV=1` for that command.
- Every shell command fails with a quota/write error even when the command is valid → the `/tmp` tmpfs is full → inspect usage and scratch freshness; remove only verified stale scratch, never directories that a live agent may still write.
- A freshness query using `find -newermt "-30 minutes"` returns no paths → the installed `bfs` implementation rejects relative timestamps → use `-mmin -30` or an absolute timestamp, and inspect stderr before cleanup.

## Windows and terminal control

- A Python tool crashes with `UnicodeEncodeError` (arrows, dashes, Cyrillic) → the console code page (cp1250/cp866/cp1252) cannot show them → the toolchain's entry points already replace unencodable characters; for a script of your own set `PYTHONUTF8=1` or run `python -X utf8 …`.
- `pytest` errors in every fixture with a permission error under `%TEMP%\pytest-of-<user>` → a stale folder owned by another process → run `python -m pytest -q tools/tests --basetemp <a fresh folder of yours>`.
- A Python edit script written in a Git Bash heredoc has its backslashes collapsed (`"\\n"` becomes a real line break in the target file) → write such a script with the file-writing tool, not a heredoc, or avoid backslashes (`chr(92)`).
- A fresh clone shows every file modified on Windows → `core.autocrlf` rewrote line endings → compare with `git diff -w` before concluding anything; do not commit a whole-file line-ending rewrite.

- A detached process exits when its PowerShell parent closes → it was launched as a child tied to that console → use `Start-Process pythonw.exe` for a hidden background Python process and verify the process independently.
- An Orca CLI call hangs or a broad process kill terminates agent calls → every CLI invocation may own an Orca child process → do not kill by the shared executable name; identify the app's main process and keep long CLI calls attached to their own handle.
- `worker_done` or heartbeat says dispatch capability is missing → the tab opened empty and the task was pasted manually → deliver the report path to the coordinator and have it settle the task; a manually populated tab cannot gain the original dispatch capability.
- A launch says it was accepted but the Worker has no task text → Windows paste submission stalled or remains in the composer → read the terminal and confirm the brief appears in the transcript; submit the task text explicitly and verify again.
- A Codex tab after a limit rejects input with `agent_prompt_blocked` → a model-switch confirmation dialog is above the prompt → choose `3`, send Enter separately, then send the task text and a separate Enter; confirm the working status in the terminal tail.

## Load

Every live seat costs RAM and CPU, and a tab left over from a failed launch keeps costing. **The ceiling is measured on THIS
machine, never copied from another one's notes.** Write the measured limits into `HANDOVER.md` «Standing» (for example «max
N Claude seats; max M heavy test runs at once») and refresh them when the machine or the product changes.

- Before launching a seat check free memory and load: Windows `Get-CimInstance Win32_OperatingSystem | Select
  FreePhysicalMemory,TotalVisibleMemorySize` · Linux/macOS `free -m` and `uptime`. If free memory is below what one more seat
  needs plus a margin, queue the Task instead of launching.
- Heavy jobs (full test suite, a build, a browser run) are limited to a few at a time; close a finished seat at once.
- A machine that freezes under load loses the whole night: lower the seat count first, ask why later.
- The Guardian's load backoff, when enabled, reads its thresholds from `producer.toml` (`work/agents/knowledge/GUARDIAN.md`);
  those are per-machine settings too.

## Worker liveness

- A Worker shows `API Error: Connection lost mid-response` then stops producing output → its turn ended silently while the tab stayed open → compare report modification times, read the terminal tail, and send a continuation naming the failure.
- An OpenCode seat opens but does no work → its per-tab database activity or RAM pressure stalls the process → verify the live model and transcript, check memory, then retry only after the failed process is resolved.
- A Go-backed Claude seat opens but immediately idles despite an apparently live key → Claude Code may have stored the custom key suffix under `rejected` → inspect `~/.claude.json` without exposing key material and move the correct suffix to `approved` only after verifying the intended credential.

## Safe checks

- A process-list pipeline reports itself as the target → its search text also appears in the shell command line → verify by executable name, parent, and working directory; avoid broad grep/kill patterns.
- A status or quota display says a route is live, but its terminal shows a limit/error → the display is stale or measures account setup rather than usable inference → trust a fresh model-specific smoke test and the live terminal response.

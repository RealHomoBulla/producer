"""Cross-model commit review: who wrote each commit, and a second model checking every tenth.

The owner's rule: every ten commits, a model family that did **not** write them reviews them,
so two different models always re-check each other. A model reviewing its own work shares its
own blind spots, so the review must come from a different family — and it must be automatic,
because a rule that depends on someone remembering is the rule that broke the hooks.

Three deliberate design choices:

1. **The commit ledger is derived from `git log`, never stored.** Only the *verdicts* — which git
   cannot know — are persisted. A journal file that could disagree with `git log` eventually
   would, and then there would be two truths about who wrote what.
2. **The reviewer family is chosen from the batch's own authorship**, so the rule is symmetric:
   a batch written by one family goes to another. A mixed batch goes to a family that wrote the
   least of it — the smallest stake is the least conflicted.
3. **A verdict is either `approved` or a list of findings**, and findings land in a real register
   (`POLISHING_TODO.md`) rather than in a chat message. "Send it back for rework" only means
   something if the rework is written down where the next Producer will read it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import orca_cli
import paths

PROJECT = paths.PROJECT

# A tool that runs under pythonw has NO console of its own. A console child started without this
# flag makes Windows allocate one, and a terminal window pops up over whatever the owner is doing.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

STATE = paths.COMMIT_REVIEW_STATE
PAGE = paths.COMMIT_JOURNAL
POLISH = paths.POLISHING_TODO
PROMPTS = paths.REVIEWS_DIR
REPORTS = paths.REPORTS_REGISTER

BATCH = 10          # the owner's number: review every tenth commit
PAGE_BATCHES = 12   # how many batches the owner-facing page keeps

_FINDINGS_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?FINDINGS\s*$", re.IGNORECASE)
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+")
_ARTIFACT_VERDICT = re.compile(r"^\s*VERDICT:\s*(approved|findings)\b", re.IGNORECASE)
_BARE_SECTION_HEADING = re.compile(r"^[A-Za-z][A-Za-z0-9 &/'()\-]*$")
_NO_FINDINGS_PROSE = re.compile(
    r"^(?:none\b|no\s+(?:live\s+)?(?:findings?|defects?)\b|clean\b|nothing\b)", re.IGNORECASE)
_DELIVERY_FALLBACK_LINE = re.compile(r"^\s*DELIVERY-FALLBACK\s*:", re.IGNORECASE)
_CLEAN_SECTION_LABEL = re.compile(
    r"^(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:"
    r"checked\s+and\s+clean"
    r"|checked\s+and\s+found\s+clean"
    r"|checked\s+clean"
    r"|checks?\s+clean"
    r"|clean-line\s*:"
    r"|clean\s*:"
    r"|clean\s+(?:so|and|check|verification|line)\b"
    r"|clean\b(?=\s*[,.;!\u2014\u2013])"
    r"|clean\s*$"
    r")", re.IGNORECASE)
_HTML_COMMENT_LINE = re.compile(r"^\s*<!--.*-->\s*$", re.DOTALL)

# Which family a `Co-Authored-By` trailer belongs to. Matched longest-first, lowercased. A family
# is coarser than a model on purpose: two members of one vendor's family still share training, and
# the point is that the two reviewers do not share a blind spot. Extend by adding keys here.
FAMILIES = {
    "claude-opus": "claude-opus",
    "claude opus": "claude-opus",
    "opus": "claude-opus",
    "claude-sonnet": "claude-sonnet",
    "claude sonnet": "claude-sonnet",
    "sonnet": "claude-sonnet",
    "claude": "claude-sonnet",
    "codex-luna": "codex-luna",
    "codex luna": "codex-luna",
    "luna": "codex-luna",
    "codex-sol": "codex-sol",
    "codex sol": "codex-sol",
    "codex": "codex-sol",
    "sol": "codex-sol",
    "terra": "codex-sol",
    "gpt": "codex-sol",
    "deepseek": "deepseek",
    "space-bunny": "space-bunny",
    "space bunny": "space-bunny",
    "spacebunny": "space-bunny",
    "muse": "muse",
    "mimo": "mimo",
    "haiku": "claude-sonnet",
    "gemini": "gemini",
}

# Preference order for the automatic reviewer pick, best-calibrated first. Availability is never
# assumed from this list — the live meter is the authority, and `open` prints the choice so a
# Producer can override it with `--reviewer` when a channel is down.
REVIEWER_PREFERENCE = ("codex-luna", "claude-sonnet", "gemini", "deepseek")


def preference() -> tuple[str, ...]:
    """The reviewer families in use: `[review] reviewers` of producer.toml, else the built-in list."""
    return paths.review_reviewers() or REVIEWER_PREFERENCE


class NoIndependentReviewer(ValueError):
    """Every usable reviewer family also wrote part of the batch: the cross-family gate is UNMET."""

# Which Orca agent (and model id, when the agent takes one) starts a reviewer of each family. Used
# only to print a launch hint; the real launch is `orca orchestration worker-start` (DISPATCH.md).
ORCA_AGENT = {
    "claude-opus": ("claude", "claude-opus-5-5"),
    "claude-sonnet": ("claude", "claude-sonnet-5-5"),
    "codex-luna": ("codex", None),
    "codex-sol": ("codex", None),
    "gemini": ("antigravity", None),
    "deepseek": ("opencode", None),
    "space-bunny": ("opencode", None),
    "muse": ("muse", None),
    "mimo": ("opencode", None),
}


def launch_hint(family: str, prompt: str) -> str:
    """A true launch recipe for a reviewer: a Task carrying the brief, then a supervised worker."""
    agent, model = ORCA_AGENT.get(family, (None, None))
    if agent is None:
        return f"(no known Orca agent for family {family!r}; start a worker of that family by hand)"
    flags = f" --agent {agent}" + (f" --model {model}" if model else "")
    return (f'orca orchestration task-create --spec "Review the commit batch per {prompt}" '
            f'--task-title "Commit review" ; then orca orchestration worker-start --task <task_id>{flags}'
            "  (work/agents/orca/DISPATCH.md)")


# The commit-message marker shapes that identify agent-authored work whose trailer is missing.
AGENT_MESSAGE_MARKERS = (
    (re.compile(r"report:\s*work/agents/", re.IGNORECASE), "message cites a work/agents/ report path"),
    (re.compile(r"falsifier:", re.IGNORECASE), "message carries a worker falsifier line"),
    (re.compile(r"worker_done", re.IGNORECASE), "message names the worker_done lifecycle"),
    (re.compile(r"delivery-fallback:", re.IGNORECASE), "message carries a worker delivery-fallback line"),
    (re.compile(r"^verdict:\s*(approved|findings)\b", re.IGNORECASE | re.MULTILINE),
     "message carries a reviewer VERDICT line"),
    (re.compile(r"sandbox marker:", re.IGNORECASE), "message names a worker sandbox marker"),
    (re.compile(r"owned zone:", re.IGNORECASE), "message declares a worker owned zone"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(PROJECT), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def family_of(trailer: str) -> str:
    """The model family behind a `Co-Authored-By` value, or `human` when there is no trailer.

    A commit with no trailer is the owner's own or a pre-agent one. It is not reviewed — but it IS
    counted in the journal, because a ledger that quietly omits rows is how you trust a wrong total.
    """
    low = trailer.lower()
    for key in sorted(FAMILIES, key=len, reverse=True):
        if key in low:
            return FAMILIES[key]
    return "human" if not trailer.strip() else "other"


def classify_untrailered(sha: str, subject: str, body: str) -> tuple[str, str] | None:
    """Explicit-evidence check for a commit with no usable trailer.

    Returns `("unattributed", reason)` when the commit is proven agent work, else `None` — and
    `None` means the commit stays `human`. Guessing a family from a bare message is refused.
    """
    text = f"{subject}\n{body}"
    for pattern, label in AGENT_MESSAGE_MARKERS:
        if pattern.search(text):
            return "unattributed", f"agent message marker: {label}"
    return None


def commits(limit: int = 400) -> list[dict]:
    """The ledger, derived from `git log` every time. Oldest first."""
    sep = "\x1e"
    fmt = sep.join(["%H", "%h", "%aI", "%an", "%s", "%b",
                    "%(trailers:key=Co-Authored-By,valueonly)"])
    raw = git("log", f"-{limit}", f"--format={fmt}\x1d")
    rows = []
    for chunk in raw.split("\x1d"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(sep)
        if len(parts) < 7:
            continue
        sha, short, at, author, subject = parts[:5]
        trailer = parts[-1].replace("\n", " ").strip()
        body = sep.join(parts[5:-1])
        family = family_of(trailer)
        model = trailer.split("<")[0].strip() or "(none)"
        if family == "human":
            hit = classify_untrailered(sha, subject, body)
            if hit is not None:
                family, why = hit
                model = f"(unattributed: {why})"
        rows.append({
            "sha": sha, "short": short, "at": at, "author": author, "subject": subject,
            "model": model, "family": family,
        })
    rows.reverse()
    return rows


class BaselineOutsideHistory(RuntimeError):
    """The stored review baseline is not inside the git history `commits()` can produce."""


_HISTORY_READ_CAP = 200_000


def _commits_covering(sha: str, *, window: int = 400) -> list[dict]:
    """Read git history deep enough that `sha` is inside the returned rows."""
    limit = window
    rows = commits(limit=limit)
    seen = {row["sha"] for row in rows}
    while sha not in seen:
        if limit >= _HISTORY_READ_CAP:
            raise BaselineOutsideHistory(
                f"baseline {sha[:12]} not found in the newest {_HISTORY_READ_CAP} commits; "
                "refusing to treat the window as the whole scope")
        limit = min(limit * 4, _HISTORY_READ_CAP)
        deeper = commits(limit=limit)
        if len(deeper) <= len(rows):
            raise BaselineOutsideHistory(
                f"baseline {sha[:12]} is not in this git history (only {len(deeper)} commits); "
                "refusing to treat the window as the whole scope")
        rows = deeper
        seen = {row["sha"] for row in rows}
    return rows


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"batches": []}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def reviewed_shas(state: dict) -> set[str]:
    out: set[str] = set()
    for batch in state["batches"]:
        out.update(batch["commits"])
    return out


def unreviewed(state: dict) -> list[dict]:
    """Agent-written commits not yet inside any batch, oldest first.

    `unattributed` rows (untrailered commits with explicit agent evidence) ARE queued: unknown
    family is not unknown authorship. The owner's own commits are counted but never queued.
    """
    seen = reviewed_shas(state)
    rows = commits()
    base = (state.get("baseline") or {}).get("sha")
    if base:
        if base not in {row["sha"] for row in rows}:
            rows = _commits_covering(base)
        shas = [row["sha"] for row in rows]
        if base not in shas:  # pragma: no cover
            raise BaselineOutsideHistory(f"baseline {base[:12]} is not in this git history")
        rows = rows[shas.index(base) + 1:]
    return [c for c in rows if c["sha"] not in seen and c["family"] not in ("human", "other")]


def independent_reviewer_families(rows: list[dict]) -> tuple[str, ...]:
    """Every configured reviewer family that did not author this batch."""
    wrote = {str(row["family"]) for row in rows}
    return tuple(family for family in preference() if family not in wrote)


def _batch_author_families(batch: dict) -> set[str]:
    stored = batch.get("author_families")
    if stored:
        return {str(family) for family in stored}
    return {family_of(str(author)) for author in batch.get("authors", [])}


def _reviewer_family(reviewer_name: str) -> str:
    candidate = str(reviewer_name or "").strip()
    if candidate in preference() or candidate in FAMILIES.values():
        return candidate
    return family_of(candidate)


def pick_reviewer(rows: list[dict], override: str | None = None,
                  require_independent: bool = False) -> tuple[str, str]:
    """Choose the reviewing family and say why, in one line the owner can check.

    A family that did not write the batch is always preferred. When there is none (one
    subscription, or every configured family wrote part of it) the gate is NOT skipped: the batch
    goes to the family that wrote the least of it, in a FRESH session, and is marked
    `SAME-FAMILY (weaker)` everywhere it is recorded. `require_independent` refuses instead, for a
    project that prefers no review to a weaker one.
    """
    pref = preference()
    wrote: dict[str, int] = {}
    for row in rows:
        wrote[row["family"]] = wrote.get(row["family"], 0) + 1
    independent = independent_reviewer_families(rows)
    stake = {family: count for family, count in wrote.items() if family in pref}
    if override:
        if independent and override not in independent:
            raise ValueError(f"{override} authored this batch or is not an independent review family")
        if not independent:
            if override not in pref:
                raise ValueError(f"{override} is not a review family")
            if require_independent:
                raise NoIndependentReviewer(_unmet_gate_text(wrote))
            return override, _same_family_why(override, stake, len(rows), chosen=True)
        return override, "chosen by the Producer, overriding the automatic pick"
    if not independent:
        if not stake:
            return pref[0], ("no attributed authorship in this batch "
                             f"({len(rows)} unattributed); defaulting to {pref[0]}")
        if require_independent:
            raise NoIndependentReviewer(_unmet_gate_text(wrote))
        smallest = min(sorted(stake), key=lambda f: stake[f])
        return smallest, _same_family_why(smallest, stake, len(rows))
    family = independent[0]
    if not stake:
        return family, (f"{family} wrote none of this batch; authorship is unattributed, "
                        f"so no stake comparison applies")
    dominant = max(stake, key=lambda f: stake[f])
    return family, f"{dominant} wrote {stake[dominant]}/{len(rows)} of this batch; " \
                   f"{family} wrote none of it"


def _same_family_why(family: str, stake: dict[str, int], total: int, chosen: bool = False) -> str:
    share = f"{stake.get(family, 0)}/{total}"
    how = "named by the Producer" if chosen else "the family that wrote the least"
    return (f"SAME-FAMILY (weaker): no independent family is configured or free ({how}: {family} wrote "
            f"{share}). Review in a FRESH session with none of the authors' context, using a different "
            "model of the family when there is one, else the same model. A different family is better "
            "when you can get one; this is the minimum, and the gate is never skipped.")


def _unmet_gate_text(wrote: dict[str, int]) -> str:
    return ("cross-family review gate UNMET and independence was required: every configured reviewer family "
            f"({', '.join(preference())}) also wrote part of this batch (authors: "
            f"{', '.join(sorted(wrote)) or 'unattributed'}). Add a family you can run to `[review] reviewers` "
            "in producer.toml, or drop --require-independent to take a same-family (weaker) review.")


_ANSWER_ARTIFACT_NAME = re.compile(r"[-_]REVIEW(?:_[^/]*)?\.md$|[-_]VERDICT(?:_[^/]*)?\.md$")


def _review_artifact_candidates(batch: dict) -> list[Path]:
    batch_id = str(batch["id"])
    preferred = (
        f"{batch_id}_REVIEW.md",
        f"{batch_id}_SOL_REVIEW.md",
        f"{batch_id}_LUNA_REVIEW.md",
        f"{batch_id}_OPUS_REVIEW.md",
        f"{batch_id}_VERDICT.md",
        f"{batch_id}-VERDICT.md",
    )
    candidates = [PROMPTS / name for name in preferred if (PROMPTS / name).is_file()]
    seen = {path.name for path in candidates}
    for path in sorted(PROMPTS.glob(f"{batch_id}*.md")):
        if path.name == f"{batch_id}.md" or path.name in seen:
            continue
        if _ANSWER_ARTIFACT_NAME.search(path.name):
            candidates.append(path)
            seen.add(path.name)
    return candidates


def _newest_with_verdict(candidates: list[Path]) -> Path | None:
    with_verdict = []
    for index, path in enumerate(candidates):
        verdict, _ = _parse_review_artifact(path)
        if verdict is not None:
            with_verdict.append((path, index))
    if not with_verdict:
        return None
    return max(with_verdict, key=lambda item: (item[0].stat().st_mtime, -item[1]))[0]


def _review_artifact_choice(batch: dict) -> tuple[Path | None, str]:
    """Resolve a batch's answer document, refusing to guess when two documents compete."""
    candidates = _review_artifact_candidates(batch)
    if not candidates:
        return None, ""
    chosen = _newest_with_verdict(candidates) or candidates[0]
    return chosen, ""


def _review_artifact(batch: dict) -> Path | None:
    return _review_artifact_choice(batch)[0]


def _is_section_heading(line: str) -> bool:
    value = line.strip()
    if not value or value.startswith(("-", "*", "+", "`", ">", "|")):
        return False
    if value.startswith(("LIVE", "FIXED")):
        return False
    if re.search(r"[:—–]|[.,;!?]$", value):
        return False
    if len(value) > 49 or len(value.split()) > 4:
        return False
    return bool(_BARE_SECTION_HEADING.fullmatch(value))


def _parse_review_artifact(path: Path | None) -> tuple[str | None, list[str]]:
    """Read a review answer's verdict and plain-list `FINDINGS` block."""
    if path is None:
        return None, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, []
    verdicts = [match.group(1).lower() for line in lines if (match := _ARTIFACT_VERDICT.match(line))]
    headings = [index for index, line in enumerate(lines) if _FINDINGS_HEADING.fullmatch(line)]
    if not headings:
        return (verdicts[-1] if verdicts else None), []

    findings: list[str] = []
    for line in lines[headings[-1] + 1:]:
        if _MARKDOWN_HEADING.match(line) or _ARTIFACT_VERDICT.match(line):
            break
        value = line.strip()
        if not value or value in {"---", "```"}:
            continue
        if _HTML_COMMENT_LINE.match(value):
            continue
        if _DELIVERY_FALLBACK_LINE.match(line) or _CLEAN_SECTION_LABEL.match(value):
            break
        if (findings and not _is_finding_line(_strip_list_marker(value))
                and not re.match(r"(?:[-*+]|\d+[.)])\s|\|", value)
                and not _is_section_heading(value)):
            findings[-1] = f"{findings[-1]} {value}"
            continue
        if _NO_FINDINGS_PROSE.match(value.lstrip("*_ ")):
            if not findings and value.lstrip().startswith(("**", "__")):
                break
            continue
        if _is_section_heading(value):
            break
        candidate = _strip_list_marker(value)
        if _is_finding_line(candidate):
            findings.append(candidate)
    return (verdicts[-1] if verdicts else None), findings


_FINDING_LINE = re.compile(
    r"^(?:\*\*|__)?"
    r"(?:LIVE|FIXED[ -]BY\s+`?[0-9a-f]{7,40}`?|ALREADY[ -]FIXED(?:\s+`?[0-9a-f]{7,40}`?)?)"
    r"(?:\*\*|__)?"
    r"(?:\s*[\u2014\u2013:|\u00b7]|\s+--?\s|\s+`|\s+[0-9a-f]{7,40}\b)")


def _is_finding_line(value: str) -> bool:
    return bool(_FINDING_LINE.match(value))


_CITATION = re.compile(r"`([^`\s]*[/:.][^`\s]*)`|([\w./-]+\.\w+:\d[\d,\u2013-]*)")


def _finding_key(value: str) -> tuple[str, str] | None:
    match = _CITATION.search(value)
    if not match:
        return None
    kind = "fixed" if _FIXED_LABEL.match(value) else "live"
    return kind, (match.group(1) or match.group(2)).rstrip(".,;:").rsplit("/", 1)[-1]


def _is_copy_of(line: str, originals: list[str], *, kind: str | None = None) -> bool:
    key = _finding_key(line)
    if key and kind:
        key = (kind, key[1])
    for original in originals:
        if original.startswith(line):
            return True
        if key and key == _finding_key(original):
            return True
    return False


def _strip_list_marker(value: str) -> str:
    value = re.sub(r"^[-*+]\s+", "", value.strip())
    return re.sub(r"^\d+[.)]\s+", "", value)


def _artifact_label(path: Path) -> str:
    try:
        return path.relative_to(PROJECT).as_posix()
    except ValueError:
        return path.as_posix()


def settled_review_tasks(run: str | None = None) -> set[str]:
    """Which dispatched reviewer Tasks has Orca already finished? Best-effort by design."""
    if not run:
        return set()
    try:
        done = subprocess.run([orca_cli.resolve_cli(), "orchestration", "task-list", "--run", run, "--json"],
                              cwd=str(PROJECT), capture_output=True, text=True,
                              encoding="utf-8", timeout=20, creationflags=NO_WINDOW)
        rows = json.loads(done.stdout)["result"]["tasks"]
    except Exception:  # noqa: BLE001 - any failure means "cannot prove", never "not live"
        return set()
    return {str(r.get("id")) for r in rows
            if str(r.get("status")) in {"completed", "failed", "cancelled"}}


def _waiting_or_error(state: dict) -> tuple[list[dict], str | None]:
    try:
        return unreviewed(state), None
    except BaselineOutsideHistory as exc:
        return [], str(exc)


def reviewer_track_record(state: dict, reviewer: str) -> tuple[int, int]:
    """How often has THIS reviewer ever returned findings? (batches, findings)."""
    seen = findings = 0
    for row in state.get("batches", []):
        if row.get("status") not in ("findings", "approved"):
            continue
        if str(row.get("reviewer_name") or row.get("reviewer")) != reviewer:
            continue
        seen += 1
        findings += row["status"] == "findings"
    return seen, findings


def command_status(args: argparse.Namespace) -> int:
    state = load_state()
    pending = [b for b in state["batches"] if b["status"] == "pending"]
    waiting, baseline_error = _waiting_or_error(state)
    if args.quiet:
        if baseline_error:
            sys.stdout.write(f"commit review: 🔴 {baseline_error}\n")
            return 0
        dispatched = [b for b in pending if (b.get("dispatch") or {}).get("ok")]
        run = next((str((b.get("dispatch") or {}).get("run")) for b in dispatched
                    if (b.get("dispatch") or {}).get("run")), None)
        settled = settled_review_tasks(run)
        owed = [b for b in dispatched if str(b["dispatch"].get("task")) in settled]
        live = len(dispatched) - len(owed)
        mark = "DUE" if len(waiting) >= BATCH else "ok"
        if owed:
            mark = f"🔴 {len(owed)} OWED VERDICT (reviewers finished) · {mark}"
        elif live:
            mark = f"{live} live · {mark}"
        sys.stdout.write(f"commit review: {len(waiting)}/{BATCH} unreviewed · {mark}\n")
        return 0

    if baseline_error:
        sys.stdout.write(f"🔴 unreviewed count UNKNOWN — {baseline_error}\n")
    else:
        sys.stdout.write(f"unreviewed agent commits : {len(waiting)} (gate every {BATCH})\n")
        by_family: dict[str, int] = {}
        for row in waiting:
            by_family[row["family"]] = by_family.get(row["family"], 0) + 1
        if by_family:
            sys.stdout.write("  written by            : "
                             + ", ".join(f"{k} x{v}" for k, v in sorted(by_family.items())) + "\n")
    dispatched = [b for b in pending if (b.get("dispatch") or {}).get("ok")]
    queued = [b for b in pending if not (b.get("dispatch") or {}).get("ok")]
    run = next((str((b.get("dispatch") or {}).get("run")) for b in dispatched
                if (b.get("dispatch") or {}).get("run")), None)
    settled = settled_review_tasks(run)
    live = [b for b in dispatched if str(b["dispatch"].get("task")) not in settled]
    owed = [b for b in dispatched if str(b["dispatch"].get("task")) in settled]
    for batch in live:
        sys.stdout.write(f"  LIVE    {batch['id']} -> {batch['reviewer']} "
                         f"({batch['dispatch']['task']})\n")
    for batch in owed:
        sys.stdout.write(f"  OWED VERDICT  {batch['id']} -> {batch['reviewer']} "
                         f"({batch['dispatch']['task']}) · reviewer finished\n")
    if queued:
        sys.stdout.write(f"  queued  : {len(queued)} awaiting dispatch; "
                         f"next {', '.join(b['id'] for b in queued[:3])}\n")
    closed = [b for b in state["batches"] if b["status"] != "pending"]
    if closed:
        last = closed[-1]
        who = str(last.get("reviewer_name") or last["reviewer"])
        seen, found = reviewer_track_record(state, who)
        rate = f" · finds on {found}/{seen} ({round(100 * found / seen)}%)" if seen else ""
        warn = (" 🔴 THIS REVIEWER ALMOST NEVER FINDS ANYTHING — an `approved` from it is weak"
                if last["status"] == "approved" and seen >= 5 and found / seen < 0.30 else "")
        sys.stdout.write(f"  last verdict          : {last['id']} {last['status']} "
                         f"by {who}{rate}{warn}\n")
    if len(waiting) >= BATCH and not pending:
        sys.stdout.write("\nA review is DUE. Open it with: python tools/commit_review.py open\n")
    return 0


def command_baseline(args: argparse.Namespace) -> int:
    state = load_state()
    sha = git("rev-parse", args.at).strip()
    short = git("rev-parse", "--short", sha).strip()
    state["baseline"] = {"sha": sha, "short": short, "at": now(),
                         "why": args.why or "the rule starts from here"}
    save_state(state)
    render_page()
    sys.stdout.write(f"baseline set at {short}: {state['baseline']['why']}\n")
    waiting, baseline_error = _waiting_or_error(state)
    if baseline_error:
        sys.stdout.write(f"🔴 {baseline_error}\n")
    else:
        sys.stdout.write(f"{len(waiting)} commits now in scope\n")
    return 0


def command_open(args: argparse.Namespace) -> int:
    state = load_state()
    if [b for b in state["batches"] if b["status"] == "pending"] and not args.force:
        sys.stdout.write("A batch is already awaiting a verdict; settle it first (--force to add "
                         "a second).\n")
        return 1
    try:
        waiting = unreviewed(state)
    except BaselineOutsideHistory as exc:
        sys.stdout.write(f"🔴 refusing to open a batch: {exc}\n")
        return 1
    if len(waiting) < args.size and not args.force:
        sys.stdout.write(f"Only {len(waiting)} unreviewed commits, gate is {args.size}. "
                         "Nothing to open (--force to open a short batch).\n")
        return 1
    rows = waiting[:args.size]
    try:
        batch = make_batch(rows, args.reviewer, require_independent=args.require_independent)
    except NoIndependentReviewer as exc:
        sys.stdout.write(f"🔴 not opening a batch: {exc}\n")
        return 1
    state["batches"].append(batch)
    save_state(state)
    render_page()
    reg = _register_brief(batch)
    sys.stdout.write(f"opened {batch['id']}: {len(rows)} commits, "
                     f"{rows[0]['short']}..{rows[-1]['short']}\n")
    sys.stdout.write(f"register: {reg}\n")
    sys.stdout.write(f"reviewer: {batch['reviewer']} -- {batch['reviewer_why']}\n")
    if batch.get("same_family"):
        sys.stdout.write("⚠️  SAME-FAMILY (weaker) review: start the reviewer in a FRESH session; a different "
                         "family is better when you can get one.\n")
    sys.stdout.write(f"prompt  : {(PROMPTS / (batch['id'] + '.md')).relative_to(PROJECT)}\n")
    sys.stdout.write(f"launch  : {launch_hint(batch['reviewer'], (PROMPTS / (batch['id'] + '.md')).relative_to(PROJECT).as_posix())}\n")
    batch["manual_launch_printed_at"] = now()
    save_state(state)
    sys.stdout.write(f"⚠️  after you launch it by hand, CLOSE THE LOOP:\n"
                     f"    python tools/commit_review.py bind --batch {batch['id']} "
                     f"--task <task_id> --run <run_id>\n")
    return 0


def make_batch(rows: list[dict], reviewer_override: str | None = None,
               historical: bool = False, require_independent: bool = False) -> dict:
    """One batch record plus its brief on disk. Shared by `open` and `backfill`."""
    reviewer, why = pick_reviewer(rows, reviewer_override, require_independent)
    batch = {
        "id": f"CR-{rows[-1]['at'][:10].replace('-', '')}-{rows[-1]['short']}",
        "opened_at": now(),
        "commits": [r["sha"] for r in rows],
        "shorts": [r["short"] for r in rows],
        "authors": sorted({r["model"] for r in rows}),
        "author_families": sorted({r["family"] for r in rows}),
        "reviewer": reviewer,
        "reviewer_why": why,
        "same_family": why.startswith("SAME-FAMILY"),
        "same_family_accepted": why.startswith("SAME-FAMILY"),
        "status": "pending",
        "historical": historical,
        "findings": [],
    }
    write_prompt(batch, rows)
    return batch


def fallback_after_launch_failure(batch_id: str, detail: str) -> str | None:
    """Persist a failed reviewer route and select the next independent family, if any."""
    state = load_state()
    batch = next((row for row in state["batches"] if row["id"] == batch_id), None)
    if batch is None:
        raise ValueError(f"no such batch: {batch_id}")
    current = str(batch["reviewer"])
    candidates = [family for family in preference()
                  if family not in _batch_author_families(batch)]
    failures = batch.setdefault("launch_failures", [])
    failures.append({"family": current, "at": now(), "reason": detail[:500]})
    try:
        next_family = candidates[candidates.index(current) + 1]
    except (ValueError, IndexError):
        save_state(state)
        return None
    batch["reviewer"] = next_family
    batch["reviewer_why"] = (f"fallback after {current} could not launch: {detail[:180]}; "
                             f"{next_family} did not author this batch")
    batch.pop("dispatch", None)
    batch["dispatch_attempts"] = 0
    save_state(state)
    return next_family


def command_backfill(args: argparse.Namespace) -> int:
    """Open the whole pre-baseline history as batches, so old work is graded too."""
    state = load_state()
    seen = reviewed_shas(state)
    rows = [c for c in commits(limit=args.depth)
            if c["sha"] not in seen and c["family"] not in ("human", "other")]
    if not rows:
        sys.stdout.write("nothing left to backfill\n")
        return 0
    chunks = [rows[i:i + args.size] for i in range(0, len(rows), args.size)]
    if args.batches:
        chunks = chunks[:args.batches]
    spread = [f.strip() for f in args.spread.split(",") if f.strip()] if args.spread else []
    opened = []
    for index, chunk in enumerate(chunks):
        if len(chunk) < args.size and not args.allow_short:
            continue
        forced = args.reviewer or (spread[index % len(spread)] if spread else None)
        try:
            batch = make_batch(chunk, forced, historical=True)
        except NoIndependentReviewer as exc:
            sys.stdout.write(f"🔴 stopping the backfill: {exc}\n")
            break
        state["batches"].append(batch)
        opened.append(batch)
    save_state(state)
    render_page()
    sys.stdout.write(f"opened {len(opened)} historical batches "
                     f"({sum(len(b['commits']) for b in opened)} commits)\n")
    for batch in opened:
        sys.stdout.write(f"  {batch['id']:26} {batch['shorts'][0]}..{batch['shorts'][-1]} "
                         f"-> {batch['reviewer']}\n")
    return 0


def _range(rows: list[dict]) -> str:
    """Commands that show exactly this batch, valid even when it starts at the root commit."""
    first, last = rows[0]["sha"], rows[-1]["sha"]
    try:
        git("rev-parse", "--verify", f"{first}^")
    except RuntimeError:
        selected_range = last
        root = True
    else:
        selected_range = f"{first}~1..{last}"
        root = False
    selected = [row["sha"] for row in rows]
    resolved = git("rev-list", "--reverse", selected_range).split()
    if resolved != selected:
        shas = " ".join(selected)
        return ("# Exact selected commits (not a contiguous history range; do not add intervening commits).\n"
                f"git log --format=%H --no-walk=unsorted {shas}\n"
                f"git show --format=fuller {shas}")
    if root:
        return (f"git log --oneline {last}\n"
                f"git show {first}   # {rows[0]['short']} is the ROOT commit -- it has no parent\n"
                f"git diff {first}..{last}")
    return f"git log --oneline {first}~1..{last}\ngit diff {first}~1..{last}"


def _weaker_notice(batch: dict) -> str:
    if not batch.get("same_family"):
        return ""
    return """## ⚠️ Same-family review (weaker) — read this first

No reviewer from another family was available, so this review comes from the same family that wrote the code. That
shares its blind spots, so work against them:

- You are in a **FRESH session**: you have not seen the authors' reasoning and you must not reconstruct it. Judge the diff
  and the live files only.
- Assume the code is wrong until a command proves it right; do not accept a claim because it sounds like something you
  would have written.
- Spend your effort on what the authors' family tends to miss: unverified numbers, an error path nobody exercised, a test
  that asserts nothing, a doc that says more than the code does.
- A different family reviewing this would be better; say in your verdict if something deserves that second look.

"""


def write_prompt(batch: dict, rows: list[dict]) -> Path:
    """The reviewer's brief. Written to disk so the Worker reads a file, not a pasted wall."""
    PROMPTS.mkdir(parents=True, exist_ok=True)
    path = PROMPTS / f"{batch['id']}.md"
    table = "\n".join(f"| `{r['short']}` | {r['model']} | {r['subject']} |" for r in rows)
    body = f"""# Commit review {batch['id']}

You are the **second model** on work written by another. Two different models always re-check
each other, every {BATCH} commits. This batch was written by **{', '.join(batch['authors'])}**;
you are **{batch['reviewer']}** -- {batch['reviewer_why']}.

{_weaker_notice(batch)}## Range

```
{_range(rows)}
```

**Selected commits: {len(rows)}.**

| commit | written by | subject |
|---|---|---|
{table}

## What a finding is

Report only what would change the owner's code, data, or decisions:

- a claim in a commit message that the diff does not support, or that the live files contradict;
- a measurement that cannot be reproduced from the raw files;
- a rule or register the change violates (`AGENTS.md`, `OPEN.md`);
- a defect the change introduces: wrong ids, an inverted value, a broken sync, a test that asserts
  nothing, an error path that can block the owner;
- work claimed as done that is not done on disk.

**Not a finding:** naming, formatting, or an approach you would have chosen differently but which is
correct as written. Say so explicitly if you find nothing -- an empty verdict is a real result.

## 🔴 Before you report anything: is it still true at HEAD?

This batch is history. Most of it has been edited since, and some of its defects were already found
and fixed by later commits. A finding that was repaired is not work. So for every candidate finding,
check the file **as it is at HEAD**:

```
git log --oneline -S'<the exact broken text>' -- <path>   # which commit changed it, and when
git show HEAD:<path> | sed -n '<line>,<line+20>p'         # what it says today
```

Then label the finding one of two ways:

- **LIVE** -- reproduced against the current file. Give the HEAD line number.
- **FIXED BY `<sha>`** -- the defect was real in this batch and a later commit repaired it. Report it
  in one sentence naming that commit, and do **not** report it as work.

If the whole batch is FIXED-BY findings and nothing is live, the verdict is still `approved`.

## How to answer — write the file as you go, then report

**The file is the deliverable, and the `worker_done` is only a notification that it exists.** A
verdict that is not on disk at the moment this terminal dies does not exist.

### Your answer file

```
work/agents/reports/reviews/{batch['id']}_REVIEW.md
```

That exact name. The tool that reads verdicts (`commit_review.py verdict`) looks for
`{batch['id']}_REVIEW.md`; a verdict it cannot find is a verdict nobody can accept.

Write it incrementally — after EVERY established fact, append it to the file BEFORE running the
next command. The verdict is the last line you add.

### The three markers the tool parses

Keep each at the **start of a line, undecorated** — no `## `, no `**bold**`:

1. `VERDICT: approved` or `VERDICT: findings`
2. `FINDINGS` on a line by itself, then one line per finding that OPENS with `LIVE — ` or
   `FIXED BY <sha> — `, then the commit, the file and **HEAD** line, what is wrong, and how you
   proved it. If there is nothing to report, that block is the single line `None.`
3. `CHECKED AND CLEAN (do not redo)` on a line by itself, then one line per thing you verified clean.

### Then report

Send a `worker_done` whose body contains the same `VERDICT:` line and the path of the file you wrote.

**Do not edit any other file. This is review-only: a `worker_done` reports findings, it does not
authorize you to fix them.**
"""
    path.write_text(body, encoding="utf-8")
    return path


def command_bind(args: argparse.Namespace) -> int:
    """Attach a Task to a batch a Producer opened and launched by hand."""
    state = load_state()
    batch = next((b for b in state["batches"] if b["id"] == args.batch), None)
    if batch is None:
        sys.stdout.write(f"no such batch: {args.batch}\n")
        return 1
    if batch.get("status") != "pending":
        sys.stdout.write(f"{args.batch} is already {batch['status']}; not binding.\n")
        return 1
    batch["dispatch"] = {"task": args.task, "run": args.run, "ok": True, "at": now(),
                         "attempt": int(batch.get("dispatch_attempts") or 0) + 1,
                         "detail": args.detail or "bound by the Producer after a manual launch"}
    batch["dispatch_attempts"] = batch["dispatch"]["attempt"]
    save_state(state)
    sys.stdout.write(f"{args.batch}: bound to {args.task} on {args.run}\n")
    return 0


def command_verdict(args: argparse.Namespace) -> int:
    state = load_state()
    batch = next((b for b in state["batches"] if b["id"] == args.batch), None)
    if batch is None:
        sys.stdout.write(f"no such batch: {args.batch}\n")
        return 1
    reviewer_name = str(getattr(args, "reviewer", None) or batch["reviewer"]).strip()
    reviewer_family = _reviewer_family(reviewer_name)
    author_families = _batch_author_families(batch)
    if reviewer_family in author_families and not batch.get("same_family_accepted"):
        sys.stdout.write(
            f"{batch['id']}: refusing same-family review; reviewer {reviewer_name!r} resolves "
            f"to {reviewer_family!r}, which authored this batch ({sorted(author_families)}).\n")
        return 1
    dispatch = batch.get("dispatch") or {}
    task = str(dispatch.get("task") or "")
    if (not getattr(args, "force", False) and dispatch.get("ok") and task
            and batch.get("status") == "pending"
            and task not in settled_review_tasks(str(dispatch.get("run") or ""))):
        sys.stdout.write(
            f"{batch['id']}: its reviewer is STILL RUNNING ({task}).\n"
            f"  Recording a verdict now closes the batch and discards that review.\n"
            f"  Wait for worker_done, or pass --force if you are deliberately overriding it.\n")
        return 1

    artifact, artifact_refusal = _review_artifact_choice(batch)
    if artifact_refusal and not getattr(args, "force", False):
        sys.stdout.write(artifact_refusal)
        return 1
    artifact_verdict, artifact_findings = _parse_review_artifact(artifact)
    cli_findings = [_strip_list_marker(str(finding)) for finding in (getattr(args, "finding", None) or [])
                    if str(finding).strip()]
    unlabelled = [c for c in cli_findings if not _is_finding_line(c)]
    if unlabelled:
        cli_findings = [c for c in cli_findings if _is_finding_line(c)]
        sys.stdout.write(
            f"{batch['id']}: ignored {len(unlabelled)} --finding line(s) that do not open with "
            "`LIVE — ` / `FIXED BY <sha> — `:\n"
            + "".join(f"  - {c[:120]}\n" for c in unlabelled))
    cli_findings = [c for c in cli_findings if not _is_copy_of(c, artifact_findings)]
    findings = list(dict.fromkeys(artifact_findings + cli_findings))
    live_findings = [f for f in findings if not _FIXED_LABEL.match(f)]
    if args.status == "approved" and (artifact_verdict == "findings" or live_findings):
        source = _artifact_label(artifact) if artifact else "the --finding arguments"
        sys.stdout.write(
            f"{batch['id']}: refusing approved; {source} contains {len(findings)} finding(s).\n"
            "  Record --status findings so the work reaches POLISHING_TODO.md.\n")
        return 1
    if args.status == "findings" and not findings:
        source = _artifact_label(artifact) if artifact else "the review input"
        sys.stdout.write(
            f"{batch['id']}: refusing findings with zero finding lines; {source} was empty or "
            "unreadable.\n"
            "  Add --finding entries or write a plain-list FINDINGS block before recording it.\n")
        return 1
    batch["status"] = args.status
    batch["reviewer_name"] = reviewer_name
    batch["closed_at"] = now()
    batch["findings"] = findings
    batch["fixed_since"] = [_strip_list_marker(str(f)) for f in (args.fixed_by or []) if str(f).strip()]
    relabelled = [f for f in batch["findings"] if _FIXED_LABEL.match(f)]
    if relabelled:
        batch["findings"] = [f for f in batch["findings"] if not _FIXED_LABEL.match(f)]
        batch["fixed_since"] = [f for f in batch["fixed_since"]
                                if not _is_copy_of(f, relabelled, kind="fixed")]
        batch["fixed_since"] = relabelled + batch["fixed_since"]
    batch["fixed_since"] = list(dict.fromkeys(batch["fixed_since"]))
    batch["fixed_since"] = [f for f in batch["fixed_since"]
                            if not any(o != f and o.startswith(f) for o in batch["fixed_since"])]
    batch["note"] = args.note or ""
    save_state(state)
    if batch["findings"] or batch["fixed_since"]:
        append_polishing(batch)
    render_page()
    reg = _register_report(
        artifact or PROMPTS / f"{batch['id']}_REVIEW.md", date=batch["closed_at"][:10],
        domain=(f"{'same-family (weaker)' if batch.get('same_family') else 'cross-family'} commit review, "
                f"{len(batch['commits'])} commits reviewed by {batch['reviewer_name']}"),
        status="routed-TODO" if batch["findings"] else "routed-PAGE",
        destination=(f"`{batch['id']}` findings filed in "
                     "`work/agents/registers/POLISHING_TODO.md`."
                     if batch["findings"] else
                     "`work/agents/state/COMMIT_JOURNAL.md` records the verdict."))
    brief_reg = _register_brief(batch)
    weaker = " [same-family (weaker)]" if batch.get("same_family") else ""
    sys.stdout.write(f"{batch['id']}: {args.status} by {batch['reviewer_name']}{weaker}"
                     f" ({len(batch['findings'])} findings)\n")
    sys.stdout.write(f"register: {reg} (review), {brief_reg} (brief)\n")
    if reg == "missing":
        sys.stdout.write(
            f"  no {batch['id']}_REVIEW.md on disk under reports/reviews/ -- register that row "
            "by hand once the review file exists, or re-run verdict after writing it.\n")
    return 0


def _register_brief(batch: dict) -> str:
    """Register the batch's brief document in REPORTS.md, idempotently."""
    if not batch.get("opened_at"):
        return "missing"
    return _register_report(
        PROMPTS / f"{batch['id']}.md", date=batch["opened_at"][:10],
        domain=f"cross-family commit-review brief, {len(batch['commits'])} commits, "
               f"written by {', '.join(batch['authors'])}",
        status="routed-PAGE",
        destination="`work/agents/state/COMMIT_JOURNAL.md` tracks this batch from opened "
                    "through its verdict.")


def _register_row_exists(text: str, cell: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = stripped.split("|")
        if len(parts) < 2:
            continue
        value = parts[1].strip().strip("`").strip()
        if not value or value.lower() == "path":
            continue
        if value == cell or cell.endswith(f"/{value}"):
            return True
    return False


def _register_report(path: Path, *, date: str, domain: str, status: str, destination: str) -> str:
    """Append one row to REPORTS.md's live register for `path`, unless it is already registered."""
    if not path.exists() or not REPORTS.exists():
        return "missing"
    try:
        cell = path.resolve().relative_to(PROJECT.resolve()).as_posix()  # one canonical repo path
    except ValueError:
        cell = path.as_posix()
    raw = REPORTS.read_bytes().decode("utf-8")
    if _register_row_exists(raw, cell):
        return "exists"
    eol = "\r\n" if "\r\n" in raw else "\n"
    row = (f"| {_cell(cell)} | {date} | {_cell(domain)} | {status} | {_cell(destination)} |{eol}")
    anchor = re.search(r"(?m)^## Archived ledger", raw)
    head = anchor.start() if anchor else -1
    if head == -1:
        text = raw.rstrip("\r\n") + eol + row
    else:
        prefix = raw[:head].rstrip("\r\n")
        text = prefix + (eol if prefix else "") + row + eol + raw[head:]
    REPORTS.write_bytes(text.encode("utf-8"))
    return "added"


def _cell(text: object) -> str:
    """Free text safe inside a Markdown table cell: one line, pipes escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")


_FIXED_LABEL = re.compile(r"^\W*(?:FIXED[ -]BY|ALREADY[ -]FIXED)\b")


POLISH_HEADER = (
    "# Polishing TODO — findings of commit reviews and small cleanups\n\n"
    "Row shape: `- [ ] L<n> (<review id>, <date>) <finding>`; done rows get `[x]` and the fixing commit.\n"
    "Rows raised by a commit review come from a model family that did not write the code; an\n"
    "`already fixed` row is kept as evidence this ground was covered, but it is never work.\n\n")


def append_polishing(batch: dict) -> None:
    """Findings go into a register, not into a chat message.

    Writes the shipped bullet shape (`- [ ] L<n> (<review id>, <date>) <finding>`). A register that
    already uses the older table shape keeps getting table rows, so an existing file is never
    scrambled; a missing file is created in the bullet shape. Already-recorded rows are not
    duplicated.
    """
    POLISH.parent.mkdir(parents=True, exist_ok=True)
    if not POLISH.exists():
        POLISH.write_text(POLISH_HEADER, encoding="utf-8")
    text = POLISH.read_text(encoding="utf-8")
    who = batch.get("reviewer_name") or batch["reviewer"]
    date = str(batch.get("closed_at") or now())[:10]
    table = "| batch |" in text
    number = max([int(n) for n in re.findall(r"\bL(\d+)\b", text)] or [0])
    rows: list[str] = []
    todo = [(f, False) for f in batch["findings"]] + [(f, True) for f in batch.get("fixed_since", [])]
    for finding, fixed in todo:
        clean = " ".join(str(finding).split())
        if table:
            row = f"| {batch['id']} | {_cell(who)} | {_cell(clean)} | {'already fixed' if fixed else 'open'} |\n"
            duplicate = row in text
        else:
            row = (f"- [{'x' if fixed else ' '}] L{number + 1} ({batch['id']}, {date}, {who}) {clean}"
                   f"{' — already fixed' if fixed else ''}\n")
            duplicate = any(batch["id"] in line and clean in line for line in text.splitlines())
            if not duplicate:
                number += 1
        if duplicate:
            continue
        rows.append(row)
        text += row
    if rows:
        with POLISH.open("a", encoding="utf-8", newline="") as handle:
            handle.write("".join(rows))


def render_page() -> None:
    """The owner-facing journal: who wrote what, and what the second model said about it."""
    state = load_state()
    rows = commits()
    graded = {}
    for batch in state["batches"]:
        for sha in batch["commits"]:
            graded[sha] = batch
    waiting, baseline_error = _waiting_or_error(state)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["model"]] = counts.get(row["model"], 0) + 1

    headline = (f"🔴 {baseline_error}" if baseline_error
                else f"**{len(waiting)}** unreviewed now")
    out = ["# Commit journal — who wrote it, and who checked it", "",
           "Derived from `git log` on every run; only the verdicts are stored, so this page can",
           "never disagree with the repository about who wrote what. Every ten commits a *different*",
           "model family reviews them, and its verdict is recorded as `approved` or as findings in",
           "[`POLISHING_TODO.md`](../registers/POLISHING_TODO.md).", "",
           f"Rebuilt: **{now()}** · gate every **{BATCH}** commits · {headline}", ""]
    baseline = state.get("baseline")
    if baseline:
        out += [f"Baseline: the rule starts after **`{baseline['short']}`** — "
                f"{baseline['why']}. Commits before it are out of scope.", ""]
    out += ["## Who has been writing", "", "| model | commits (last 400) |", "|---|---|"]
    for model, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| {model} | {count} |")

    out += ["", "## Reviews", ""]
    if not state["batches"]:
        out.append("_No batch has been reviewed yet._")
    else:
        out += ["| batch | commits | written by | reviewer | verdict | findings |",
                "|---|---|---|---|---|---|"]
        older = state["batches"][:-PAGE_BATCHES]
        for batch in [b for b in older if b["status"] == "pending"] + state["batches"][-PAGE_BATCHES:]:
            mark = {"approved": "✅ approved", "findings": "🔴 findings",
                    "pending": "⏳ pending"}.get(batch["status"], batch["status"])
            out.append(f"| {batch['id']} | {len(batch['commits'])} "
                       f"(`{batch['shorts'][0]}`..`{batch['shorts'][-1]}`) | "
                       f"{', '.join(batch['authors'])} | "
                       f"{batch.get('reviewer_name') or batch['reviewer']}"
                       f"{' (same-family, weaker)' if batch.get('same_family') else ''} | {mark} | "
                       f"{len(batch['findings'])} |")

    if any(b.get("same_family") for b in state["batches"]):
        out += ["", "_«same-family, weaker»: no reviewer from another family was available, so a fresh session of "
                "the authors' own family checked it. Better than no review; a different family is better._"]
    out += ["", "## Commits awaiting review", ""]
    if baseline_error:
        out.append(f"🔴 **The queue cannot be counted** — {baseline_error}")
    elif not waiting:
        out.append("_None — every agent commit is inside a reviewed batch._")
    else:
        out += ["| commit | written by | subject |", "|---|---|---|"]
        for row in waiting[-BATCH * 2:]:
            out.append(f"| `{row['short']}` | {row['model']} | {row['subject']} |")

    out += ["", "## The last 30 commits, graded", "",
            "| commit | written by | checked by | subject |", "|---|---|---|---|"]
    for row in rows[-30:]:
        batch = graded.get(row["sha"])
        if batch is None:
            checked = "—"
        elif batch["status"] == "pending":
            checked = f"⏳ {batch['reviewer']}"
        else:
            checked = ("✅ " if batch["status"] == "approved" else "🔴 ") + \
                      str(batch.get("reviewer_name") or batch["reviewer"]) + \
                      (" (same-family, weaker)" if batch.get("same_family") else "")
        out.append(f"| `{row['short']}` | {row['model']} | {checked} | {row['subject']} |")
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    PAGE.write_text("\n".join(out) + "\n", encoding="utf-8")


def command_journal(_args: argparse.Namespace) -> int:
    render_page()
    sys.stdout.write(f"wrote {PAGE.relative_to(PROJECT)}\n")
    return 0


def command_show(args: argparse.Namespace) -> int:
    state = load_state()
    batch = next((b for b in state["batches"] if b["id"] == args.batch), None)
    if batch is None:
        sys.stdout.write(f"no such batch: {args.batch}\n")
        return 1
    sys.stdout.write(json.dumps(batch, indent=2, ensure_ascii=False) + "\n")
    return 0


# Environment markers that say "an agent is running this git command". A human typing in a plain
# terminal has none of them.
AGENT_ENV = ("ORCA_TERMINAL_HANDLE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_HOME",
             "CODEX_SANDBOX", "OPENCODE", "ORCA_AGENT_HOOK_VERSION")
_TRAILER = re.compile(r"^(Co-Authored-By|Authored-By):\s*\S", re.IGNORECASE | re.MULTILINE)
_HUMAN_TRAILER = re.compile(r"^Authored-By:\s*human\b", re.IGNORECASE | re.MULTILINE)


def authorship_problem(message: str, environ: dict | None = None) -> str | None:
    """Why a commit message lacks the authorship the tenth-commit review needs, or None.

    A commit made from an agent's terminal must name its author: a `Co-Authored-By: <model>` trailer,
    or `Authored-By: human` when a person really wrote it. Without that the commit would count as
    human and silently escape the cross-family review.
    """
    env = os.environ if environ is None else environ
    if message.lstrip().startswith(("Merge ", "Revert ")) or _TRAILER.search(message):
        return None
    if not any(env.get(name) for name in AGENT_ENV):
        return None
    return ("this commit has no authorship trailer, and it is being made from an agent terminal. "
            "Add `Co-Authored-By: <model name> <noreply@...>` (the model that wrote it) or, if a "
            "person wrote it, `Authored-By: human`. Without it the commit escapes the cross-family review.")


def command_check_msg(args: argparse.Namespace) -> int:
    """The commit-msg hook body (installed by `knowledge_gate.py install`): exit 1 = refused."""
    message = Path(args.file).read_text(encoding="utf-8", errors="replace")
    problem = authorship_problem(message)
    if problem:
        sys.stderr.write(f"\nCOMMIT REVIEW authorship check refused this commit:\n- {problem}\n\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="how many commits are unreviewed, and by whom written")
    status.add_argument("--quiet", action="store_true", help="one line, for a dashboard")
    status.set_defaults(func=command_status)

    opened = sub.add_parser("open", help="open the next review batch and write the reviewer's brief")
    opened.add_argument("--size", type=int, default=BATCH)
    opened.add_argument("--reviewer", help="override the automatic family pick")
    opened.add_argument("--force", action="store_true", help="open a short batch, or a second one")
    opened.add_argument("--require-independent", action="store_true",
                        help="refuse (exit 1) when no reviewer family is independent of the authors, "
                             "instead of taking the default same-family (weaker) fresh-session review")
    opened.add_argument("--accept-same-family", action="store_true", help=argparse.SUPPRESS)  # old name, now the default
    opened.set_defaults(func=command_open)

    verdict = sub.add_parser("verdict", help="record a reviewer's answer; findings go to the register")
    verdict.add_argument("--batch", required=True)
    verdict.add_argument("--status", required=True, choices=("approved", "findings"))
    verdict.add_argument("--reviewer", help="the concrete model, e.g. gpt-5.6-sol")
    verdict.add_argument("--finding", action="append",
                         help="repeatable; one line each, opening with `LIVE — ` "
                              "(or `FIXED BY <sha> — `); an unlabelled line is ignored")
    verdict.add_argument("--fixed-by", action="append", dest="fixed_by",
                         help="repeatable; a real defect in this batch that a LATER commit already "
                              "repaired. Recorded, never filed as work. Name the fixing commit")
    verdict.add_argument("--note", help="one line on what was checked and found clean")
    verdict.add_argument("--force", action="store_true",
                         help="record the verdict even though Orca says the reviewer is still "
                              "running -- this DISCARDS that review; only for a deliberate override")
    verdict.set_defaults(func=command_verdict)

    bind = sub.add_parser("bind", help="attach a Task to a batch opened and launched by hand")
    bind.add_argument("--batch", required=True)
    bind.add_argument("--task", required=True)
    bind.add_argument("--run", required=True)
    bind.add_argument("--detail")
    bind.set_defaults(func=command_bind)

    back = sub.add_parser("backfill", help="open the pre-baseline history as batches of --size, "
                                           "each marked historical, for parallel review")
    back.add_argument("--size", type=int, default=BATCH)
    back.add_argument("--batches", type=int, help="open at most this many; omit for all")
    back.add_argument("--depth", type=int, default=1000, help="how far back to read git log")
    back.add_argument("--reviewer", help="force one family for every batch opened now")
    back.add_argument("--spread", help="comma-separated families to round-robin across")
    back.add_argument("--allow-short", action="store_true",
                      help="also open the final under-sized chunk")
    back.set_defaults(func=command_backfill)

    base = sub.add_parser("baseline", help="declare where the rule starts; earlier commits are out "
                                           "of scope, and the page says so")
    base.add_argument("--at", required=True, help="commit-ish; this one and everything before it")
    base.add_argument("--why", help="one line, recorded on the page")
    base.set_defaults(func=command_baseline)

    check = sub.add_parser("check-msg", help="the commit-msg hook: refuse an agent commit with no "
                                             "authorship trailer")
    check.add_argument("file")
    check.set_defaults(func=command_check_msg)

    journal = sub.add_parser("journal", help="rewrite the owner-facing journal page")
    journal.set_defaults(func=command_journal)

    show = sub.add_parser("show", help="print one batch as JSON")
    show.add_argument("--batch", required=True)
    show.set_defaults(func=command_show)

    args = parser.parse_args(argv)
    paths.console_safe()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""What the owner never got round to answering — collected before it can be forgotten.

The failure this closes is **not** his memory; it is that the question disappears with
the session that asked it. A Producer asks something mid-turn, he is doing three things at
once, the answer never comes — and at the next rotation the question is gone, because the only
place it ever lived was a chat message.

Three deliberate choices:

1. **This is a register of QUESTIONS, not of work.** A row is a thing only he can settle.
2. **Every row carries what it BLOCKS and what it costs to leave open.** "Waiting on the owner"
   is not a status, it is a bill.
3. **Rows expire, and expiring one is a real outcome.** `close --stale` records that a question
   stopped mattering, with the reason — different from answering it, and it must not read the same.

Before a row is written, the ruling gate checks whether an owner ruling already exists; a found
ruling refuses the question. Sweeping is the Producer's job and cannot be a script: deciding
whether a message answered a question is a judgement. This file guarantees that once swept, the
result survives the session.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

import owner_text
import paths
import ruling_gate
import state_io

PROJECT = paths.PROJECT
STATE = paths.UNANSWERED_STATE
# Lives in `work/` root beside the digest, not under agents/registers/: he is the only reader
# who matters here, and a page among internal registers is a page he does not find.
PAGE = paths.UNANSWERED_PAGE

PRIORITY = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    """The question register. A corrupt file raises StateCorrupt - it is never replaced by an empty
    register (that would erase every open question without a trace)."""
    state = state_io.load_json(STATE, {"rows": []})
    state.setdefault("rows", [])
    return state


def save(state: dict) -> None:
    state_io.save_json(STATE, state)


def next_id(rows: list[dict]) -> str:
    """The next `U###`: one past the highest number ever issued, never the row count."""
    numbers = [int(m.group(1)) for r in rows if (m := re.fullmatch(r"U(\d+)", str(r.get("id", ""))))]
    return f"U{max(numbers, default=0) + 1:03d}"


def cell(text: object) -> str:
    """Make free text safe inside a Markdown table cell: no pipes, no line breaks."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _gate_warning(result: ruling_gate.GateResult) -> str:
    details = "; ".join(f"{source}: {error}" for source, error in result.unreadable_sources)
    return f"RULING GATE ERROR / UNCHECKED: {details or result.summary}"


def command_add(args: argparse.Namespace) -> int:
    supersedes = bool(getattr(args, "supersedes", False))
    supersedes_reason = getattr(args, "supersedes_reason", None) or ""
    if supersedes and not supersedes_reason.strip():
        sys.stdout.write("--supersedes requires a non-empty --supersedes-reason/--reason\n")
        return 2
    if not supersedes and supersedes_reason.strip():
        sys.stdout.write("--supersedes-reason/--reason requires --supersedes\n")
        return 2

    gate = ruling_gate.check_ruling(text=args.question, repo_path=PROJECT)
    if gate.status == "FOUND" and not supersedes:
        sys.stdout.write("Question refused: an existing owner ruling was found; nothing was recorded.\n")
        sys.stdout.write(ruling_gate.format_report(gate) + "\n")
        return 1

    with state_io.locked(STATE):
        return _add_locked(args, gate, supersedes, supersedes_reason)


def _add_locked(args, gate, supersedes, supersedes_reason) -> int:
    state = load()
    row = {
        "id": next_id(state["rows"]),
        "asked_at": args.at or now(),
        "question": args.question,
        "blocks": args.blocks or "",
        "cost": args.cost or "",
        "priority": args.priority,
        "session": args.session or "",
        "status": "open",
        "ruling_gate": gate.status,
        "ruling_gate_summary": gate.summary,
        "ruling_gate_unreadable_sources": [
            {"source": source, "error": error}
            for source, error in gate.unreadable_sources
        ],
        "supersedes": supersedes,
        "supersedes_reason": supersedes_reason,
    }
    state["rows"].append(row)
    save(state)
    render()
    if gate.status in {"ERROR", "UNCHECKED"}:
        sys.stdout.write(_gate_warning(gate) + "\n")
    elif supersedes:
        sys.stdout.write(f"RULING GATE FOUND overridden by --supersedes: {supersedes_reason}\n")
    sys.stdout.write(f"{row['id']} open ({row['priority']}): {row['question'][:80]}\n")
    return 0


def command_close(args: argparse.Namespace) -> int:
    with state_io.locked(STATE):
        return _close_locked(args)


def _close_locked(args: argparse.Namespace) -> int:
    state = load()
    row = next((r for r in state["rows"] if r["id"] == args.id), None)
    if row is None:
        sys.stdout.write(f"no such row: {args.id}\n")
        return 1
    row["status"] = "stale" if args.stale else "answered"
    row["closed_at"] = now()
    row["reason"] = args.reason or ""
    save(state)
    render()
    sys.stdout.write(f"{row['id']} -> {row['status']}\n")
    return 0


def command_list(args: argparse.Namespace) -> int:
    state = load()
    rows = [r for r in state["rows"] if r["status"] == "open"]
    if args.quiet:
        if not rows:
            sys.stdout.write("unanswered: none\n")
            return 0
        worst = min(r["priority"] for r in rows)
        sys.stdout.write(f"unanswered: {len(rows)} waiting on him · worst {worst}\n")
        return 0
    if not rows:
        sys.stdout.write("nothing waiting on him\n")
        return 0
    for row in sorted(rows, key=lambda r: r["priority"]):
        sys.stdout.write(f"{row['id']} {row['priority']} {row['asked_at'][:10]} {row['question']}\n")
        if row["blocks"]:
            sys.stdout.write(f"      blocks: {row['blocks']}\n")
    return 0


def render() -> None:
    """The owner-facing page, in the owner's language (`producer.toml owner_language`)."""
    state = load()
    rows = state["rows"]
    open_rows = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] != "open"]
    t = owner_text.text

    out = [
        t("unanswered.title"), "",
        t("unanswered.rebuilt", when=now(), open=len(open_rows), closed=len(closed)), "",
    ]

    if open_rows:
        out += [t("unanswered.waiting"), "", t("unanswered.columns"), "|---|---|---|---|---|---|"]
        for row in sorted(open_rows, key=lambda r: (r["priority"], r["asked_at"])):
            mark = PRIORITY.get(row["priority"], "")
            question = cell(row["question"])
            if row.get("supersedes"):
                reason = cell(row.get("supersedes_reason") or t("unanswered.no_reason"))
                question += f"<br>⚠️ **Supersedes:** {reason}"
            if row.get("ruling_gate") in {"ERROR", "UNCHECKED"}:
                question += f"<br>⚠️ **RULING GATE ERROR / UNCHECKED:** {t('unanswered.gate_unchecked')}"
            out.append(f"| {mark} | {row['id']} | {question} | {cell(row['blocks']) or '—'} | "
                       f"{cell(row['cost']) or '—'} | {row['asked_at'][:16].replace('T', ' ')} |")
    else:
        out += [t("unanswered.waiting"), "", t("unanswered.empty")]

    if closed:
        out += ["", t("unanswered.recent_closed", total=len(closed)), "", t("unanswered.closed_columns"),
                "|---|---|---|---|"]
        for row in closed[-5:]:
            word = t("unanswered.answered") if row["status"] == "answered" else t("unanswered.stale")
            out.append(f"| {row['id']} | {cell(row['question'])[:110]} | {word} | "
                       f"{cell(row.get('reason') or '') or '—'} |")

    out.append("")
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    PAGE.write_text("\n".join(out) + "\n", encoding="utf-8")


def command_render(_args: argparse.Namespace) -> int:
    render()
    sys.stdout.write(f"wrote {PAGE.relative_to(PROJECT)}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="record a question he has not answered")
    add.add_argument("--question", required=True)
    add.add_argument("--blocks", help="what stays stuck until he answers")
    add.add_argument("--cost", help="what leaving it open actually costs")
    add.add_argument("--priority", default="P2", choices=tuple(PRIORITY))
    add.add_argument("--at", help="when it was asked; omit for now()")
    add.add_argument("--session", help="session id, if known")
    add.add_argument("--supersedes", action="store_true",
                     help="allow a new question despite a prior ruling; requires a reason")
    add.add_argument("--supersedes-reason", "--reason", dest="supersedes_reason",
                     help="mandatory reason explaining why the prior ruling is being revisited")
    add.set_defaults(func=command_add)

    close = sub.add_parser("close", help="he answered it, or it stopped mattering")
    close.add_argument("--id", required=True)
    close.add_argument("--stale", action="store_true",
                       help="closed as no longer relevant rather than answered — a real outcome, "
                            "and it must not read the same as an answer")
    close.add_argument("--reason")
    close.set_defaults(func=command_close)

    listing = sub.add_parser("list", help="what is still waiting on him")
    listing.add_argument("--quiet", action="store_true", help="one line, for a dashboard")
    listing.set_defaults(func=command_list)

    page = sub.add_parser("render", help="rewrite the owner-facing page")
    page.set_defaults(func=command_render)

    args = parser.parse_args(argv)
    paths.console_safe()
    try:
        return args.func(args)
    except state_io.StateCorrupt as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

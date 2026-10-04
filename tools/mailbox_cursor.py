"""Read the Producer's mailbox log BY CURSOR, never by clock.

The drainer (drain_mailbox.py) acks every Delivery, so `check --peek` shows nothing and the
mailbox is the actionable log (producer_mailbox.py). The cursor is the last PHYSICAL line (1-based)
the Producer has handled, kept in `.runtime/actionable.cursor` as `<line> <message-id>`; the log is
append-only and never rotated, so a line number never moves.

    mailbox_cursor.py            # = new: every line after the cursor, numbered (read-only)
    mailbox_cursor.py status     # one line: cursor, file size in lines, how many new, by type
    mailbox_cursor.py ack <N>    # handled through line N: moves the cursor forward (never back
                                 # without --force)

No cursor yet: `new` refuses to guess (exit 2) and shows the last lines with their numbers.
Never start from EOF: that is exactly the «since now» loss.
Exit codes: 0 ok · 2 no cursor / bad request.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import paths
import producer_mailbox

LOG = Path(os.environ.get("PRODUCER_ACTIONABLE_LOG", str(producer_mailbox.actionable_log())))
CURSOR = Path(os.environ.get("PRODUCER_ACTIONABLE_CURSOR", str(paths.RUNTIME / "actionable.cursor")))
SHOW_WITHOUT_CURSOR = 15
BODY_CHARS = 300


def complete_lines(log: Path) -> list[str]:
    """Every newline-terminated line; a torn last line (a write in progress) is not a line yet."""
    try:
        data = log.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines = data.split("\n")
    return lines[:-1]


def read_cursor(cursor: Path) -> tuple[int, str] | None:
    try:
        raw = cursor.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return None
    if not raw or not raw[0].isdigit():
        return None
    return int(raw[0]), (raw[1] if len(raw) > 1 else "")


def parse(line: str) -> dict:
    try:
        row = json.loads(line)
    except ValueError:
        return {"type": "unparseable", "subject": line[:120]}
    return row if isinstance(row, dict) else {"type": "unparseable"}


def new_rows(log: Path, cursor: Path) -> tuple[int | None, list[tuple[int, dict]]]:
    lines = complete_lines(log)
    mark = read_cursor(cursor)
    start = 0 if mark is None else mark[0]
    return (None if mark is None else mark[0]), [
        (number, parse(text)) for number, text in enumerate(lines, 1) if number > start and text.strip()
    ]


def render(number: int, row: dict) -> str:
    body = " ".join(str(row.get("body") or "").split())[:BODY_CHARS]
    head = f"{number:>6} {row.get('at', '?')} {row.get('type', '?'):<11} {row.get('id', '?')} · {row.get('subject', '')}"
    return head + (f"\n       {body}" if body else "")


def command_new(log: Path, cursor: Path) -> int:
    mark, rows = new_rows(log, cursor)
    if mark is None:
        lines = complete_lines(log)
        print(f"NO CURSOR ({cursor}). Not guessing «since now». The log has {len(lines)} lines; the "
              f"last {SHOW_WITHOUT_CURSOR} are below. Start after the last worker_done HANDOVER "
              f"names, then `mailbox_cursor.py ack <line>`.")
        for number, text in list(enumerate(lines, 1))[-SHOW_WITHOUT_CURSOR:]:
            print(render(number, parse(text)))
        return 2
    print(f"cursor {mark} · {len(rows)} new")
    for number, row in rows:
        print(render(number, row))
    return 0


def command_status(log: Path, cursor: Path) -> int:
    mark, rows = new_rows(log, cursor)
    total = len(complete_lines(log))
    kinds = Counter(str(row.get("type")) for _, row in rows)
    cursor_text = "none" if mark is None else str(mark)
    new_text = "?" if mark is None else f"{len(rows)} new" + (
        " (" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")" if kinds else "")
    print(f"mailbox {log}: {total} lines · cursor {cursor_text} · {new_text}")
    return 0 if mark is not None else 2


def command_ack(log: Path, cursor: Path, line: int, *, force: bool = False) -> int:
    lines = complete_lines(log)
    if not 1 <= line <= len(lines):
        print(f"line {line} is not in {log} (1..{len(lines)})", file=sys.stderr)
        return 2
    mark = read_cursor(cursor)
    if mark is not None and line < mark[0] and not force:
        print(f"refusing to move the cursor back {mark[0]} -> {line} (use --force)", file=sys.stderr)
        return 2
    message_id = str(parse(lines[line - 1]).get("id") or "-")
    cursor.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor.with_name(cursor.name + ".tmp")
    temporary.write_text(f"{line} {message_id}\n", encoding="utf-8")
    os.replace(temporary, cursor)
    print(f"cursor {line} {message_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--cursor", type=Path, default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], parents=[common])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("new", parents=[common])
    sub.add_parser("status", parents=[common])
    ack = sub.add_parser("ack", parents=[common])
    ack.add_argument("line", type=int)
    ack.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    args.log = getattr(args, "log", LOG)
    args.cursor = getattr(args, "cursor", CURSOR)
    if args.command == "status":
        return command_status(args.log, args.cursor)
    if args.command == "ack":
        return command_ack(args.log, args.cursor, args.line, force=args.force)
    return command_new(args.log, args.cursor)


if __name__ == "__main__":
    raise SystemExit(main())

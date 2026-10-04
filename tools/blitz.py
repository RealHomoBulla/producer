#!/usr/bin/env python3
"""blitz.py — the paperwork of an owner decision blitz: open the next queue, cross-check the last one.

A blitz is a session in its own Worker tab where the owner answers decision questions one at a time
(`work/agents/orca/OWNER_SHORTHANDS.md` «блиц»). Two files carry it:

  work/agents/registers/BLITZ<N>_READY.md    the questions waiting (the Producer writes them as they appear)
  work/agents/registers/BLITZ<N>_ACTIVE.md   the blitz Worker's record: `## Qn — asked <UTC> (<title>)` and
                                             `Answer (<UTC>): «<his words>»`, closed by a closure section

An answer that is not carried into TODO / OPEN / the brief is lost silently, which is exactly what this
tool exists to prevent.

    python tools/blitz.py status                  # open questions per READY file, and the active blitz
    python tools/blitz.py crosscheck [--blitz N]  # each answered Q of blitz N (default: the latest
                                                  # before the newest READY) -> done / queued / MISSING; exit 1 on MISSING
    python tools/blitz.py new                     # create BLITZ<next>_READY.md from the template and write
                                                  # the «Сверка прошлых блицев» table into it

How an answer is traced: the Q is looked for in `TODO.md`, `OPEN.md` and the brief by an explicit reference
(`BLITZ3-Q2`, `blitz 3 Q2`, `B3Q2`) or by its full title. Found in an OPEN row/section that is ruled, or in the
brief, or in a closed TODO row = **done**; in an open TODO row = **queued**; nowhere = **MISSING**. A question
asked but not yet answered is **unanswered** and is carried into the next READY file by hand.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import paths

REGISTERS = paths.AGENT_REGISTERS
READY_RE = re.compile(r"^BLITZ(\d+)_READY(?:_[^/]*)?\.md$")
ACTIVE_RE = re.compile(r"^BLITZ(\d+)_ACTIVE\.md$")
QUESTION_RE = re.compile(r"^#{2,3}\s*Q(\d+)\b(.*)$")
ANSWER_RE = re.compile(r"^\s*(?:\*\*)?(?:Answer|Ответ)\b[^:\n]*:(?:\*\*)?\s*(.*)$", re.IGNORECASE)
CLOSURE_RE = re.compile(r"^#{1,3}\s*(?:closure|закрыт)", re.IGNORECASE | re.MULTILINE)
CROSSCHECK_HEADING_RE = re.compile(r"^#{1,3}\s*(?:Cross-check of past blitzes|Сверка прошлых блицев)\s*$",
                                   re.IGNORECASE | re.MULTILINE)
CLOSED_MARKERS = ("✅", "closed", "done", "ruled", "решено", "закрыто", "выполнено")


@dataclass
class Question:
    blitz: int
    number: int
    title: str
    answer: str  # "" = unanswered


def numbered(directory: Path, pattern: re.Pattern) -> dict[int, Path]:
    found: dict[int, Path] = {}
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            match = pattern.match(path.name)
            if match:
                found[int(match.group(1))] = path
    return found


def _title(rest: str) -> str:
    """The question title from the rest of a `Q<n>` heading, in either shipped heading style."""
    paren = re.search(r"\(([^()]*)\)\s*$", rest)
    if paren:
        return paren.group(1).strip()
    return re.sub(r"^[\s·—–:-]+", "", rest).strip()


def parse_questions(path: Path, blitz: int) -> list[Question]:
    questions: list[Question] = []
    current: Question | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        heading = QUESTION_RE.match(line)
        if heading:
            current = Question(blitz, int(heading.group(1)), _title(heading.group(2)), "")
            questions.append(current)
            continue
        answer = ANSWER_RE.match(line)
        if answer and current is not None:
            text = answer.group(1).strip().strip("«»").strip()
            if text and not text.startswith(("<", "…")):
                current.answer = text
    return questions


def is_closed(path: Path) -> bool:
    return bool(CLOSURE_RE.search(path.read_text(encoding="utf-8", errors="replace")))


# ------------------------------------------------------------------ tracing an answer
def _ref_patterns(blitz: int, number: int) -> list[re.Pattern]:
    return [re.compile(rf"\bBLITZ\s*-?\s*{blitz}\s*[-_ ·]?\s*Q\s*{number}\b", re.IGNORECASE),
            re.compile(rf"\bB{blitz}\s*[-_ ]?\s*Q{number}\b", re.IGNORECASE)]


def _blocks(path: Path, by_heading: bool) -> list[tuple[int, str]]:
    """(line number, text) of the units a hit is judged in: whole `## ` sections (OPEN, brief) or single
    list rows / paragraphs (TODO), so a ruling written under a heading still belongs to that heading."""
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 1
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if by_heading:
            new_block = line.startswith("## ")
        else:
            new_block = line.startswith(("- ", "| ", "* ")) or not line.strip()
        if new_block and buffer:
            out.append((start, "\n".join(buffer)))
            buffer = []
        if line.strip() or by_heading:
            if not buffer:
                start = number
            buffer.append(line)
    if buffer:
        out.append((start, "\n".join(buffer)))
    return out


def _open_block_ruled(block: str) -> bool:
    """An OPEN section is closed when it carries a real ruling line (not the template placeholder) or a check mark."""
    import ruling_gate
    heading = block.splitlines()[0] if block else ""
    if "✅" in heading:
        return True
    return any(ruling_gate.label_content(line, ruling_gate._RULING_LABELS) is not None for line in block.splitlines())


def _todo_row_closed(block: str) -> bool:
    low = block.lower()
    return any(marker in low for marker in CLOSED_MARKERS)


def trace(question: Question, todo: Path, open_register: Path, briefs: list[Path]) -> tuple[str, str]:
    """(verdict, where) for one answered question: done / queued / MISSING."""
    refs = _ref_patterns(question.blitz, question.number)
    title = question.title.lower()
    best: tuple[str, str] | None = None
    for label, path in (("OPEN.md", open_register), ("TODO.md", todo), *[("brief", b) for b in briefs]):
        if not path.is_file():
            continue
        for line, block in _blocks(path, by_heading=label != "TODO.md"):
            hit = any(r.search(block) for r in refs) or (len(title) >= 6 and title in block.lower())
            if not hit:
                continue
            where = f"{path.name}:{line}"
            if label == "brief":
                return "done", where
            if label == "OPEN.md":
                if _open_block_ruled(block):
                    return "done", where
                best = best or ("queued", f"{where} (open decision, not ruled yet)")
            elif _todo_row_closed(block):
                return "done", where
            else:
                best = ("queued", where) if not best or best[0] != "done" else best
    return best or ("MISSING", "no trace in TODO / OPEN / brief")


def crosscheck(registers: Path, blitz: int, briefs: list[Path]) -> tuple[list[tuple[Question, str, str]], list[Question]]:
    """Rows (question, verdict, where) for every answered Q of `blitz`, plus its unanswered questions."""
    active = numbered(registers, ACTIVE_RE).get(blitz)
    if active is None:
        raise FileNotFoundError(f"no BLITZ{blitz}_ACTIVE.md in {registers}")
    questions = parse_questions(active, blitz)
    rows = [(q, *trace(q, registers / "TODO.md", registers / "OPEN.md", briefs)) for q in questions if q.answer]
    return rows, [q for q in questions if not q.answer]


def render_table(rows: list[tuple[Question, str, str]], unanswered: list[Question]) -> list[str]:
    lines = []
    for question, verdict, where in rows:
        excerpt = " ".join(question.answer.split())
        excerpt = excerpt[:60] + ("…" if len(excerpt) > 60 else "")
        mark = {"done": "✅ done", "queued": "🟡 queued", "MISSING": "🔴 MISSING"}[verdict]
        title = question.title.replace("|", "\\|")
        lines.append(f"| BLITZ{question.blitz} Q{question.number} · {title} | {excerpt.replace('|', chr(92) + '|')} | "
                     f"{mark} — {where} |")
    for question in unanswered:
        lines.append(f"| BLITZ{question.blitz} Q{question.number} · {question.title.replace('|', chr(92) + '|')} | "
                     f"_(not answered)_ | ⏳ unanswered — carry into this READY file |")
    return lines


def write_crosscheck(ready: Path, lines: list[str]) -> bool:
    """Replace the body of the «Сверка прошлых блицев» table in a READY file; returns whether it was found."""
    text = ready.read_text(encoding="utf-8").replace("\r\n", "\n")
    heading = CROSSCHECK_HEADING_RE.search(text)
    if not heading:
        return False
    before, after = text[:heading.end()], text[heading.end():].split("\n")
    # after[0] is the rest of the heading line; the table is the first run of lines starting with "|".
    index = 1
    while index < len(after) and not after[index].lstrip().startswith("|"):
        index += 1
    start = index
    while index < len(after) and after[index].lstrip().startswith("|"):
        index += 1
    table = after[start:index]
    header = table[:2] if len(table) >= 2 else ["| blitz · question | answer | where it is now |", "|---|---|---|"]
    rebuilt = after[:start] + header + lines + after[index:]
    ready.write_text(before + "\n".join(rebuilt), encoding="utf-8", newline="\n")
    return True


# ------------------------------------------------------------------ commands
def brief_pages() -> list[Path]:
    return [paths.owner_page("brief", lang) for lang in paths.PAGE_NAMES]


def next_number(registers: Path) -> int:
    found = set(numbered(registers, READY_RE)) | set(numbered(registers, ACTIVE_RE))
    return max(found, default=0) + 1


def command_new(args: argparse.Namespace) -> int:
    registers = Path(args.registers)
    template = Path(args.template) if args.template else registers / "BLITZ_READY_TEMPLATE.md"
    if not template.is_file():
        print(f"no template at {template}", file=sys.stderr)
        return 2
    number = args.number or next_number(registers)
    target = registers / f"BLITZ{number}_READY.md"
    if target.exists():
        print(f"{target.name} already exists; not overwriting", file=sys.stderr)
        return 1
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = template.read_text(encoding="utf-8").replace("\r\n", "\n").replace("<N>", str(number)).replace("<date>", today)
    registers.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"created {target.name}")
    previous = max((n for n in numbered(registers, ACTIVE_RE) if n < number), default=None)
    if previous is None:
        print("no earlier BLITZ*_ACTIVE.md: nothing to cross-check")
        return 0
    return _crosscheck_into(registers, previous, target, brief_pages())


def _crosscheck_into(registers: Path, blitz: int, ready: Path, briefs: list[Path]) -> int:
    rows, unanswered = crosscheck(registers, blitz, briefs)
    lines = render_table(rows, unanswered)
    wrote = write_crosscheck(ready, lines)
    missing = [q for q, verdict, _w in rows if verdict == "MISSING"]
    print(f"cross-check of BLITZ{blitz}: {sum(v == 'done' for _q, v, _w in rows)} done, "
          f"{sum(v == 'queued' for _q, v, _w in rows)} queued, {len(missing)} MISSING, {len(unanswered)} unanswered"
          + (f" -> table written into {ready.name}" if wrote else f" (no «Сверка» heading in {ready.name})"))
    for question in missing:
        print(f"  MISSING: BLITZ{blitz} Q{question.number} · {question.title} — route it (TODO / OPEN / brief) before the next blitz")
    return 1 if missing else 0


def command_crosscheck(args: argparse.Namespace) -> int:
    registers = Path(args.registers)
    ready = numbered(registers, READY_RE)
    active = numbered(registers, ACTIVE_RE)
    blitz = args.blitz
    if blitz is None:
        newest = max(ready, default=None)
        earlier = [n for n in active if newest is None or n < newest]
        if not earlier:
            earlier = list(active)
        blitz = max(earlier, default=None)
    if blitz is None or blitz not in active:
        print("nothing to cross-check: no BLITZ*_ACTIVE.md")
        return 0
    rows, unanswered = crosscheck(registers, blitz, brief_pages())
    for line in render_table(rows, unanswered):
        print(line)
    missing = [q for q, verdict, _w in rows if verdict == "MISSING"]
    print(f"\nBLITZ{blitz}: {len(rows)} answered, {len(missing)} MISSING, {len(unanswered)} unanswered")
    if args.write:
        target = ready.get(max(ready)) if ready else None
        if target is None or max(ready) <= blitz:
            print("no newer READY file to write into (run `blitz.py new`)", file=sys.stderr)
        else:
            write_crosscheck(target, render_table(rows, unanswered))
            print(f"table written into {target.name}")
    return 1 if missing else 0


def command_status(args: argparse.Namespace) -> int:
    registers = Path(args.registers)
    ready, active = numbered(registers, READY_RE), numbered(registers, ACTIVE_RE)
    if not ready and not active:
        print("no blitz files yet (`python tools/blitz.py new` opens BLITZ1_READY.md)")
        return 0
    for number in sorted(ready):
        asked = parse_questions(ready[number], number)
        answered = {q.number for q in parse_questions(active[number], number) if q.answer} if number in active else set()
        open_count = sum(1 for q in asked if q.number not in answered)
        print(f"BLITZ{number}_READY: {open_count} open question(s) of {len(asked)}")
    live = [n for n, path in sorted(active.items()) if not is_closed(path)]
    print("active blitz: " + (", ".join(f"BLITZ{n}" for n in live) if live else "none"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registers", default=str(REGISTERS), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create the next BLITZ<N>_READY.md and its cross-check table")
    new.add_argument("--template", default=None, help="template file (default: BLITZ_READY_TEMPLATE.md beside the READY files)")
    new.add_argument("--number", type=int, help="override the next number")
    new.set_defaults(func=command_new)
    cross = sub.add_parser("crosscheck", help="where did each answer of the previous blitz go?")
    cross.add_argument("--blitz", type=int, help="blitz number (default: the latest before the newest READY)")
    cross.add_argument("--write", action="store_true", help="also write the table into the newest READY file")
    cross.set_defaults(func=command_crosscheck)
    sub.add_parser("status", help="open questions and the active blitz").set_defaults(func=command_status)
    args = parser.parse_args(argv)
    paths.console_safe()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""Tiered context index for `work/` — read an abstract, then a section, then the text.

Why this exists
---------------
`work/` grows to hundreds of markdown files. Every agent that needs one paragraph out of it pays
for whole files, and a vector database buys fuzzy recall at the cost of a service, an embedding
model, and a retrieval path nobody can audit. The standing rule is *verify, do not infer*, and an
opaque similarity score is the opposite of that.

So this borrows the one good idea from tiered-loading systems (a ~100-token abstract, a ~1k
overview, the full text) and drops everything else. Nothing is installed, no service runs, no
model is called.

Three levels, each cheap to reach:

    L0  one line per file  — H1 title + the document's own verdict/lead sentence
    L1  one file           — its section map: every heading, its line range, its first sentence
    L2  the text itself    — read ONLY the line range L1 named

Every level is EXTRACTED, never summarised: an abstract is a sentence the author already wrote,
quoted verbatim with its line number, so the index can be wrong about *relevance* but never about
*content*.

Usage
-----
    python tools/context_index.py build                 # rebuild .runtime/_context_index.json
    python tools/context_index.py find <words...>       # L0 rows, ranked (start here, always)
    python tools/context_index.py overview <path>       # L1 section map for one file
    python tools/context_index.py section <path> <n>    # the exact read command for a section
    python tools/context_index.py stats                 # what the index costs and what it saves
    python tools/context_index.py fresh                 # is the index stale? (exit 1 if it is)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import paths

ROOT = paths.PROJECT
INDEX_PATH = paths.CONTEXT_INDEX

# What gets indexed. Paths are relative to the project root.
SCAN_TARGETS: tuple[str, ...] = ("work",)
SCAN_FILES: tuple[str, ...] = ("AGENTS.md", "README.md")
# Never index a worktree copy or generated output.
EXCLUDED_PARTS: frozenset[str] = frozenset({".claude", "node_modules", "__pycache__", ".git", ".runtime"})
# Indexed, but ranked below live documents and hidden from `find` unless --all is passed.
COLD_PARTS: frozenset[str] = frozenset({"archive", "backups"})

VERDICT_MARKERS: tuple[str, ...] = (
    "verdict first", "verdict:", "вердикт", "the finding", "tl;dr", "итог", "коротко",
    "recommendation", "рекомендация",
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9_.:/-]{2,}")
DATE_RE = re.compile(r"(20\d\d)[-_](\d\d)[-_](\d\d)")
TOKENS_PER_CHAR = 0.25


@dataclass
class Section:
    level: int
    title: str
    line: int
    end_line: int
    lead: str = ""
    tokens: int = 0

    @property
    def lines(self) -> int:
        return max(0, self.end_line - self.line + 1)


@dataclass
class Document:
    path: str
    title: str
    abstract: str
    abstract_line: int
    bytes: int
    sha256: str
    lines: int
    tokens: int
    modified: str
    cold: bool
    dates: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        row = asdict(self)
        row["sections"] = [asdict(section) for section in self.sections]
        return row


def _clean(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"^\s*>+\s*", "", text)
    text = text.replace("**", "")
    return " ".join(text.split())


def _first_sentence(text: str, limit: int = 240) -> str:
    text = _clean(text)
    if not text:
        return ""
    parts = SENTENCE_END_RE.split(text)
    sentence = parts[0] if parts else text
    if len(sentence) > limit:
        clipped = sentence[: limit - 1]
        if " " in clipped[limit // 2:]:
            clipped = clipped.rsplit(" ", 1)[0]
        sentence = clipped.rstrip(" ,;:-") + "…"
    return sentence


def _is_prose(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "|", "```", "---", "<!--")):
        return False
    if stripped.startswith(">"):
        return bool(stripped.lstrip("> ").strip())
    return True


def _pick_abstract(lines: list[str], body_start: int) -> tuple[str, int]:
    candidates: list[tuple[int, str]] = []
    for offset in range(body_start, min(len(lines), body_start + 120)):
        line = lines[offset]
        if not _is_prose(line):
            continue
        candidates.append((offset + 1, line))
        if len(candidates) >= 40:
            break
    for number, line in candidates:
        lowered = line.lower()
        if any(marker in lowered for marker in VERDICT_MARKERS):
            return _first_sentence(line), number
    for number, line in candidates:
        return _first_sentence(line), number
    return "", 0


def _sections(lines: list[str]) -> list[Section]:
    found: list[Section] = []
    fenced = False
    for offset, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING_RE.match(line)
        if not match or len(match.group(1)) not in (2, 3):
            continue
        found.append(Section(level=len(match.group(1)), title=_clean(match.group(2)),
                             line=offset + 1, end_line=len(lines)))
    for index, section in enumerate(found):
        if index + 1 < len(found):
            section.end_line = max(section.line, found[index + 1].line - 1)
        body = lines[section.line: section.end_line]
        section.tokens = int(sum(len(line) + 1 for line in body) * TOKENS_PER_CHAR)
        for offset in range(section.line, min(section.end_line, section.line + 30)):
            if offset < len(lines) and _is_prose(lines[offset]):
                section.lead = _first_sentence(lines[offset], limit=160)
                break
    return found


def _read_document(path: Path) -> Document | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.split("\n")
    title = ""
    body_start = 0
    for offset, line in enumerate(lines[:40]):
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            title = _clean(match.group(2))
            body_start = offset + 1
            break
    if not title:
        title = path.stem.replace("_", " ")
    abstract, abstract_line = _pick_abstract(lines, body_start)
    relative = path.relative_to(ROOT).as_posix()
    stat = path.stat()
    return Document(
        path=relative, title=title, abstract=abstract, abstract_line=abstract_line,
        bytes=stat.st_size, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), lines=len(lines), tokens=int(len(text) * TOKENS_PER_CHAR),
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d"),
        cold=bool(COLD_PARTS.intersection(Path(relative).parts)),
        dates=sorted({"-".join(match) for match in DATE_RE.findall(relative + " " + text[:4000])}),
        sections=_sections(lines),
    )


def _markdown_files() -> Iterable[Path]:
    seen: set[Path] = set()
    for target in SCAN_TARGETS:
        base = ROOT / target
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            if EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts):
                continue
            if path not in seen:
                seen.add(path)
                yield path
    for name in SCAN_FILES:
        path = ROOT / name
        if path.is_file() and path not in seen:
            seen.add(path)
            yield path


def build_index() -> dict[str, Any]:
    documents = [document for path in _markdown_files() if (document := _read_document(path))]
    documents.sort(key=lambda document: document.path)
    payload = {
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "root": ROOT.as_posix(),
        "tokensPerChar": TOKENS_PER_CHAR,
        "documentCount": len(documents),
        "totalTokens": sum(document.tokens for document in documents),
        "documents": [document.to_json() for document in documents],
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def load_index() -> dict[str, Any]:
    if not INDEX_PATH.is_file():
        raise SystemExit("no index yet — run: python tools/context_index.py build")
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _terms(text: str) -> list[str]:
    return [word.lower() for word in WORD_RE.findall(text)]


def _score(document: dict[str, Any], terms: list[str]) -> float:
    path = document["path"].lower()
    title = document["title"].lower()
    abstract = document["abstract"].lower()
    headings = " ".join(section["title"] for section in document["sections"]).lower()
    leads = " ".join(section["lead"] for section in document["sections"]).lower()
    score = 0.0
    for term in terms:
        if term in path:
            score += 4.0
        if term in title:
            score += 3.0
        if term in abstract:
            score += 2.0
        if term in headings:
            score += 1.5
        if term in leads:
            score += 0.75
    if score and document["cold"]:
        score *= 0.35
    return score


def command_build(_args: argparse.Namespace) -> int:
    payload = build_index()
    index_tokens = int(INDEX_PATH.stat().st_size * TOKENS_PER_CHAR)
    print(
        f"indexed {payload['documentCount']} documents · "
        f"{payload['totalTokens']:,} tokens of source · index is {index_tokens:,} tokens "
        f"({INDEX_PATH.relative_to(ROOT).as_posix()})")
    return 0


def _best_sections(document: dict[str, Any], terms: list[str], limit: int = 2) -> list[dict[str, Any]]:
    ranked = []
    for section in document["sections"]:
        haystack = (section["title"] + " " + section["lead"]).lower()
        hits = sum(1 for term in terms if term in haystack)
        if hits:
            ranked.append((hits, section))
    ranked.sort(key=lambda row: (-row[0], row[1]["line"]))
    return [section for _hits, section in ranked[:limit]]


def command_find(args: argparse.Namespace) -> int:
    index = load_index()
    terms = _terms(" ".join(args.query))
    if not terms:
        raise SystemExit("give me something to look for")
    scored = [
        (score, document)
        for document in index["documents"]
        if (score := _score(document, terms)) > 0 and (args.all or not document["cold"])
    ]
    scored.sort(key=lambda row: (-row[0], row[1]["path"]))
    hits = scored[: args.limit]
    if args.json:
        print(json.dumps([document for _score_value, document in hits], ensure_ascii=False, indent=1))
        return 0
    if not hits:
        print("nothing matched. Try fewer words, or --all to include archive/backups.")
        return 1
    print(f"{len(hits)} of {len(scored)} matches · L0 rows — open none of these blind\n")
    for score, document in hits:
        marker = " [cold]" if document["cold"] else ""
        print(f"{score:5.1f}  {document['path']}{marker}")
        print(f"       {document['title']} · {document['modified']} · ~{document['tokens']:,} tokens")
        if document["abstract"]:
            print(f"       “{document['abstract']}” (:{document['abstract_line']})")
        for section in _best_sections(document, terms):
            print(f"       §{section['line']}-{section['end_line']}  {section['title']}")
        print()
    print("next: python tools/context_index.py overview <path>")
    return 0


def _document_by_path(index: dict[str, Any], wanted: str) -> dict[str, Any]:
    wanted_norm = wanted.replace("\\", "/").lstrip("./").lower()
    exact = [row for row in index["documents"] if row["path"].lower() == wanted_norm]
    if exact:
        return exact[0]
    partial = [row for row in index["documents"] if wanted_norm in row["path"].lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SystemExit(f"not in the index: {wanted}")
    listing = "\n  ".join(row["path"] for row in partial[:10])
    raise SystemExit(f"{len(partial)} files match {wanted}:\n  {listing}")


def command_overview(args: argparse.Namespace) -> int:
    index = load_index()
    document = _document_by_path(index, args.path)
    print(f"{document['path']} · {document['lines']} lines · ~{document['tokens']:,} tokens")
    print(f"{document['title']} · last touched {document['modified']}")
    if document["abstract"]:
        print(f"“{document['abstract']}” (:{document['abstract_line']})")
    print()
    if not document["sections"]:
        print("no H2/H3 sections — this file is read whole or not at all")
        return 0
    for number, section in enumerate(document["sections"], start=1):
        indent = "  " * (section["level"] - 2)
        print(f"[{number:2}] {indent}{section['title']}  ·  "
              f"lines {section['line']}-{section['end_line']} (~{section['tokens']:,} tokens)")
        if section["lead"]:
            print(f"     {indent}{section['lead']}")
    print(f"\nnext: python tools/context_index.py section {document['path']} <number>")
    return 0


def command_section(args: argparse.Namespace) -> int:
    index = load_index()
    document = _document_by_path(index, args.path)
    sections = document["sections"]
    if not 1 <= args.number <= len(sections):
        raise SystemExit(f"section number must be 1..{len(sections)}")
    section = sections[args.number - 1]
    start, end = section["line"], section["end_line"]
    print(f"{document['path']} §{args.number}: {section['title']}  (lines {start}-{end})")
    print(f"  sed -n {start},{end}p {document['path']}")
    print(f"  Read(file_path=\"{(ROOT / document['path']).as_posix()}\", offset={start}, limit={end - start + 1})")
    if args.show:
        text = (ROOT / document["path"]).read_text(encoding="utf-8", errors="replace").split("\n")
        print()
        print("\n".join(text[start - 1: end]))
    return 0


def command_stats(_args: argparse.Namespace) -> int:
    index = load_index()
    documents = index["documents"]
    hot = [row for row in documents if not row["cold"]]
    index_tokens = int(INDEX_PATH.stat().st_size * TOKENS_PER_CHAR)
    l0_tokens = sum(int((len(row["path"]) + len(row["title"]) + len(row["abstract"])) * TOKENS_PER_CHAR)
                    for row in hot)
    print(f"documents      {len(documents):,}  ({len(hot):,} live, {len(documents) - len(hot):,} cold)")
    print(f"L2 (all text)  {index['totalTokens']:,} tokens")
    print(f"L1 (index)     {index_tokens:,} tokens on disk")
    print(f"L0 (live rows) {l0_tokens:,} tokens — what a full `find` listing would cost at most")
    if index["totalTokens"]:
        print(f"ratio          reading one L0 row costs {index['totalTokens'] / max(1, l0_tokens):.0f}× less than the tree")
    biggest = sorted(documents, key=lambda row: -row["tokens"])[:5]
    print("\nheaviest files — these are the ones never to open whole:")
    for row in biggest:
        print(f"  ~{row['tokens']:>8,} tokens  {row['path']}")
    return 0


def _changed(path: Path, row: dict[str, Any]) -> bool:
    """Whether the file is not what the index recorded. Compares CONTENT, not size: a same-length
    edit (a reversed conclusion, a changed digit) must not leave stale search evidence behind. An
    index row from before hashes existed is treated as changed, so one rebuild upgrades it."""
    recorded = row.get("sha256")
    if not recorded:
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return hashlib.sha256(text.encode("utf-8")).hexdigest() != recorded


def command_fresh(_args: argparse.Namespace) -> int:
    """Answer one question honestly: would a rebuild change what `find` returns?"""
    index = load_index()
    known = {row["path"]: row for row in index["documents"]}
    stale: list[str] = []
    added: list[str] = []
    for path in _markdown_files():
        relative = path.relative_to(ROOT).as_posix()
        row = known.pop(relative, None)
        if row is None:
            added.append(relative)
        elif _changed(path, row):
            stale.append(relative)
    removed = list(known)
    if not (stale or added or removed):
        print(f"index is current · {index['documentCount']} documents · built {index['generatedAt']}")
        return 0
    print(f"index is STALE · {len(added)} new, {len(stale)} changed, {len(removed)} gone")
    for label, rows in (("new", added), ("changed", stale), ("gone", removed)):
        for relative in rows[:5]:
            print(f"  {label:>7}: {relative}")
        if len(rows) > 5:
            print(f"  {'':>7}  … and {len(rows) - 5} more")
    print("run: python tools/context_index.py build")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="rebuild the index from the markdown tree").set_defaults(handler=command_build)

    find = sub.add_parser("find", help="ranked L0 rows — always the first step")
    find.add_argument("query", nargs="+")
    find.add_argument("--limit", type=int, default=8)
    find.add_argument("--all", action="store_true", help="include archive/ and backups/")
    find.add_argument("--json", action="store_true")
    find.set_defaults(handler=command_find)

    overview = sub.add_parser("overview", help="L1 section map for one file")
    overview.add_argument("path")
    overview.set_defaults(handler=command_overview)

    section = sub.add_parser("section", help="the exact line range to read for one section")
    section.add_argument("path")
    section.add_argument("number", type=int)
    section.add_argument("--show", action="store_true", help="print the section instead of the command")
    section.set_defaults(handler=command_section)

    sub.add_parser("stats", help="what the index costs and what it saves").set_defaults(handler=command_stats)
    sub.add_parser("fresh", help="is the index stale? exit 1 if it is").set_defaults(handler=command_fresh)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())

"""Ruling Gate: verify whether an owner ruling already exists before asking the owner.

A generic port of the pack's ruling gate, with the project-specific sources removed. It reads
the durable decision registers this skeleton defines:

  1. ``work/agents/registers/OPEN.md``      — the open-decisions register (index rows + sections)
  2. ``work/agents/registers/RULINGS.md``   — the law/ruling register (optional)
  3. ``work/agents/registers/TODO.md``      — closed tasks that carry a verdict

Fail-closed contract:
  - "NOT_FOUND" represents permission to ask the owner.
  - If any source is unreadable or fails to parse, the status is ERROR / UNCHECKED.
  - An unreadable source NEVER produces "NOT_FOUND". Asking the owner is BLOCKED (fail-closed).

Exit codes for the CLI: 0 FOUND · 1 NOT_FOUND · 2 ERROR/UNCHECKED.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import owner_text
import paths

DEFAULT_REPO = paths.PROJECT


@dataclass
class RulingMatch:
    source: str
    file_path: str
    line: int
    date: str
    answer: str
    quote: str
    confidence: str = "DIRECT"


@dataclass
class GateResult:
    query_type: str
    query: str
    status: str  # "FOUND", "NOT_FOUND", "ERROR"
    permission_to_ask: bool
    matches: List[RulingMatch]
    unreadable_sources: List[Tuple[str, str]]
    checked_sources: List[str]
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_type": self.query_type,
            "query": self.query,
            "status": self.status,
            "permission_to_ask": self.permission_to_ask,
            "matches": [asdict(m) for m in self.matches],
            "unreadable_sources": [{"source": s, "error": e} for s, e in self.unreadable_sources],
            "checked_sources": self.checked_sources,
            "summary": self.summary,
        }


def extract_date(text: str, fallback_date: str = "unknown") -> str:
    """Extract standard ISO date (YYYY-MM-DD) or Russian date (DD.MM)."""
    m_iso = re.search(r"\b(20\d\d-[01]\d-[0-3]\d)\b", text)
    if m_iso:
        return m_iso.group(1)
    m_ru = re.search(r"\b([0-3]\d)\.([01]\d)\b", text)
    if m_ru:
        return f"{datetime.now().year}-{m_ru.group(2)}-{m_ru.group(1)}"
    return fallback_date


def normalize_sec(sec: str) -> str:
    """Normalize §1.488 -> 1.488."""
    s = sec.strip()
    if s.startswith("§"):
        s = s[1:]
    return s


def make_sec_regex(clean_sec: str) -> re.Pattern:
    """Match a section number with a boundary, avoiding §1.1 matching §1.11."""
    escaped = re.escape(clean_sec)
    return re.compile(r"(?:§|(?<![\w.])\b)" + escaped + r"(?!\w|\.\d)")


_RULING_MARKERS = (
    "ЗАКРЫТО", "РЕШЕНО", "ОТВЕЧЕНО", "ОТМЕНЁН", "ИСПОЛНЕНО",
    "ПРИМЕНЕНО", "CLOSED", "RULED", "DECIDED", "ANSWERED",
)
_RULING_MARKER_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(marker) for marker in _RULING_MARKERS) + r")(?!\w)",
    re.IGNORECASE,
)
_NEGATED_MARKER_PREFIX_RE = re.compile(r"(?:^|[^\w])не\s*[-:–—]?\s*$", re.IGNORECASE)
_LEADING_MARKER_RE = re.compile(
    r"^[\W\s]*(?:" + "|".join(re.escape(marker) for marker in _RULING_MARKERS) + r")(?!\w)",
    re.IGNORECASE,
)
_LEADING_NEGATED_MARKER_RE = re.compile(
    r"^[\W\s]*не\s*[-:–—]?\s*(?:" + "|".join(re.escape(marker) for marker in _RULING_MARKERS) + r")(?!\w)",
    re.IGNORECASE,
)

_CLOSED_STATE_ICON = "✅"
_OPEN_STATE_ICONS = ("🔴", "🟠", "🟡")
_STATE_ICON_RE = re.compile(
    "|".join(re.escape(icon) for icon in (_CLOSED_STATE_ICON,) + _OPEN_STATE_ICONS)
)
_INDEX_ROW_SECTION_RE = re.compile(r"§?(\d+(?:\.\d+)?[a-z]?)(?!\w|\.\d)")
# The shipped OPEN.md template indexes with list lines:  - §12 · ✅ ruled · short title
_LIST_INDEX_RE = re.compile(r"^[-*]\s*(§\d+(?:\.\d+)?[a-z]?)\s*[·|—–-]\s*(.*)$")
# Ruling-body labels, Russian and English; the English ones are the template's own.
_RULING_LABELS = ("**Рулинг владельца:**", "**Ответ владельца:**", "**Ruling:**", "**Owner ruling:**",
                  "**Owner answer:**")
_BODY_PREFIXES = ("Рулинг владельца:", "Ответ владельца:", "Ответ:", "Ruling:", "Owner answer:", "Answer:")
_ANSWER_LABELS = ("**Answer:**", "**Ответ:**", "**Owner answer:**", "**Ответ владельца:**")


def label_content(line: str, labels: Tuple[str, ...]) -> Optional[str]:
    """Text after the first label found in `line`, or None. Empty text or the template's own
    placeholder («<his words verbatim>») is no ruling at all."""
    for label in labels:
        if label in line:
            content = line.split(label, 1)[1].strip().strip("-").strip()
            if not content or content.startswith(("«<", "<", "…")):
                return None
            return content
    return None


_ANSWER_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?(?:Answer|Ответ(?: владельца)?)(?:\s*\([^)]*\))?\s*:?(?:\*\*)?\s*:?\s*(.*)$", re.IGNORECASE)


def _answer_text(line: str) -> Optional[str]:
    """The owner's answer on a blitz answer line - `Answer (<UTC>): «...»` as the blitz brief writes
    it, or `**Answer:** ...` - or None for no answer / an empty or placeholder one."""
    match = _ANSWER_LINE_RE.match(line)
    if not match or not re.match(r"^\s*(?:\*\*)?(?:Answer|Ответ)", line, re.IGNORECASE):
        return None
    content = match.group(1).strip().strip("-").strip()
    if not content or content.startswith(("«<", "<", "…")):
        return None
    return content


def index_parts(stripped: str, in_table: bool, before_sections: bool) -> Optional[List[str]]:
    """One OPEN.md index row as [_, status, row id, text], from a table row or a `- §N · ...` line."""
    if in_table and stripped.startswith("|"):
        parts = [p.strip() for p in stripped.split("|")]
        return parts if len(parts) >= 4 else None
    if before_sections:
        match = _LIST_INDEX_RE.match(stripped)
        if match:
            rest = match.group(2)
            return ["", first_state_icon(rest) or "", match.group(1), rest]
    return None


def ruling_marker_state(text: str) -> Optional[bool]:
    match = _RULING_MARKER_RE.search(text)
    if match is None:
        return None
    if _NEGATED_MARKER_PREFIX_RE.search(text[:match.start()]):
        return False
    return True


def has_positive_ruling_marker(text: str) -> bool:
    return ruling_marker_state(text) is True


def leading_ruling_marker_state(text: str) -> Optional[bool]:
    if _LEADING_NEGATED_MARKER_RE.search(text):
        return False
    if _LEADING_MARKER_RE.search(text):
        return True
    return None


def has_any_negated_ruling_marker(text: str) -> bool:
    return any(
        _NEGATED_MARKER_PREFIX_RE.search(text[:match.start()])
        for match in _RULING_MARKER_RE.finditer(text)
    )


def first_state_icon(text: str) -> Optional[str]:
    match = _STATE_ICON_RE.search(text)
    return match.group(0) if match else None


def canonical_index_states(lines: List[str]) -> Dict[str, Set[str]]:
    """Map each section in the live OPEN.md index to its row states."""
    states: Dict[str, Set[str]] = {}
    in_index_table = False
    before_sections = True
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## §"):
            before_sections = False
        if stripped.startswith("|") and ("row | what it is" in stripped or "строка | что это" in stripped):
            in_index_table = True
            continue
        if in_index_table and (stripped.startswith("#") or stripped == ""):
            in_index_table = False
        parts = index_parts(stripped, in_index_table, before_sections)
        if parts is None:
            continue
        m = _INDEX_ROW_SECTION_RE.search(parts[2])
        if not m:
            continue
        status_icon = parts[1]
        row_text = parts[3]
        is_ruled = "✅" in status_icon or leading_ruling_marker_state(row_text) is True
        states.setdefault(m.group(1), set()).add("ruled" if is_ruled else "open")
    return states


_TEXT_SECTION_MARKED_RE = re.compile(r"§(?P<section>\d+(?:\.\d+)?(?:[a-z](?![A-Za-z]))?)")
_TEXT_SECTION_BARE_RE = re.compile(r"(?<![\w.])(?P<section>\d+\.\d{3,}[a-z]?)(?![\w.])")
_TEXT_ROW_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<row>[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+){2,}[-_]20"
    r"(?:\d{6}|\d{2}_\d{2}_\d{2}))(?![A-Z0-9_])"
)


def extract_text_references(text: str) -> List[Tuple[str, str]]:
    """Extract durable section/row identifiers from owner prose."""
    refs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for match in _TEXT_SECTION_MARKED_RE.finditer(text):
        ref = ("section", normalize_sec(match.group("section")))
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    for match in _TEXT_SECTION_BARE_RE.finditer(text):
        ref = ("section", normalize_sec(match.group("section")))
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    for match in _TEXT_ROW_ID_RE.finditer(text):
        ref = ("row", match.group("row"))
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def text_reference_patterns(text: Optional[str]) -> Tuple[re.Pattern, ...]:
    patterns: List[re.Pattern] = []
    for kind, token in extract_text_references(text or ""):
        if kind == "section":
            patterns.append(make_sec_regex(token))
        else:
            patterns.append(re.compile(
                r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", re.IGNORECASE))
    return tuple(patterns)


def text_references_match(text: Optional[str], candidate: str) -> bool:
    return any(pattern.search(candidate) for pattern in text_reference_patterns(text))


def text_references_match_kind(text: Optional[str], candidate: str, kind: str) -> bool:
    for ref_kind, token in extract_text_references(text or ""):
        if ref_kind != kind:
            continue
        if kind == "section":
            if make_sec_regex(token).search(candidate):
                return True
        elif re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])",
                       candidate, re.IGNORECASE):
            return True
    return False


def check_open_md(
    open_file: Path, clean_sec: Optional[str], text_query: Optional[str]
) -> Tuple[List[RulingMatch], Set[str]]:
    matches: List[RulingMatch] = []
    found_refs: Set[str] = set()
    sec_pat = make_sec_regex(clean_sec) if clean_sec else None
    content = open_file.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    in_index_table = False
    before_sections = True
    current_sec_heading = ""
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("## §"):
            before_sections = False
        if stripped.startswith("|") and ("row | what it is" in stripped or "строка | что это" in stripped):
            in_index_table = True
            continue
        if in_index_table and (stripped.startswith("#") or stripped == ""):
            in_index_table = False

        parts = index_parts(stripped, in_index_table, before_sections)
        if parts is not None:
            if len(parts) >= 4:
                status_icon, row_id, row_text = parts[1], parts[2], parts[3]
                hit = False
                is_exact_row = False
                if sec_pat:
                    if sec_pat.search(row_id):
                        hit = True
                        is_exact_row = True
                    elif sec_pat.search(row_text):
                        hit = True
                elif text_references_match(text_query, row_text) or text_references_match(text_query, row_id):
                    hit = True
                    is_exact_row = (
                        text_references_match(text_query, row_id)
                        or text_references_match_kind(text_query, row_text, "row")
                    )
                if hit:
                    is_ruled = "✅" in status_icon or leading_ruling_marker_state(row_text) is True
                    if is_ruled:
                        if is_exact_row:
                            for r in re.findall(r"§[0-9.]*[0-9]", row_text):
                                if not clean_sec or r != f"§{clean_sec}":
                                    found_refs.add(r)
                        matches.append(RulingMatch(
                            source="OPEN.md (index table)", file_path=str(open_file), line=idx,
                            date=extract_date(row_text), answer=row_text[:250], quote=stripped,
                            confidence="DIRECT" if is_exact_row else "MENTION",
                        ))

        if stripped.startswith("##"):
            current_sec_heading = stripped
            hit = bool(sec_pat and sec_pat.search(stripped)) or text_references_match(text_query, stripped)
            if hit and ("✅" in stripped or has_positive_ruling_marker(stripped)):
                d = extract_date(stripped)
                ruling_body = ""
                for next_line in lines[idx:min(idx + 10, len(lines))]:
                    if any(k in next_line for k in _BODY_PREFIXES):
                        ruling_body = next_line.strip()
                        break
                matches.append(RulingMatch(
                    source="OPEN.md (section heading)", file_path=str(open_file), line=idx, date=d,
                    answer=ruling_body or stripped, quote=f"{stripped}\n{ruling_body}".strip(),
                    confidence="DIRECT",
                ))

        if not in_index_table and label_content(stripped, _RULING_LABELS) is not None:
            hit = bool(sec_pat and (sec_pat.search(current_sec_heading) or sec_pat.search(stripped))) \
                or text_references_match(text_query, stripped) \
                or text_references_match(text_query, current_sec_heading)
            if hit:
                matches.append(RulingMatch(
                    source="OPEN.md (ruling body)", file_path=str(open_file), line=idx,
                    date=extract_date(f"{current_sec_heading} {stripped}"), answer=stripped,
                    quote=f"{current_sec_heading}\n{stripped}", confidence="DIRECT",
                ))
    return matches, found_refs


def check_blitz(
    blitz_file: Path, clean_sec: Optional[str], text_query: Optional[str]
) -> Tuple[List[RulingMatch], Set[str]]:
    """Answers the owner has already submitted in a blitz file (`**Answer:** ...` under a question)."""
    matches: List[RulingMatch] = []
    sec_pat = make_sec_regex(clean_sec) if clean_sec else None
    lines = blitz_file.read_text(encoding="utf-8", errors="replace").splitlines()
    heading = ""
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped
            continue
        answer = _answer_text(stripped)
        if answer is None:
            continue
        block = f"{heading} {stripped}"
        if (sec_pat and sec_pat.search(block)) or text_references_match(text_query, block):
            matches.append(RulingMatch(
                source=f"{blitz_file.name} (submitted answer)", file_path=str(blitz_file), line=idx,
                date=extract_date(block), answer=answer[:250], quote=f"{heading}\n{stripped}".strip(),
                confidence="DIRECT",
            ))
    return matches, set()


def open_canonical_sections(open_file: Path) -> Set[str]:
    content = open_file.read_text(encoding="utf-8", errors="replace")
    states = canonical_index_states(content.splitlines())
    return {section for section, row_states in states.items() if "open" in row_states}


def check_todo(
    todo_file: Path, clean_sec: Optional[str], text_query: Optional[str]
) -> Tuple[List[RulingMatch], Set[str]]:
    matches: List[RulingMatch] = []
    found_refs: Set[str] = set()
    sec_pat = make_sec_regex(clean_sec) if clean_sec else None
    lines = todo_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        m_header = re.search(r"-\s*(?:\[[^\]]*\]\s*)?(?:[^\w*]+\s*)?\*\*([^*]+)\*\*", stripped)
        hit = bool(sec_pat and sec_pat.search(stripped)) or text_references_match(text_query, stripped)
        if not hit:
            continue
        header_is_ruled = False
        if m_header:
            header_text = stripped[:m_header.end()]
            if first_state_icon(stripped[:m_header.start(1)]) in _OPEN_STATE_ICONS:
                header_is_ruled = False
            else:
                header_is_ruled = "✅" in header_text or has_positive_ruling_marker(header_text)
        if header_is_ruled:
            if m_header and sec_pat and sec_pat.search(m_header.group(1)):
                for r in re.findall(r"§[0-9.]*[0-9]", stripped[:400]):
                    found_refs.add(r)
            matches.append(RulingMatch(
                source="TODO.md (closed task)", file_path=str(todo_file), line=idx,
                date=extract_date(stripped), answer=stripped[:250], quote=stripped,
                confidence="DIRECT",
            ))
    return matches, found_refs


def check_rulings_register(
    rulings_file: Path, clean_sec: Optional[str], text_query: Optional[str]
) -> Tuple[List[RulingMatch], Set[str]]:
    matches: List[RulingMatch] = []
    sec_pat = make_sec_regex(clean_sec) if clean_sec else None
    lines = rulings_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        hit = bool(sec_pat and sec_pat.search(stripped)) or text_references_match(text_query, stripped)
        if hit and ("|" in stripped or "##" in stripped):
            matches.append(RulingMatch(
                source="RULINGS.md", file_path=str(rulings_file), line=idx,
                date=extract_date(stripped), answer=stripped[:250], quote=stripped,
                confidence="DIRECT",
            ))
    return matches, set()


def check_ruling(
    section: Optional[str] = None,
    text: Optional[str] = None,
    repo_path: Optional[Path] = None,
    sources_override: Optional[Dict[str, Path]] = None,
) -> GateResult:
    """Core gate check: returns GateResult with fail-closed semantics."""
    if not section and not text:
        raise ValueError("Must provide either section or text query")
    repo = repo_path or DEFAULT_REPO
    query_type = "section" if section else "text"
    query_val = section if section else (text or "")
    clean_sec = normalize_sec(section) if section else None

    default_sources = {
        "OPEN.md": repo / "work/agents/registers/OPEN.md",
        "TODO.md": repo / "work/agents/registers/TODO.md",
        "RULINGS.md": repo / "work/agents/registers/RULINGS.md",
    }
    registers = repo / "work/agents/registers"
    if registers.is_dir():
        for blitz in sorted(registers.glob("BLITZ*_ACTIVE.md")):
            default_sources[f"BLITZ:{blitz.name}"] = blitz
    if sources_override:
        default_sources.update(sources_override)

    checked_sources: List[str] = []
    unreadable_sources: List[Tuple[str, str]] = []
    matches: List[RulingMatch] = []

    for s_name, s_path in default_sources.items():
        if not s_path.exists():
            # An optional register (RULINGS.md/TODO.md) may legitimately be absent; OPEN.md is the
            # load-bearing one. Only OPEN.md's absence is an unreadable source.
            if s_name == "OPEN.md":
                unreadable_sources.append((str(s_path), f"File not found: {s_path}"))
            continue
        try:
            if s_name == "OPEN.md":
                m, _refs = check_open_md(s_path, clean_sec, text)
            elif s_name == "TODO.md":
                m, _refs = check_todo(s_path, clean_sec, text)
            elif s_name.startswith("BLITZ:"):
                m, _refs = check_blitz(s_path, clean_sec, text)
            else:
                m, _refs = check_rulings_register(s_path, clean_sec, text)
            checked_sources.append(s_name)
            matches.extend(m)
        except Exception as ex:  # noqa: BLE001 - any failure is fail-closed evidence
            unreadable_sources.append((str(s_path), f"{type(ex).__name__}: {ex}"))

    open_p = default_sources.get("OPEN.md")
    open_canonical: Set[str] = set()
    if open_p and open_p.exists():
        try:
            open_canonical = open_canonical_sections(open_p)
        except Exception:  # noqa: BLE001
            open_canonical = set()

    # Supersession: a section currently open in the canonical index cannot be closed by older prose.
    unique: List[RulingMatch] = []
    seen_locs: Set[Tuple[str, int]] = set()
    for m in matches:
        loc = (m.file_path, m.line)
        if loc in seen_locs:
            continue
        seen_locs.add(loc)
        conf = m.confidence
        if conf == "DIRECT" and not m.source.startswith("OPEN.md") and \
                _INDEX_ROW_SECTION_RE.sub(r"\1", clean_sec or "") in open_canonical:
            conf = "MENTION"
        unique.append(RulingMatch(m.source, m.file_path, m.line, m.date, m.answer, m.quote, conf))

    conf_order = {"DIRECT": 0, "CROSS_REF": 1, "MENTION": 2}
    unique.sort(key=lambda m: (conf_order.get(m.confidence, 3), m.line))
    authoritative = [m for m in unique if m.confidence == "DIRECT"]

    if authoritative:
        status, permission = "FOUND", False
        summary = owner_text.text("gate.found", count=len(authoritative), query=query_val)
    elif unreadable_sources:
        status, permission = "ERROR", False
        summary = owner_text.text("gate.error", count=len(unreadable_sources))
    else:
        status, permission = "NOT_FOUND", True
        summary = owner_text.text("gate.not_found", query=query_val, count=len(checked_sources))

    return GateResult(
        query_type=query_type, query=query_val, status=status, permission_to_ask=permission,
        matches=unique, unreadable_sources=unreadable_sources, checked_sources=checked_sources,
        summary=summary,
    )


def format_report(result: GateResult) -> str:
    lines = [
        "=" * 70,
        f"RULING GATE: {result.status} (permission_to_ask={result.permission_to_ask})",
        f"Query: [{result.query_type}] '{result.query}'",
        "=" * 70,
        result.summary,
        "",
    ]
    t = owner_text.text
    if result.unreadable_sources:
        lines.append(t("gate.unreadable"))
        for src, err in result.unreadable_sources:
            lines.append(f"  ❌ {src}: {err}")
        lines.append("")
    if result.matches:
        lines.append(t("gate.matches", count=len(result.matches)))
        for idx, m in enumerate(result.matches, 1):
            lines.append(t("gate.ruling", n=idx, confidence=m.confidence))
            lines.append(t("gate.date", value=m.date))
            lines.append(t("gate.source", value=f"{m.source} ({m.file_path}:{m.line})"))
            lines.append(t("gate.answer", value=m.answer))
            lines.append(t("gate.quote", value=m.quote))
            lines.append("")
    lines.append(t("gate.checked", count=len(result.checked_sources), value=", ".join(result.checked_sources)))
    return "\n".join(lines)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ruling Gate: verify if an owner ruling already exists before asking.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--section", type=str, help="Section number, e.g. §1.488, 1.488")
    group.add_argument("--text", type=str, help="Text query, e.g. 'SOME-ROW-ID-20260907'")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="Path to repository root")
    parser.add_argument("--json", action="store_true", help="Output JSON structure")
    parser.add_argument("--quiet", action="store_true", help="Quiet output: only the summary line")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    paths.console_safe()
    args = parse_args(argv)
    res = check_ruling(section=args.section, text=args.text, repo_path=args.repo)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
    elif args.quiet:
        print(f"[{res.status}] permission_to_ask={res.permission_to_ask}: {res.summary}")
    else:
        print(format_report(res))
    if res.status == "FOUND":
        return 0
    if res.status == "NOT_FOUND":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

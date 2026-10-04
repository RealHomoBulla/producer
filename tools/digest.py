"""What the agents produced that the owner has not been told about yet.

The failure this closes is structural, not a lapse of memory. A Producer holds a dozen live
Workers, a blitz queue and its own edits; "report the finished ones" is precisely the item
that loses to whatever arrived most recently. Nothing else in the pipeline watches for
*products the owner has not heard about*: the registers track where content went and the
unanswered page tracks questions he owes us, and neither tracks what we owe him.

So this tracks exactly one fact per file: **has he been told about this yet.** It does not
summarise — a generated summary of a report is a third copy of the truth. It lists what is
unreported and how big it is; reading it and telling him what it means stays the Producer's job.

Product filter: files under ``work/agents/**.md``, excluding registers, state, knowledge
living pages and Producer-written briefs. The set of watched folders is derived from the live
tree, never hand-kept (a hand-kept list goes quietly wrong and the failure mode is silence).

⚠️ Marking something reported is a claim that it was **actually said to him in chat**, with the
verdict in it — not that the file was opened.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import owner_text
import paths
import state_io

PROJECT = paths.PROJECT
STATE = paths.DIGEST_STATE
# The owner-facing page. He reads this one, says "прочитал дайджест", and `clear` files it away.
PAGE = paths.DIGEST_PAGE


def archive_dir():
    """Where `clear` files the read digest: `[paths] archive_dir` (default `.runtime/archive`) / digests."""
    return paths.archive_dir() / "digests"


OWNER_TIME, TIMEZONE_WARNING = paths.owner_tzinfo()

# Section headers in either owner language, so a page survives a language switch.
_HEADER_WORDS = "|".join(re.escape(table["digest.added"]) for table in owner_text.TEXT.values())
APPEND_HEADER_RE = re.compile(rf"^### (?:{_HEADER_WORDS}) (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}})\s*$",
                              re.MULTILINE)
ANY_APPEND_HEADER_RE = re.compile(rf"^### (?:{_HEADER_WORDS})\b.*$", re.MULTILINE)

# Where Workers put products. The tree is watched; the few non-products are named.
AGENTS = "work/agents"

# Folders under `agents/` that are NOT owner-facing products. Everything else is watched.
NOT_A_PRODUCT = frozenset({
    "registers",   # TODO/OPEN/REPORTS/POLISHING — where products are ROUTED, not products
    "state",       # HANDOVER, machine journals — internal continuity
    "archive",     # retired material
    "_harvest",    # mechanical harvest output
})

# Filename shapes under watched folders that are inputs or internal reference, never products:
# Producer-written briefs, commit-review verdicts, living knowledge pages.
NON_PRODUCT_NAME = re.compile(r"^(CR-.*|.*_REVIEW\.md)$")
NON_PRODUCT_DIRS = ("orca/briefs",)
LIVING_PAGE_RE = re.compile(r"^20\d\d_\d\d_\d\d_")


def is_product(path: str) -> bool:
    """Project-relative .md path → False for briefs/verdicts/living pages/ops docs."""
    parts = path.split("/")
    if len(parts) < 4 or parts[0] != "work" or parts[1] != "agents":
        return True  # not an agents product path; leave watched
    domain = parts[2]
    if len(parts) == 4 and parts[3] == "README.md":
        return False  # a folder's own instructions page (e.g. reports/README.md), never a product
    if domain == "orca" and ("/".join(parts[2:4]) in NON_PRODUCT_DIRS
                             or (len(parts) == 4 and not LIVING_PAGE_RE.match(parts[3]))):
        return False  # ops inputs; dated orca reports stay listed
    if "reviews" in parts and NON_PRODUCT_NAME.match(parts[-1]):
        return False  # commit-review briefs + verdicts, wherever they live
    if domain == "knowledge" and not LIVING_PAGE_RE.match(parts[-1]):
        return False  # living domain pages; dated reports stay listed
    return True


def watched_folders() -> list[str]:
    """Every product folder under `agents/`, read from the live tree (derived, never hand-kept)."""
    base = PROJECT / AGENTS
    if not base.is_dir():
        return []
    return sorted(
        f"{AGENTS}/{child.name}"
        for child in base.iterdir()
        if child.is_dir() and child.name not in NOT_A_PRODUCT and not child.name.startswith(".")
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _owner_datetime(utc_value: datetime) -> datetime:
    """Convert an aware UTC value to the configured project timezone (UTC if it is unusable)."""
    return utc_value.astimezone(OWNER_TIME)


def _owner_now() -> datetime:
    return _owner_datetime(datetime.now(timezone.utc))


def _next_append_stamp() -> str:
    """Local minute, never earlier than an existing generated page header."""
    stamp = _owner_now().replace(second=0, microsecond=0, tzinfo=None)
    if PAGE.exists():
        for match in APPEND_HEADER_RE.finditer(PAGE.read_text(encoding="utf-8")):
            existing = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
            stamp = max(stamp, existing)
    return stamp.strftime("%Y-%m-%d %H:%M")


def load() -> dict:
    """The digest ledger. A corrupt file raises StateCorrupt - it is never silently reset."""
    state = state_io.load_json(STATE, {"reported": {}})
    state.setdefault("reported", {})
    return state


def save(state: dict) -> None:
    state_io.save_json(STATE, state)


def products(since_days: int = 0) -> list[dict]:
    """Every Worker product, newest first. `since_days` > 0 narrows the view; 0 (the default) shows
    everything, so an old product nobody reported can never silently drop out of sight."""
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400 if since_days > 0 else None
    out = []
    for folder in watched_folders():
        base = PROJECT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            rel = path.relative_to(PROJECT).as_posix()
            if not is_product(rel):
                continue
            stat = path.stat()
            if cutoff is not None and stat.st_mtime < cutoff:
                continue
            out.append({
                "path": rel,
                "mtime": stat.st_mtime,
                "lines": sum(1 for _ in path.open(encoding="utf-8", errors="replace")),
            })
    return sorted(out, key=lambda r: -r["mtime"])


def command_list(args: argparse.Namespace) -> int:
    state = load()
    rows = products(args.days)
    unreported = [r for r in rows
                  if state["reported"].get(r["path"], {}).get("mtime") != r["mtime"]]

    if args.quiet:
        sys.stdout.write(f"digest: {len(unreported)} unreported product(s)\n"
                         if unreported else "digest: nothing unreported\n")
        return 0

    if not unreported:
        sys.stdout.write("nothing the owner has not been told about\n")
        return 0

    sys.stdout.write(f"{len(unreported)} product(s) he has NOT been told about:\n\n")
    for row in unreported:
        seen = state["reported"].get(row["path"])
        mark = "UPDATED" if seen else "NEW"
        stamp = _owner_datetime(
            datetime.fromtimestamp(row["mtime"], timezone.utc)
        ).strftime("%Y-%m-%d %H:%M")
        sys.stdout.write(f"  {mark:8} {stamp}  {row['lines']:>5} lines  {row['path']}\n")
    sys.stdout.write("\nRead each one and tell him the VERDICT, not that the file exists.\n")
    return 0


def command_mark(args: argparse.Namespace) -> int:
    """Record that these were actually reported to him, in chat, with their conclusions."""
    state = load()
    rows = {r["path"]: r for r in products(args.days)}
    targets = list(rows) if args.all else list(args.path or [])
    if not targets:
        sys.stdout.write("nothing to mark (pass --path ... or --all)\n")
        return 1

    # A product counts as told about if the digest page names it, OR if `append --covers` said so.
    # The second route exists because the first contradicted his own length rule (no paths in the
    # owner page). `covered` is the service ledger — it never touches the page he reads.
    page_text = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""
    covered = state.get("covered", {})
    invalid = [path for path in targets if path not in rows]
    unbacked = [path for path in targets
                if path in rows and path not in page_text and path not in covered]
    if invalid or unbacked:
        for path in invalid:
            sys.stdout.write(f"  not a current product: {path}\n")
        for path in unbacked:
            sys.stdout.write(f"  REFUSED, no digest section covers product: {path}\n")
        sys.stdout.write("marked 0 as reported; name each product in `append --covers ...` first\n")
        return 1

    for path in targets:
        row = rows[path]
        state["reported"][path] = {"mtime": row["mtime"], "at": now(), "lines": row["lines"]}
    save(state)
    sys.stdout.write(f"marked {len(targets)} as reported\n")
    return 0


# The owner digest is written in the owner's language and must stay short: 1-3 plain sentences.
DIGEST_SECTION_MAX_CHARS = 900
_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def _owner_text_problem(text: str) -> str | None:
    """Return why `text` is not an owner-readable digest section, or None if it is.

    Length applies to every language. The script check follows `owner_language`: a Russian owner
    gets Cyrillic prose, an English owner is not asked for Cyrillic.
    """
    body = text.strip()
    if len(body) > DIGEST_SECTION_MAX_CHARS:
        return owner_text.text("digest.too_long", size=len(body), limit=DIGEST_SECTION_MAX_CHARS)
    latin, cyrillic = len(_LATIN_RE.findall(body)), len(_CYRILLIC_RE.findall(body))
    language = paths.owner_language()
    if language == "ru" and latin > cyrillic:
        return owner_text.text("digest.wrong_language", "ru")
    if language == "en" and cyrillic > latin:
        return owner_text.text("digest.wrong_language", "en")
    return None


def command_append(args: argparse.Namespace) -> int:
    """Add to the digest. Never replace it. While unread there is no rewrite path at all."""
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        sys.stdout.write("nothing to append\n")
        return 1
    if ANY_APPEND_HEADER_RE.search(text):
        sys.stdout.write("refusing text with its own '### Added' / '### Добавлено' header; "
                         "append creates one\n")
        return 1
    problem = _owner_text_problem(text)
    if problem:
        sys.stdout.write(f"refusing: {problem}\n")
        return 1
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    fresh = not PAGE.exists()
    stamp = _next_append_stamp()
    # `newline="\n"` is explicit so this tool never writes CRLF while other writers write LF.
    with PAGE.open("a", encoding="utf-8", newline="\n") as fh:
        if fresh:
            fh.write(f"{owner_text.text('digest.title')}\n\n{owner_text.text('digest.intro')}\n\n"
                     f"{owner_text.text('digest.warning')}\n")
        fh.write(f"\n---\n\n### {owner_text.text('digest.added')} {stamp}\n\n")
        fh.write(text.rstrip() + "\n")
    covers = [c for c in (args.covers or [])]
    if covers:
        state = load()
        ledger = state.setdefault("covered", {})
        current = {r["path"]: r for r in products(args.days)}
        unknown = [c for c in covers if c not in current]
        for c in covers:
            # `mtime` pins the revision the section reported on: a product edited afterwards is a
            # different revision and stays unreported when the page is cleared.
            ledger[c] = {"at": now(), "section": stamp,
                         "mtime": current[c]["mtime"] if c in current else None}
        save(state)
        sys.stdout.write(f"  covers {len(covers)} product(s) in the service ledger"
                         f" (not written to his page)\n")
        for c in unknown:
            sys.stdout.write(f"  note: not a current product, recorded anyway: {c}\n")
    sys.stdout.write(f"appended to {PAGE.relative_to(PROJECT)}"
                     f" ({'created' if fresh else 'existing'})\n")
    return 0


def command_clear(args: argparse.Namespace) -> int:
    """He has read it: archive the page, then mark as reported exactly what the page covered."""
    if not PAGE.exists():
        sys.stdout.write("no digest to clear\n")
        return 0
    archive = archive_dir()
    text = PAGE.read_text(encoding="utf-8")
    stamp = now().replace(":", "").replace("-", "")[:15]
    target = archive / f"{stamp}_DIGEST.md"
    try:
        archive.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if target.read_text(encoding="utf-8") != text:
            raise OSError("archived copy differs from the page")
    except OSError as exc:
        # The unread page stays where it is until the archive copy is verified.
        print(f"ERROR: could not archive digest at {target}: {exc}; the page was NOT cleared",
              file=sys.stderr)
        return 1
    # The page's own mtime IS the moment of the last `append`, so capture it before unlinking.
    last_append = PAGE.stat().st_mtime
    PAGE.unlink()
    state = load()
    covered = state.get("covered", {})
    marked = kept = uncovered = 0
    for row in products(args.days):
        path = row["path"]
        entry = covered.get(path)
        if not entry and path not in text:
            uncovered += 1  # no section named it: nobody told him, so it stays on the list
            continue
        pinned = entry.get("mtime") if entry else None
        if (pinned is not None and pinned != row["mtime"]) or row["mtime"] > last_append:
            kept += 1  # edited after it was reported on: a new revision he has not heard about
            continue
        previous = state["reported"].get(path, {})
        state["reported"][path] = {
            "mtime": row["mtime"],
            "at": previous["at"] if "at" in previous else now(),
            "lines": row["lines"],
        }
        marked += 1
    # The page is gone, so its coverage ledger has nothing left to back.
    state.pop("covered", None)
    save(state)
    left = kept + uncovered
    note = f"; {left} product(s) the page did not cover or that changed since stay UNREPORTED" if left else ""
    sys.stdout.write(f"archived to {target}; {marked} marked reported{note}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=0,
                        help="only products changed in the last N days (default 0 = all)")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="what he has not been told about")
    listing.add_argument("--days", type=int, default=argparse.SUPPRESS,
                         help="only products changed in the last N days (0 = all)")
    listing.add_argument("--quiet", action="store_true", help="one line, for a dashboard")
    listing.set_defaults(func=command_list)

    mark = sub.add_parser("mark", help="record that these were reported to him IN CHAT, with verdicts")
    mark.add_argument("--days", type=int, default=argparse.SUPPRESS,
                      help="only products changed in the last N days (0 = all)")
    mark.add_argument("--path", action="append")
    mark.add_argument("--all", action="store_true")
    mark.set_defaults(func=command_mark)

    append = sub.add_parser("append", help="ADD to the digest; while unread there is no rewrite path")
    append.add_argument("--days", type=int, default=argparse.SUPPRESS,
                        help="only products changed in the last N days (0 = all)")
    append.add_argument("--text", help="the section to add; omit to read it from stdin")
    append.add_argument("--covers", action="append", metavar="PATH",
                        help="a product this section reports on. Recorded in the service ledger, "
                             "NOT written to his page. Repeatable.")
    append.set_defaults(func=command_append)

    clear = sub.add_parser("clear", help="he said he read it: archive the page, mark all reported")
    clear.add_argument("--days", type=int, default=argparse.SUPPRESS,
                       help="only products changed in the last N days (0 = all)")
    clear.set_defaults(func=command_clear)

    args = parser.parse_args(argv)
    paths.console_safe()
    if TIMEZONE_WARNING:
        print(f"warning: {TIMEZONE_WARNING}", file=sys.stderr)
    try:
        return args.func(args)
    except state_io.StateCorrupt as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

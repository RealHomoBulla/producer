---
name: work-recall
description: Find prior decisions, reports, runbooks, and project knowledge without opening large files blindly. Use whenever a question may already be answered in the work/ tree or when a search could lead into long documents.
---

# Work recall — abstract, then section, then text

The project's Markdown tree may contain large journals and registers. Opening a large file whole to answer one question burns context and hides the relevant evidence. `tools/context_index.py` provides three tiers; each is extracted from source text rather than generated as a summary, so the index can mis-rank relevance but cannot invent the quoted content.

| tier | contents | typical cost |
|---|---|---|
| **L0** | One line per document: title and its own lead or verdict | Low |
| **L1** | One document's headings, line spans, and opening sentence per section | Moderate |
| **L2** | The exact text at the relevant line span | Only what you need |

## Use this order

```bash
# Run from the project root.
python tools/context_index.py find <topic>
python tools/context_index.py find <topic> --limit 12 --all

# Pick a document and inspect its section map.
python tools/context_index.py overview work/agents/reports/<topic>/<date>_<seat>_<subject>.md

# Read only the relevant section.
python tools/context_index.py section <path> <section-number>
python tools/context_index.py section <path> <section-number> --show
```

1. Start with `find`; use a second or third phrasing when a miss may be vocabulary mismatch.
2. Use `overview` on one promising document, then `section` for the exact text. Do not skip directly to a large file.
3. Run `python tools/context_index.py fresh` before trusting a search result if the Markdown tree changed during the session. Exit status 1 means rebuild with `python tools/context_index.py build`.
4. Use `stats` to see the index size and its current coverage. Treat the index as a disposable derived cache, never hand-edit it or load its generated data file into context.

## Evidence rules

- A quoted abstract proves what a document says, not that it is current or true. Verify decisions and live facts against their source of truth.
- A miss is useful evidence. If a few distinct searches return nothing, say the answer may not be recorded and write it down when you establish it.
- Give new reports a conclusion-first lead sentence so the index can find them.
- For code and configuration searches, use targeted search tools directly; this index covers Markdown only.

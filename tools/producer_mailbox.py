"""The ONE definition of where the Producer's mailbox files live: the drainer's actionable log,
its error log and its single-instance lock, read by mailbox_cursor.py and appended to by a
drainer.

The directory is **per project**: `<repo>/.runtime/mailbox/` (gitignored). Two projects on one
machine therefore never share a log, a lock or a cursor — a second project's drainer starts, and
each Producer reads only its own messages. Set `PRODUCER_MAILBOX_DIR` to put the directory
somewhere else (for example a tmpfs); the override is still per-process, so give each project its
own value.

Every reader and writer imports this module; a second literal of the directory anywhere is the bug.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import paths

ENV_DIR = "PRODUCER_MAILBOX_DIR"
ACTIONABLE_NAME = "actionable.jsonl"
ERR_NAME = "drain_err.txt"
LOCK_NAME = "drain_mailbox.lock"


def mailbox_dir(environ: Mapping[str, str] | None = None) -> Path:
    """This project's mailbox directory: `$PRODUCER_MAILBOX_DIR`, else `<repo>/.runtime/mailbox`."""
    env = os.environ if environ is None else environ
    override = env.get(ENV_DIR)
    if override:
        return Path(os.path.expanduser(override))
    return paths.RUNTIME / "mailbox"


def mailbox_file(name: str, environ: Mapping[str, str] | None = None) -> Path:
    return mailbox_dir(environ) / name


def actionable_log() -> Path:
    return mailbox_file(ACTIONABLE_NAME)


def ensure_mailbox_dir() -> Path:
    """Create the directory if it is missing and return it."""
    directory = mailbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory

"""Durable JSON state for the tools: atomic writes, a single-writer lock, and corruption that is loud.

Every tool that keeps a state file (digest, unanswered, commit review) goes through here, so a
crash mid-write never leaves half a file, two writers never interleave a read-modify-write, and a
file that is not valid JSON is a visible error — never silently replaced by an empty register
(which is how a question list or a "reported" ledger would quietly vanish).
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

_LOCK_OFFSET = 0x7FFFFFF0  # Windows byte locks are mandatory: lock one byte far past any data


class StateCorrupt(RuntimeError):
    """A state file exists but is not the JSON object the tool wrote."""


def load_json(path: Path, default: dict) -> dict:
    """The JSON object in `path`; `default` (a fresh copy) when the file does not exist.

    Raises StateCorrupt when the file exists but cannot be read as a JSON object. The caller
    reports it and stops: the file is left untouched so it can be recovered by hand.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return json.loads(json.dumps(default))
    except OSError as exc:
        raise StateCorrupt(f"{path}: unreadable ({exc})") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise StateCorrupt(
            f"{path} is not valid JSON ({exc}). It was NOT reset; fix or restore it "
            f"(for example from git history) before running this tool again.") from exc
    if not isinstance(value, dict):
        raise StateCorrupt(f"{path} holds {type(value).__name__}, expected a JSON object")
    return value


def save_json(path: Path, data: dict) -> None:
    """Write `data` atomically: temp file in the same folder, fsync, then rename over the target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def locked(path: Path, timeout: float = 30.0):
    """Hold the single-writer lock for `path` (a `<path>.lock` file) around a read-modify-write."""
    lock_path = Path(path).with_name(Path(path).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if WINDOWS:
                    os.lseek(handle.fileno(), _LOCK_OFFSET, os.SEEK_SET)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"another process holds {lock_path} for over {timeout:.0f}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            if WINDOWS:
                os.lseek(handle.fileno(), _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()

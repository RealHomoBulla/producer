"""Resolve the Orca IDE CLI consistently in interactive shells, hooks and services."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


class OrcaCliError(RuntimeError):
    pass


def is_gnome_screen_reader(candidate: str) -> bool:
    try:
        with Path(candidate).open("rb") as stream:
            head = stream.read(4096).decode("utf-8", "replace")
    except OSError:
        return False
    return head.startswith("#!") and any(
        marker in head for marker in ("orca.orca", "import orca", "Orca Team", "brlapi")
    )


def cli_candidates() -> list[str]:
    home = Path.home()
    if os.name == "nt":
        # The Windows installer puts the CLI beside the app and does NOT add it to PATH until the
        # app's own "install command" is run.
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        # orca.exe first: orca.cmd refuses to forward orchestration message bodies safely.
        bin_dir = local / "Programs" / "orca" / "resources" / "bin"
        return [str(bin_dir / "orca.exe"), str(bin_dir / "orca.cmd")]
    return [
        str(home / ".cache/orca/appimage/launcher/orca-ide"),
        str(home / ".config/orca/linux-orca-cli-shim/orca"),
    ]


def prefer_native(found: str, windows: bool | None = None) -> str:
    """On Windows the Orca `.cmd` shim cannot forward orchestration message bodies safely; when the
    native `orca.exe` sits beside it, use that instead (same build, so same protocol)."""
    if (os.name == "nt") if windows is None else windows:
        path = Path(found)
        if path.suffix.lower() in (".cmd", ".bat") and path.with_suffix(".exe").is_file():
            return str(path.with_suffix(".exe"))
    return found


def resolve_cli() -> str:
    """The Orca IDE CLI to run. An explicit `ORCA_CLI_COMMAND` is a pin: if it is wrong or
    missing this refuses, it never falls back to another binary (a silent substitute could be a
    different install or the GNOME screen reader)."""
    override = os.environ.get("ORCA_CLI_COMMAND")
    if override:
        # The override is one executable, never shell text or extra arguments.
        found = shutil.which(os.path.expanduser(override))
        if not found:
            if len(override.split()) > 1 and shutil.which(os.path.expanduser(override.split()[0])):
                raise OrcaCliError(
                    f"ORCA_CLI_COMMAND must be one executable, not a command line with arguments: "
                    f"{override!r}")
            raise OrcaCliError(
                f"ORCA_CLI_COMMAND executable is unavailable: {override}. Fix or unset it; "
                "the pin is not silently replaced by another Orca binary")
        if is_gnome_screen_reader(found):
            raise OrcaCliError(
                f"{found} is the GNOME Orca screen reader, not the Orca IDE CLI. "
                "Set ORCA_CLI_COMMAND to the IDE launcher.")
        return prefer_native(found)
    refused = None
    for candidate in [*cli_candidates(), shutil.which("orca-ide"), shutil.which("orca")]:
        if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
            continue
        if is_gnome_screen_reader(candidate):
            refused = candidate
            continue
        return prefer_native(candidate)
    if refused:
        raise OrcaCliError(
            f"{refused} is the GNOME Orca screen reader, not the Orca IDE CLI. "
            "Set ORCA_CLI_COMMAND to the IDE launcher."
        )
    raise OrcaCliError("Orca IDE CLI is unavailable; open or reinstall the Orca desktop app")


def run_json(*args: str, timeout: float = 90) -> dict:
    # Preserve Orca's intentional long poll, but bound a hung CLI beyond that deadline.
    if "--timeout-ms" in args:
        timeout = max(timeout, int(args[args.index("--timeout-ms") + 1]) / 1000 + 30)
    try:
        done = subprocess.run(
            [resolve_cli(), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrcaCliError(f"Orca {' '.join(args[:2])} failed: {exc}") from exc
    if done.returncode:
        raise OrcaCliError(f"Orca {' '.join(args[:2])}: {done.stderr.strip()[:400]}")
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise OrcaCliError("Orca CLI returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OrcaCliError("Orca CLI returned a non-object response")
    if payload.get("ok") is False:
        raise OrcaCliError(f"Orca CLI rejected the request: {payload.get('error')}")
    return payload

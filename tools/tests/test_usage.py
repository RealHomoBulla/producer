"""usage.py: parse the Orca meter, probe each route, and never leak a key. Faked subprocess/HTTP."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess

import usage


# ---------------------------------------------------------------- fakes
def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> CompletedProcess:
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def runner_for(handler):
    """A subprocess.run stand-in: records argv, returns ``handler(argv)``."""
    calls: list[list[str]] = []

    def _run(args, **kwargs):
        calls.append(list(args))
        return handler(list(args))

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def http_for(handler):
    calls: list[tuple] = []

    def _http(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        return handler(method, url, headers, body)

    _http.calls = calls  # type: ignore[attr-defined]
    return _http


ORCA_PAYLOAD = {
    "result": {
        "rateLimits": {
            "claude": {
                "status": "ok",
                "session": {"usedPercent": 12, "resetDescription": "22:50"},
                "weekly": {"usedPercent": 95, "resetDescription": "Mon 15:00"},
            },
            "codex": {
                "status": "ok",
                "session": {"usedPercent": 6, "resetsAt": 1_791_148_003_000},
                "weekly": {"usedPercent": 100, "resetDescription": "Sat 00:24"},
            },
            "gemini": {"status": "unavailable", "error": "OAuth disabled"},
        }
    }
}


def orca_runner(payload: dict = ORCA_PAYLOAD):
    return runner_for(lambda argv: completed(json.dumps(payload)))


# ---------------------------------------------------------------- keys
def test_load_keys_parses_comments_export_and_quotes(tmp_path: Path):
    keys_file = tmp_path / "keys.env"
    keys_file.write_text(
        "# a comment\n"
        "OPENROUTER_API_KEY=\"sk-or-secret\"\n"
        "export OPENCODE_API_KEY='go-secret'\n"
        "NOT_A_KEY=ignored\n",
        encoding="utf-8",
    )
    keys = usage.load_keys(keys_file, environ={})
    assert keys["OPENROUTER_API_KEY"] == "sk-or-secret"
    assert keys["OPENCODE_API_KEY"] == "go-secret"
    # unknown names are parsed too but never used by a probe
    assert keys["NOT_A_KEY"] == "ignored"


def test_load_keys_falls_back_to_the_environment(tmp_path: Path):
    keys_file = tmp_path / "keys.env"
    keys_file.write_text("# empty\n", encoding="utf-8")
    keys = usage.load_keys(keys_file, environ={"OPENCODE_API_KEY": "from-env"})
    assert keys["OPENCODE_API_KEY"] == "from-env"


def test_load_keys_missing_file_is_empty_not_an_error(tmp_path: Path):
    assert usage.load_keys(tmp_path / "nope.env", environ={}) == {}


# ---------------------------------------------------------------- orca meter
def test_parse_orca_limits_reads_both_windows():
    rows = {row["route"]: row for row in usage.parse_orca_limits(ORCA_PAYLOAD)}
    assert rows["claude"]["state"] == "ok"
    assert rows["claude"]["windows"]["5h"]["usedPercent"] == 12
    assert rows["claude"]["windows"]["5h"]["reset"] == "22:50"
    assert rows["claude"]["windows"]["week"]["usedPercent"] == 95
    assert rows["codex"]["windows"]["week"]["usedPercent"] == 100


def test_reset_text_prefers_a_built_local_date_over_orcas_cyrillic_day():
    """`пн 15:00` renders as «?? 15:00» on a non-Cyrillic console; build the day ourselves."""
    now = datetime(2026, 10, 4, 12, 0).astimezone()  # Sunday, local
    today = {"resetsAt": datetime(2026, 10, 4, 22, 50).timestamp() * 1000}
    later = {"resetsAt": datetime(2026, 10, 9, 15, 0).timestamp() * 1000}
    fallback = {"resetDescription": "пн 15:00"}
    assert usage._reset_text(today, now=now) == "22:50"
    assert usage._reset_text(later, now=now) == "Fri 09.10 15:00"
    assert usage._reset_text(fallback, now=now) == "пн 15:00"


def test_parse_orca_limits_missing_provider_is_not_configured():
    rows = {row["route"]: row for row in usage.parse_orca_limits({"result": {"rateLimits": {}}})}
    assert rows["claude"]["state"] == "not-configured"
    assert rows["codex"]["state"] == "not-configured"


def test_orca_limits_absent_cli_is_not_configured(monkeypatch):
    monkeypatch.setattr(usage.shutil, "which", lambda name: None)
    rows = usage.orca_limits(runner=runner_for(lambda argv: completed()), exe=None)
    assert all(row["state"] == "not-configured" for row in rows)


def test_orca_limits_parses_real_payload():
    rows = usage.orca_limits(runner=orca_runner(), exe="orca")
    assert [row["route"] for row in rows] == ["claude", "codex"]
    assert rows[0]["windows"]["5h"]["usedPercent"] == 12


# ---------------------------------------------------------------- OpenCode Go probe
def test_opencode_go_probe_200_is_live():
    http = http_for(lambda method, url, headers, body: (200, '{"ok":true}'))
    row = usage.probe_opencode_go({"OPENCODE_API_KEY": "secret"}, http=http)
    assert row["state"] == "ok"
    assert "secret" not in json.dumps(row)


def test_opencode_go_probe_429_names_the_window():
    body = json.dumps({"metadata": {"limitName": "monthly"}})
    http = http_for(lambda method, url, headers, body_: (429, body))
    row = usage.probe_opencode_go({"OPENCODE_API_KEY": "secret"}, http=http)
    assert row["state"] == "limited"
    assert "monthly" in row["detail"]


def test_opencode_go_probe_missing_key_is_not_configured():
    row = usage.probe_opencode_go({}, http=http_for(lambda *a: (500, "")))
    assert row["state"] == "not-configured"


# ---------------------------------------------------------------- OpenRouter probe
def test_openrouter_probe_reports_remaining_credit():
    body = json.dumps({"data": {"limit": 10.0, "usage": 8.01, "limit_remaining": 1.99}})
    http = http_for(lambda method, url, headers, body_: (200, body))
    row = usage.probe_openrouter({"OPENROUTER_API_KEY": "secret"}, http=http)
    assert row["state"] == "ok"
    assert "1.99" in row["detail"] and "10.00" in row["detail"]


def test_openrouter_probe_missing_key_is_not_configured():
    row = usage.probe_openrouter({}, http=http_for(lambda *a: (200, "{}")))
    assert row["state"] == "not-configured"


# ---------------------------------------------------------------- AGY + free
def test_agy_probe_ok():
    runner = runner_for(lambda argv: completed('{"result":"ok"}'))
    row = usage.probe_agy(runner=runner, exe="agy")
    assert row["state"] == "ok"


def test_agy_probe_missing_cli_is_not_configured(monkeypatch):
    monkeypatch.setattr(usage.shutil, "which", lambda name: None)
    row = usage.probe_agy(runner=runner_for(lambda argv: completed()), exe=None)
    assert row["state"] == "not-configured"


def test_free_probe_reports_live_and_limit():
    def handler(argv):
        model = argv[3]
        if "space-bunny" in model:
            return completed("ok")
        if "mimo" in model:
            return completed("usage limit reached", returncode=1)
        return completed("", returncode=1)

    rows = {row["route"]: row for row in usage.probe_free(runner=runner_for(handler), exe="opencode")}
    assert rows["free:opencode/space-bunny-free"]["state"] == "ok"
    assert rows["free:opencode/mimo-v2.6-flash-free"]["state"] == "limited"


# ---------------------------------------------------------------- collect + render + main
def test_collect_marks_unconfigured_and_never_prints_a_key():
    secret = "sk-super-secret-value"
    http = http_for(lambda method, url, headers, body: (200, '{"data":{"limit_remaining":5.25}}'))
    rows = usage.collect(
        {"OPENCODE_API_KEY": secret, "OPENROUTER_API_KEY": secret},
        runner=orca_runner(), http=http,
        cli={"orca": "orca", "opencode": None, "agy": None},
    )
    states = {row["route"]: row["state"] for row in rows}
    assert states["claude"] == "ok" and states["codex"] == "ok"
    assert states["openrouter"] == "ok"
    text = usage.render(rows)
    assert secret not in text
    assert "not configured" in text  # AGY / free lanes


def test_main_json_is_machine_readable(monkeypatch, capsys):
    rows = [usage._row("claude", "Claude", state="ok", windows={"5h": {"usedPercent": 3, "reset": None}})]
    monkeypatch.setattr(usage, "collect", lambda *a, **k: rows)
    assert usage.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["routes"][0]["route"] == "claude"


# ---------------------------------------------------------------- presets.toml (sibling product)
def test_presets_toml_parses_and_every_preset_is_a_guardian_config():
    import tomllib

    import guardian

    path = Path(usage.__file__).resolve().parent / "presets.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    presets = data["preset"]
    assert {p["name"] for p in presets} == {"solo-claude", "claude-max-100", "claude-plus-go", "budget-free"}
    for preset in presets:
        assert preset["who"] and preset["cost"], preset["name"]
        routes = preset["producer_routes"]
        assert routes, f"{preset['name']} has no producer route"
        for row in routes:
            problems: list[str] = []
            assert guardian._parse_route(dict(row), problems) is not None, (preset["name"], problems)
            assert not problems, (preset["name"], problems)
        roster = preset["roster"]
        assert roster, f"{preset['name']} has no roster"
        for seat in roster:
            assert seat["name"] and seat["title_prefix"] and seat["command"], (preset["name"], seat)
            assert int(seat["count"]) >= 1

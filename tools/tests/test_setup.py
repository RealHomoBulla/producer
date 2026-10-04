"""Tests for `tools/setup.py` — the first-run setup wizard.

Detection runs against a fake `which`, so no CLI needs to be installed; the writer is checked
against a real-ish `producer.toml` (Guardian sections included) to prove it replaces only the
sections it owns; and the non-interactive path runs on a throwaway directory with hooks injected.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import setup as wizard  # noqa: E402
from setup import Plan, Seat  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


@pytest.fixture(autouse=True)
def no_presets_file(tmp_path_factory, monkeypatch):
    """By default exercise the built-in presets; tests that want presets.toml use `presets_file`."""
    monkeypatch.setattr(wizard, "PRESETS_FILE", tmp_path_factory.mktemp("nopresets") / "absent.toml")


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


EXISTING_TOML = """\
# producer.toml — settings for the generic Producer toolchain.
# This preamble must survive.

[project]
name = "producer"
timezone = "Europe/Moscow"
owner_language = "ru"

[paths]
# Where the owner's model keys live. OUTSIDE the repository on purpose.
keys_file = "~/.config/producer/keys.env"

[orca]
run = ""

[knowledge]
product_globs = []

[guardian]
producer_command = "OLD COMMAND"
bootstrap_prompt = "old prompt"

# The wanted worker seats
[[roster]]
name = "old"
title_prefix = "OLD"
count = 9
command = "old --cmd"
"""


def fake_which(names):
    present = set(names)
    return lambda name: (f"/usr/bin/{name}" if name in present else None)


def _run(root, argv, *, which, out, **kwargs):
    kwargs.setdefault("gate_installer", lambda _root: (True, "ok"))
    kwargs.setdefault("home", root)  # never touch the real ~/.claude during tests
    return wizard.run_setup(
        root, argv, which=which, environ={}, input_fn=lambda _prompt: "", out=out,
        keys_path=root / "keys.env", git_init=False, **kwargs,
    )


# ------------------------------------------------------------------ detection
def test_detect_marks_found_and_missing_and_required():
    found = wizard.detect_clis(fake_which(["claude", "git"]))
    table = "\n".join(wizard.render_cli_table(found))
    assert found["claude"] == "/usr/bin/claude"
    assert found["orca"] is None
    assert "[x] claude" in table
    assert "[ ] orca (required)" in table
    assert wizard.missing_required(found) == ["orca"]


# ------------------------------------------------------------------ roster proposal
def test_default_roster_claude_only():
    roster = wizard.default_roster({"claude": "/c"}, {})
    assert [(s.name, s.count) for s in roster] == [("sonnet", 1)]
    assert "claude-sonnet-5-5" in roster[0].command
    assert "--dangerously-skip-permissions" not in roster[0].command  # never a default


def test_unattended_adds_the_bypass_flag_to_claude_commands_only():
    roster = wizard.default_roster({"claude": "/c", "opencode": "/o"}, {}, unattended=True)
    by_name = {seat.name: seat.command for seat in roster}
    assert by_name["sonnet"].endswith("--dangerously-skip-permissions")
    assert "--dangerously" not in by_name["space-bunny"]


def test_solo_claude_preset_is_one_sonnet_and_one_haiku_and_nothing_else():
    roster = wizard.solo_claude_roster()
    assert [(s.name, s.count) for s in roster] == [("sonnet", 1), ("haiku", 1)]
    assert all(s.command.startswith("claude ") for s in roster)
    assert "sonnet" in roster[0].command and "haiku" in roster[1].command


@pytest.mark.parametrize("found, expected", [
    ({"claude": "/c", "orca": "/o"}, "solo-claude"),
    ({"claude": "/c", "orca": "/o", "git": "/g"}, "solo-claude"),
    ({"claude": "/c", "codex": "/x"}, "full"),
    ({"claude": "/c", "opencode": "/o"}, "full"),
    ({}, "solo-claude"),
])
def test_auto_preset_is_solo_claude_when_only_claude_is_found(found, expected):
    assert wizard.choose_preset("auto", found) == expected
    assert wizard.choose_preset("full", found) == "full"
    assert wizard.choose_preset("solo-claude", found) == "solo-claude"
    with pytest.raises(ValueError, match="unknown preset"):
        wizard.choose_preset("nope", found)


def test_default_roster_opencode_with_key_is_two_deepseek():
    roster = wizard.default_roster({"claude": "/c", "opencode": "/o"}, {"OPENCODE_API_KEY": "x"})
    names = {s.name: s for s in roster}
    assert names["deepseek"].count == 2
    assert "opencode-go/deepseek-v4.1-flash" in names["deepseek"].command


def test_default_roster_opencode_without_key_is_free_space_bunny():
    roster = wizard.default_roster({"opencode": "/o"}, {})
    assert [s.name for s in roster] == ["space-bunny"]
    assert "space-bunny-free" in roster[0].command


def test_default_roster_codex_is_luna():
    roster = wizard.default_roster({"codex": "/x"}, {})
    assert [s.name for s in roster] == ["luna"]
    assert "gpt-6-luna" in roster[0].command


# ------------------------------------------------------------------ keys file
def test_keys_file_has_names_only_and_is_idempotent(workdir):
    path = workdir / "nested" / "keys.env"
    assert wizard.ensure_keys_file(path) is True
    content = path.read_text(encoding="utf-8")
    assert "# OPENCODE_API_KEY=" in content
    assert "# DEEPSEEK_API_KEY=" in content
    # every key mention is a comment: no live `NAME=value` line exists
    assert not any(line and not line.startswith("#") and "=" in line for line in content.splitlines())
    assert wizard.ensure_keys_file(path) is False


def test_the_keys_file_follows_the_configured_location(workdir):
    custom = workdir / "my" / "keys.env"
    (workdir / "producer.toml").write_text(f'[paths]\nkeys_file = "{custom.as_posix()}"\n', encoding="utf-8")
    assert wizard.keys_path_from_config(workdir) == custom
    out = []
    wizard.run_setup(workdir, ["--non-interactive", "--name", "s", "--yes"],
                     which=fake_which(["claude", "orca"]), environ={}, input_fn=lambda _p: "",
                     out=out.append, git_init=False, gate_installer=lambda _r: (True, "ok"))
    assert custom.exists() and any(str(custom) in line for line in out)


# ------------------------------------------------------------------ .gitignore
def test_gitignore_appends_missing_entries_once(workdir):
    path = workdir / ".gitignore"
    path.write_text(".runtime/\n*.env\n", encoding="utf-8")
    assert wizard.ensure_gitignore(workdir) is True
    content = path.read_text(encoding="utf-8")
    assert "__pycache__/" in content
    assert content.count(".runtime/") == 1
    assert wizard.ensure_gitignore(workdir) is False


# ------------------------------------------------------------------ config writer
def test_write_config_preserves_unmanaged_sections_and_replaces_roster(workdir):
    path = workdir / "producer.toml"
    path.write_text(EXISTING_TOML, encoding="utf-8")
    plan = Plan(name="site", lang="en", timezone="UTC",
                roster=[Seat("sonnet", "Sonnet", 1, "claude --model claude-sonnet-5-5 --effort medium")])
    result = wizard.write_config(path, plan)
    assert 'name = "site"' in result
    assert "This preamble must survive." in result
    assert 'keys_file = "~/.config/producer/keys.env"' in result
    assert "[orca]" in result and 'run = ""' in result
    assert "OLD COMMAND" not in result and "old --cmd" not in result
    assert result.count("[[roster]]") == 1
    assert result.count("wanted worker seats") == 1  # the existing banner is not duplicated
    assert "claude-sonnet-5-5" in result
    assert "producer_command = " in result
    assert "[setup]" in result and 'preset = "full"' in result


def test_write_config_can_leave_the_guardian_sections_alone(workdir):
    path = workdir / "producer.toml"
    path.write_text(EXISTING_TOML, encoding="utf-8")
    plan = Plan(name="site", lang="en", timezone="UTC", apply_guardian=False)
    result = wizard.write_config(path, plan)
    assert "OLD COMMAND" in result and "old --cmd" in result and 'count = 9' in result


def test_write_config_sets_product_globs_when_given(workdir):
    path = workdir / "producer.toml"
    plan = Plan(name="site", lang="ru", timezone="Europe/Moscow",
                product_globs=("config/**", "src/**"))
    result = wizard.write_config(path, plan)
    assert 'product_globs = ["config/**", "src/**"]' in result


# ------------------------------------------------------------------ non-interactive end to end
def test_non_interactive_writes_config_and_prints_next_commands(workdir):
    out = []
    which = fake_which(["claude", "opencode", "codex", "git", "orca"])
    code = _run(workdir, ["--non-interactive", "--name", "site", "--lang", "en", "--yes"],
                which=which, out=out.append)
    assert code == 0
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "site"
    assert data["project"]["owner_language"] == "en"
    assert data["guardian"]["producer_command"] == "claude --model claude-sonnet-5-5 --effort medium"
    assert data["setup"]["preset"] == "full"
    assert [row["name"] for row in data["roster"]] == ["sonnet", "space-bunny", "luna"]
    text = "\n".join(out)
    assert "python tools/guardian.py start" in text
    assert "python tools/guardian.py autonomy on --objective" in text
    assert ".runtime/" in (workdir / ".gitignore").read_text(encoding="utf-8")


def test_commit_hooks_install_in_a_git_repo_and_the_gate_stays_off_without_globs(workdir):
    calls = []

    def installer(root):
        calls.append(root)
        return True, "ok"

    which = fake_which(["claude", "orca"])
    base = ["--non-interactive", "--name", "a", "--lang", "ru", "--yes"]
    out = []
    _run(workdir, base, which=which, out=out.append, gate_installer=installer)
    assert calls == []  # not a git repository yet: nothing to install into
    (workdir / ".git").mkdir()
    _run(workdir, base, which=which, out=out.append, gate_installer=installer)
    assert calls == [workdir]
    assert any("Knowledge gate stays off" in line for line in out)
    _run(workdir, base + ["--product-globs", "config/**"], which=which, out=out.append,
         gate_installer=installer)
    assert any("Knowledge gate is ON" in line for line in out)


def test_the_review_families_follow_what_is_installed(workdir):
    assert wizard.review_families({"claude": "/c"}) == ("claude-opus", "claude-sonnet")
    assert wizard.review_families({"claude": "/c", "codex": "/x"}, "solo-claude") == ("claude-opus", "claude-sonnet")
    full = wizard.review_families({"claude": "/c", "codex": "/x", "opencode": "/o", "agy": "/a"})
    assert full == ("codex-luna", "codex-sol", "gemini", "deepseek", "claude-opus", "claude-sonnet")
    _run(workdir, ["--non-interactive", "--name", "a", "--yes"], which=fake_which(["claude", "orca"]),
         out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert data["review"]["reviewers"] == ["claude-opus", "claude-sonnet"]


def test_missing_required_cli_fails_and_prints_no_launch_instructions(workdir):
    out = []
    code = _run(workdir, ["--non-interactive", "--name", "a", "--lang", "ru", "--yes"],
                which=fake_which([]), out=out.append)
    text = "\n".join(out)
    assert code == 1
    assert "WARNING: required CLI 'orca' is MISSING" in text
    assert "WARNING: required CLI 'claude' is MISSING" in text
    assert "NOT READY" in text and "guardian.py start" not in text
    assert (workdir / "producer.toml").exists()  # the config is still written: re-running is cheap


def test_allow_missing_is_a_deliberate_dry_run(workdir):
    code = _run(workdir, ["--non-interactive", "--name", "a", "--yes", "--allow-missing"],
                which=fake_which([]), out=lambda _l: None)
    assert code == 0


def test_only_claude_and_orca_found_gives_the_solo_claude_preset(workdir):
    code = _run(workdir, ["--non-interactive", "--name", "site", "--lang", "en", "--yes"],
                which=fake_which(["claude", "orca", "git"]), out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert code == 0 and data["setup"]["preset"] == "solo-claude"
    assert [(r["name"], r["count"]) for r in data["roster"]] == [("sonnet", 1), ("haiku", 1)]
    assert "--dangerously-skip-permissions" not in data["guardian"]["producer_command"]


def test_unattended_flag_reaches_producer_and_workers(workdir):
    _run(workdir, ["--non-interactive", "--name", "s", "--yes", "--unattended"],
         which=fake_which(["claude", "orca"]), out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert data["guardian"]["producer_command"].endswith("--dangerously-skip-permissions")
    assert all(r["command"].endswith("--dangerously-skip-permissions") for r in data["roster"])


def test_rerunning_setup_keeps_edited_guardian_commands_unless_reset(workdir):
    which = fake_which(["claude", "orca"])
    argv = ["--non-interactive", "--name", "s", "--yes"]
    _run(workdir, argv, which=which, out=lambda _l: None)
    config = workdir / "producer.toml"
    text = config.read_text(encoding="utf-8")
    text = text.replace('command = "claude --model claude-haiku-4-5-20251001"', 'command = "mine"')
    text = text.replace('producer_command = "claude --model claude-sonnet-5-5 --effort medium"',
                        'producer_command = "MY OWN COMMAND"')
    config.write_text(text, encoding="utf-8")
    _run(workdir, argv, which=which, out=lambda _l: None)
    kept = config.read_text(encoding="utf-8")
    assert "MY OWN COMMAND" in kept and 'command = "mine"' in kept
    _run(workdir, argv + ["--reset-guardian"], which=which, out=lambda _l: None)
    reset = config.read_text(encoding="utf-8")
    assert "MY OWN COMMAND" not in reset and 'command = "mine"' not in reset


def test_probe_only_runs_version_and_reports_without_claiming_login(workdir):
    calls = []

    class Done:
        stdout, stderr, returncode = "claude 9.9.9\n", "", 0

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return Done()

    out = []
    _run(workdir, ["--non-interactive", "--name", "s", "--yes", "--probe"],
         which=fake_which(["claude", "orca"]), out=out.append, probe_runner=runner)
    assert calls and all(cmd[1:] == ["--version"] for cmd in calls)
    assert any("login and model access are NOT checked" in line for line in out)


# ------------------------------------------------------------------ the GitHub template remote
needs_git = pytest.mark.skipif(wizard.shutil.which("git") is None, reason="git not installed")


@needs_git
@pytest.mark.parametrize("url", ["https://github.com/RealHomoBulla/producer",
                                 "https://github.com/RealHomoBulla/producer.git",
                                 "git@github.com:realhomobulla/Producer.git"])
def test_origin_pointing_at_the_template_is_renamed_and_push_disabled(workdir, url):
    wizard._run_git(workdir, "init", "-q")
    wizard._run_git(workdir, "remote", "add", "origin", url)
    lines = wizard.fix_template_remote(workdir, "my-site")
    assert wizard._run_git(workdir, "remote").stdout.split() == ["template"]
    assert "DISABLED" in wizard._run_git(workdir, "remote", "get-url", "--push", "template").stdout
    text = "\n".join(lines)
    assert "gh repo create my-site" in text and "git remote add origin" in text


@needs_git
def test_a_users_own_origin_is_left_alone(workdir):
    wizard._run_git(workdir, "init", "-q")
    wizard._run_git(workdir, "remote", "add", "origin", "https://github.com/someone/their-site.git")
    assert wizard.fix_template_remote(workdir) == []
    assert wizard._run_git(workdir, "remote").stdout.split() == ["origin"]


def test_no_git_directory_means_nothing_to_fix(workdir):
    assert wizard.fix_template_remote(workdir) == []


# ------------------------------------------------------------------ English seed pages
def _seed_tree(root):
    _shipped_seeds_or_skip()
    for ru_rel, _en in wizard.SEED_PAGES:
        source = TOOLS.parent / ru_rel
        target = root / ru_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _shipped_seeds_or_skip():
    missing = [ru for ru, _en in wizard.SEED_PAGES if not (TOOLS.parent / ru).is_file()]
    if missing:
        pytest.skip(f"setup.py --lang en already localised the seed pages here ({missing[0]} is gone)")


def test_every_shipped_seed_page_matches_its_recorded_hash():
    _shipped_seeds_or_skip()
    for ru_rel, _en in wizard.SEED_PAGES:
        text = (TOOLS.parent / ru_rel).read_text(encoding="utf-8")
        assert wizard.seed_digest(text) == wizard.SEED_SHA256.get(ru_rel), (
            f"{ru_rel} changed: set SEED_SHA256[{ru_rel!r}] in tools/setup.py to {wizard.seed_digest(text)!r}")


def test_every_seed_page_has_an_english_template():
    for _ru, en_rel in wizard.SEED_PAGES:
        assert (wizard.TEMPLATES / "en" / Path(en_rel).name).is_file(), en_rel


def test_english_mode_replaces_untouched_seeds_and_keeps_edited_ones(workdir):
    _seed_tree(workdir)
    (workdir / "work" / "ЧЕКЛИСТ.md").write_text("# Чеклист\n\n- [ ] моя проверка\n", encoding="utf-8")
    notes = wizard.localize_owner_pages(workdir, "en")
    assert (workdir / "work" / "BRIEF.md").read_text(encoding="utf-8").startswith("# Project brief")
    assert not (workdir / "work" / "БРИФ.md").exists()
    assert (workdir / "work" / "DIGEST.md").exists() and (workdir / "work" / "UNANSWERED.md").exists()
    assert (workdir / "work" / "ЧЕКЛИСТ.md").exists() and not (workdir / "work" / "CHECKLIST.md").exists()
    assert any("you have edited it" in note for note in notes)
    assert wizard.localize_owner_pages(workdir, "ru") == []  # Russian is the shipped language


@needs_git
def test_ensure_git_repo_initializes_once(workdir):
    (workdir / "producer.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    assert wizard.ensure_git_repo(workdir) is True
    assert (workdir / ".git").exists()
    assert wizard.ensure_git_repo(workdir) is False


# ------------------------------------------------------------------ routes, and the Guardian's own tuning
GUARDIAN_TOML = """\
[project]
name = "p"

[guardian]
interval_seconds = 60
ignore_titles = ["Blitz"]
producer_command = "OLD"
recover_minutes = 30

[[producer_routes]]
name = "claude-opus"
command = "claude --model claude-opus-5-5 --effort high"

[[producer_routes]]
name = "other"
command = "x"
"""


def test_setup_keeps_the_guardians_own_tuning_and_replaces_only_command_routes_and_roster(workdir):
    config = workdir / "producer.toml"
    config.write_text(GUARDIAN_TOML, encoding="utf-8")
    _run(workdir, ["--non-interactive", "--name", "p", "--yes"], which=fake_which(["claude", "orca"]),
         out=lambda _l: None)
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["guardian"]["ignore_titles"] == ["Blitz"] and data["guardian"]["recover_minutes"] == 30
    assert data["guardian"]["producer_command"] == "claude --model claude-sonnet-5-5 --effort medium"
    assert [r["name"] for r in data["producer_routes"]] == ["claude-sonnet"]  # solo-claude: one route
    assert data["producer_routes"][0]["command"] == "claude --model claude-sonnet-5-5 --effort medium"
    assert "args_unattended" not in data["producer_routes"][0]


def test_unattended_puts_the_bypass_flag_in_args_unattended_only(workdir):
    _run(workdir, ["--non-interactive", "--name", "p", "--yes", "--unattended"],
         which=fake_which(["claude", "orca"]), out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    route = data["producer_routes"][0]
    assert route["args_unattended"] == "--dangerously-skip-permissions"
    assert "--dangerously" not in route["command"]


def test_full_preset_routes_follow_what_is_installed(workdir):
    _run(workdir, ["--non-interactive", "--name", "p", "--yes"],
         which=fake_which(["claude", "codex", "orca"]), out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert [r["name"] for r in data["producer_routes"]] == ["claude-sonnet", "codex-luna"]


# ------------------------------------------------------------------ Claude permission mode
def test_permission_mode_a_merges_user_and_project_settings_without_dropping_keys(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        '{"theme": "dark", "permissions": {"allow": ["Bash(git:*)"]}}', encoding="utf-8")
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.local.json").write_text('{"existing": true}', encoding="utf-8")
    notes = wizard.apply_permission_settings(root, "unattended", home=home)
    user = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert user["theme"] == "dark" and user["skipDangerousModePermissionPrompt"] is True
    assert user["permissions"]["allow"] == ["Bash(git:*)"]  # existing keys survive
    project = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert project["existing"] is True
    assert project["permissions"]["defaultMode"] == "bypassPermissions"
    assert notes


def test_permission_mode_b_unions_the_allow_list_and_keeps_existing_keys(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.local.json").write_text(
        '{"permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]}}', encoding="utf-8")
    wizard.apply_permission_settings(root, "attended", home=tmp_path / "home")
    project = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert project["permissions"]["defaultMode"] == "acceptEdits"
    allow = project["permissions"]["allow"]
    assert "Bash(ls:*)" in allow and "Bash(git:*)" in allow
    assert project["permissions"]["deny"] == ["Bash(rm:*)"]  # untouched


def test_apply_permission_settings_creates_missing_files(tmp_path):
    root = tmp_path / "proj"
    assert wizard.apply_permission_settings(root, "attended", home=tmp_path / "home")
    assert (root / ".claude" / "settings.local.json").is_file()


def test_non_interactive_unattended_writes_the_permission_files(workdir, tmp_path):
    home = tmp_path / "home"
    _run(workdir, ["--non-interactive", "--name", "p", "--yes", "--unattended"],
         which=fake_which(["claude", "orca"]), out=lambda _l: None, home=home)
    assert json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))[
        "skipDangerousModePermissionPrompt"] is True
    project = json.loads((workdir / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert project["permissions"]["defaultMode"] == "bypassPermissions"


def test_non_interactive_attended_writes_the_allow_list(workdir, tmp_path):
    _run(workdir, ["--non-interactive", "--name", "p", "--yes", "--attended"],
         which=fake_which(["claude", "orca"]), out=lambda _l: None, home=tmp_path / "home")
    project = json.loads((workdir / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert project["permissions"]["defaultMode"] == "acceptEdits"


def test_non_interactive_without_a_permission_flag_writes_nothing(workdir, tmp_path):
    home = tmp_path / "home"
    _run(workdir, ["--non-interactive", "--name", "p", "--yes"],
         which=fake_which(["claude", "orca"]), out=lambda _l: None, home=home)
    assert not (workdir / ".claude" / "settings.local.json").exists()
    assert not (home / ".claude" / "settings.json").exists()


def test_interactive_permission_question_blank_answer_takes_the_recommended_a(tmp_path):
    # name, lang, timezone, preset, permission mode (blank -> a), accept roster, globs
    answers = iter(["int", "", "", "", "", "y", ""])
    code = wizard.run_setup(tmp_path, [], which=fake_which(["claude", "orca"]), environ={},
                            input_fn=lambda _prompt: next(answers), out=lambda _l: None,
                            keys_path=tmp_path / "keys.env", git_init=False,
                            gate_installer=lambda _r: (True, "ok"), home=tmp_path / "home")
    assert code == 0
    user = json.loads((tmp_path / "home" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert user["skipDangerousModePermissionPrompt"] is True
    data = tomllib.loads((tmp_path / "producer.toml").read_text(encoding="utf-8"))
    assert data["producer_routes"][0]["args_unattended"] == "--dangerously-skip-permissions"


# ------------------------------------------------------------------ presets.toml
PRESET_FILE_TEXT = """\
[[preset]]
name = "solo-claude"
label = "Solo Claude"
who = "one Claude subscription"
cost = "$20/mo"

[[preset.producer_routes]]
name = "claude-sonnet"
agent = "claude"
command = "claude --model claude-sonnet-5-5 --effort medium"
args_unattended = "--dangerously-skip-permissions"
limit_patterns = ["usage limit", "\\\\d-hour limit"]
probe = "claude --version"

[[preset.roster]]
name = "sonnet"
title_prefix = "Sonnet"
count = 1
command = "claude --model claude-sonnet-5-5 --effort medium"

[[preset]]
name = "claude-plus-go"
label = "Claude + Go"
who = "Claude and an OpenCode key"
cost = "$20 + usage"

[[preset.producer_routes]]
name = "claude-sonnet"
agent = "claude"
command = "claude --model claude-sonnet-5-5 --effort medium"
args_unattended = "--dangerously-skip-permissions"

[[preset.roster]]
name = "deepseek"
title_prefix = "DS"
count = 2
command = "opencode --model opencode-go/deepseek-v4.1-flash"

[[preset.roster]]
name = "agy"
title_prefix = "AGY"
count = 1
command = "agy --mode accept-edits --dangerously-skip-permissions"
"""


@pytest.fixture
def presets_file(tmp_path, monkeypatch):
    path = tmp_path / "presets.toml"
    path.write_text(PRESET_FILE_TEXT, encoding="utf-8")
    monkeypatch.setattr(wizard, "PRESETS_FILE", path)
    return path


def test_the_shipped_presets_file_loads_and_names_the_four_presets():
    names = [p["name"] for p in wizard.load_presets(TOOLS / "presets.toml")]
    assert names[:1] == ["solo-claude"]
    assert {"claude-max-100", "claude-plus-go", "budget-free"} <= set(names)
    for preset in wizard.load_presets(TOOLS / "presets.toml"):
        assert preset["cost"] and preset["roster"], preset["name"]


def test_a_missing_presets_file_falls_back_to_the_builtin_presets(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "PRESETS_FILE", tmp_path / "absent.toml")
    assert wizard.load_presets() == []
    assert wizard.choose_preset("auto", {"claude": "/c", "codex": "/x"}, []) == "full"


def test_list_presets_shows_cost_and_what_is_missing(presets_file):
    out = []
    code = wizard.run_setup(Path(tempfile.mkdtemp()), ["--list-presets"], which=fake_which(["claude"]),
                            environ={}, input_fn=lambda _p: "", out=out.append, keys_path=Path(tempfile.mkdtemp()) / "k",
                            git_init=False)
    text = "\n".join(out)
    assert code == 0 and "solo-claude" in text and "$20/mo" in text and "needs agy, opencode" in text


def test_auto_picks_the_fullest_preset_the_machine_can_run(presets_file):
    presets = wizard.load_presets()
    both = {"claude": "/c", "opencode": "/o", "agy": "/a", "orca": "/x"}
    assert wizard.choose_preset("auto", both, presets) == "claude-plus-go"
    assert wizard.choose_preset("auto", {"claude": "/c", "opencode": "/o"}, presets) == "full"  # agy missing
    assert wizard.choose_preset("auto", {"claude": "/c"}, presets) == "solo-claude"


def test_a_preset_is_written_from_the_file_and_the_bypass_flag_needs_consent(workdir, presets_file):
    which = fake_which(["claude", "opencode", "agy", "orca"])
    _run(workdir, ["--non-interactive", "--name", "p", "--yes"], which=which, out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert data["setup"]["preset"] == "claude-plus-go"
    assert [r["name"] for r in data["roster"]] == ["deepseek", "agy"]
    assert "args_unattended" not in data["producer_routes"][0]
    assert all("--dangerously" not in r["command"] for r in data["roster"])
    _run(workdir, ["--non-interactive", "--name", "p", "--yes", "--reset-guardian", "--unattended"], which=which,
         out=lambda _l: None)
    data = tomllib.loads((workdir / "producer.toml").read_text(encoding="utf-8"))
    assert data["producer_routes"][0]["args_unattended"] == "--dangerously-skip-permissions"
    assert "--dangerously" not in [r for r in data["roster"] if r["name"] == "deepseek"][0]["command"]


def test_an_unknown_preset_is_an_error_not_a_silent_default(workdir, presets_file):
    out = []
    code = _run(workdir, ["--non-interactive", "--name", "p", "--preset", "nope"],
                which=fake_which(["claude", "orca"]), out=out.append)
    assert code == 2 and any("unknown preset" in line for line in out)


def test_the_windows_system_shell_is_not_mistaken_for_command_code():
    found = wizard.detect_clis(lambda name: r"C:\WINDOWS\system32\cmd.EXE" if name == "cmd" else None)
    assert found["cmd"] is None
    found = wizard.detect_clis(lambda name: "/usr/local/bin/cmd" if name == "cmd" else None)
    assert found["cmd"] == "/usr/local/bin/cmd"
    assert wizard.choose_preset("auto", {"claude": "/c", "cmd": None}) == "solo-claude"


def test_setup_output_is_plain_ascii_punctuation(workdir):
    out = []
    _run(workdir, ["--non-interactive", "--name", "a", "--yes"], which=fake_which(["claude", "orca"]), out=out.append)
    assert not any("\u2014" in line or "\u00b7" in line for line in out)


@needs_git
def test_the_review_baseline_skips_the_templates_history_once(workdir):
    wizard._run_git(workdir, "init", "-q")
    (workdir / "tools").mkdir()
    (workdir / "tools" / "commit_review.py").write_text("", encoding="utf-8")
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return Done()

    assert wizard.set_review_baseline(workdir, runner) == ""  # no commit yet: nothing to skip
    wizard._run_git(workdir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "template")
    assert "starts after the template" in wizard.set_review_baseline(workdir, runner)
    assert calls[0][-3:-2] == ["HEAD"] or "HEAD" in calls[0]
    state = workdir / "work" / "agents" / "state"
    state.mkdir(parents=True)
    (state / "COMMIT_REVIEW_STATE.json").write_text("{}", encoding="utf-8")
    assert wizard.set_review_baseline(workdir, runner) == ""  # already set: left alone
    assert len(calls) == 1

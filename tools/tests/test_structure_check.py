import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import structure_check as sc  # noqa: E402


def _repo(tmp_path, files, page):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    pg = tmp_path / "STRUCTURE.md"
    pg.write_text(page, encoding="utf-8")
    subprocess.run(["git", "add", *files], cwd=tmp_path, check=True)
    return pg


def test_in_sync(tmp_path):
    pg = _repo(tmp_path, ["README.md", "src/a.py"], "- `README.md` — r\n- `src/` — s\n")
    assert sc.check(tmp_path, pg) == ([], [])


def test_new_folder_is_missing(tmp_path):
    pg = _repo(tmp_path, ["README.md", "src/a.py", "site/x.html"], "- `README.md` — r\n- `src/` — s\n")
    assert sc.check(tmp_path, pg) == (["site/"], [])


def test_removed_folder_is_stale(tmp_path):
    pg = _repo(tmp_path, ["README.md"], "- `README.md` — r\n- `old/` — gone\n- `.runtime/` — ignored\n")
    assert sc.check(tmp_path, pg) == ([], ["old/"])


def test_report_topics_are_free(tmp_path):
    pg = _repo(tmp_path, ["work/agents/reports/ui/2026_01_01_X.md"],
               "- `work/` — w\n- `work/agents/` — a\n- `work/agents/reports/` — r\n")
    assert sc.check(tmp_path, pg) == ([], [])

"""tools/serve.py: one fixed address, honest about a busy port, recognisable as ours."""
from __future__ import annotations

import threading
import urllib.request

import pytest

import serve


@pytest.fixture
def running(tmp_path):
    (tmp_path / "index.html").write_text("<h1>héllo</h1>", encoding="utf-8")
    server = serve.make_server(tmp_path, 0, "my-site")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, server.server_address[1], tmp_path
    server.shutdown()
    server.server_close()


def test_it_serves_files_with_dev_headers_and_its_identity(running):
    _server, port, _dir = running
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as reply:
        assert "héllo" in reply.read().decode("utf-8")
        assert reply.headers["Cache-Control"].startswith("no-store")
        assert reply.headers[serve.HEADER] == "my-site"


def test_the_identity_check_tells_our_server_from_another_one(running):
    _server, port, _dir = running
    assert serve.is_ours(port, "my-site") is True
    assert serve.is_ours(port, "another-project") is False


def test_a_busy_port_is_refused_not_replaced(running, capsys, tmp_path):
    _server, port, directory = running
    code = serve.main(["--dir", str(directory), "--port", str(port)])
    out = capsys.readouterr().err
    assert "busy" in out and "never moves to another one" in out
    assert code == 1  # something else (a different project name) holds it


def test_check_reports_not_running_on_a_free_port(capsys):
    assert serve.main(["--check", "--port", "1"]) == 1
    assert "not running" in capsys.readouterr().out


def test_a_missing_folder_is_a_clear_error(capsys, tmp_path):
    assert serve.main(["--dir", str(tmp_path / "nope"), "--port", "0"]) == 2
    assert "nothing to serve" in capsys.readouterr().err


def test_the_port_and_folder_come_from_the_web_section(monkeypatch):
    monkeypatch.setattr(serve.paths, "_section", lambda name: {"dir": "public", "port": 9123} if name == "web" else {})
    assert serve.paths.web_dir() == "public" and serve.paths.web_port() == 9123
    monkeypatch.setattr(serve.paths, "_section", lambda name: {"port": "bad"} if name == "web" else {})
    assert serve.paths.web_port() == 8080 and serve.paths.web_dir() == "site"

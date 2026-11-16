"""Shared fixtures: a fake SoC login server and an App wired to it.

Nothing here touches the real SoC servers, the real keyring or ~/.ssh.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pikepdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

USER = "tester"
PASSWORD = "correct horse battery"


def _short_tmp() -> Path:
    # Unix socket paths are limited to ~100 characters, so stay out of pytest's deep tmp dirs.
    root = os.environ.get("SOC_PRINT_TEST_ROOT") or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(tempfile.mkdtemp(prefix="spt-", dir=root))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeSoC:
    def __init__(self, root: Path):
        self.state = root / "soc"
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("fake_soc.py")),
             "--port", str(self.port), "--state", str(self.state),
             "--user", USER, "--password", PASSWORD],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        line = self.proc.stdout.readline()
        assert line.startswith("READY"), line + self.proc.stdout.read()

    def events(self, kind: str) -> list[dict]:
        import json
        log = self.state / "auth.log"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        return [r for r in rows if r["event"] == kind]

    def jobs(self) -> list[Path]:
        return sorted((self.state / "jobs").iterdir())

    def clear(self) -> None:
        for job in self.jobs():
            job.unlink()
        (self.state / "auth.log").unlink(missing_ok=True)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


@pytest.fixture(scope="session")
def soc():
    root = _short_tmp()
    server = FakeSoC(root)
    yield server
    server.stop()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def env(soc, monkeypatch):
    """An isolated home for one test: config, cache, runtime dir, keyring, known_hosts."""
    root = _short_tmp()
    for name in ("config", "cache", "run", "data"):
        (root / name).mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root / "run"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    monkeypatch.setenv(
        "SOC_PRINT_SSH_ARGS",
        f"-F /dev/null -o UserKnownHostsFile={root / 'known_hosts'} "
        "-o GlobalKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o IdentityAgent=none",
    )
    soc.clear()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def make_app(env, soc, monkeypatch):
    """Factory for Apps pointed at the fake server; all are closed after the test."""
    from socprint import app as app_module

    # Never read the developer's ~/.ssh/config or default to the real SoC hosts.
    monkeypatch.setattr(app_module, "guess_username", lambda hosts: "")
    monkeypatch.setitem(app_module.DEFAULTS, "host", "127.0.0.1")
    monkeypatch.setitem(app_module.DEFAULTS, "jump_host", "localhost")
    apps = []

    def factory():
        app = app_module.App()
        app.config.update(port=soc.port, jump_port=soc.port)
        app._apply_target()
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()


def sign_in(app, password=PASSWORD, **extra):
    app.sign_in({"username": USER, "password": password, "host": "127.0.0.1",
                 "route": "direct", "remember": True, **extra})


def make_pdf(path: Path, pages: int, size=(595.276, 841.89)) -> Path:
    with pikepdf.Pdf.new() as pdf:
        for _ in range(pages):
            pdf.add_blank_page(page_size=size)
        pdf.save(path)
    return path


def add_file(app, path: Path) -> dict:
    entry = app.new_file(path.name, path.stat().st_size)
    shutil.copyfile(path, entry.workdir / "upload")
    return app.ingest(entry)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False

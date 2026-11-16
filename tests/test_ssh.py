"""Host keys and backend details, on whichever SSH backend this system uses."""

import base64
import os

import pytest

from socprint.ssh import HOST_KEY_CHANGED, SSHError, default_backend
from tests.conftest import sign_in


def known_hosts(env):
    return (env / "known_hosts").read_text().splitlines()


def test_a_new_host_key_is_pinned(make_app, env, soc):
    app = make_app()
    sign_in(app)
    (line,) = known_hosts(env)
    assert line.startswith(f"[127.0.0.1]:{soc.port} ssh-ed25519 ")
    app.close()
    sign_in(make_app())  # the pinned key matches next time
    assert len(known_hosts(env)) == 1


def test_a_changed_host_key_stops_before_any_password(make_app, env, soc):
    fake_key = base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + b"\x01" * 32).decode()
    (env / "known_hosts").write_text(f"[127.0.0.1]:{soc.port} ssh-ed25519 {fake_key}\n")
    with pytest.raises(SSHError, match="host key doesn't match"):
        sign_in(make_app())
    assert soc.events("password") == []
    assert HOST_KEY_CHANGED.startswith("The server's SSH host key")


def test_windows_uses_the_paramiko_backend(monkeypatch):
    monkeypatch.delenv("SOC_PRINT_SSH_BACKEND", raising=False)
    assert default_backend() == ("paramiko" if os.name == "nt" else "openssh")
    monkeypatch.setenv("SOC_PRINT_SSH_BACKEND", "paramiko")
    assert default_backend() == "paramiko"


def test_paramiko_backend_reads_the_supported_ssh_options(monkeypatch):
    pytest.importorskip("paramiko")
    from socprint.ssh_paramiko import _options
    monkeypatch.setenv("SOC_PRINT_SSH_ARGS",
                       "-F /dev/null -o UserKnownHostsFile=/x/kh -oPubkeyAuthentication=no "
                       "-i ~/.ssh/a -o IdentityFile=/k/b -o IdentityAgent=none")
    opts = _options()
    assert opts["userknownhostsfile"] == "/x/kh"
    assert opts["pubkeyauthentication"] == "no" and opts["identityagent"] == "none"
    assert opts["identityfile"] == ["~/.ssh/a", "/k/b"]

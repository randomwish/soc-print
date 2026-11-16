"""SSH transport built on paramiko, for systems where the OpenSSH backend can't work.

Windows' OpenSSH has no connection sharing (ControlMaster), and Python there has
no unix sockets for the password helper. Here the connection lives inside this
process instead: one authenticated paramiko Transport (two, through the jump
host) stays open and every command opens a channel on it, so the server still
sees one login. The password stays in this process's memory.

Host keys follow OpenSSH's StrictHostKeyChecking=accept-new against the same
~/.ssh/known_hosts: a new host is pinned by appending a line (the file is never
rewritten), and a changed key stops everything. A few OpenSSH options from
SOC_PRINT_SSH_ARGS are honoured: UserKnownHostsFile, IdentityFile (or -i),
PubkeyAuthentication and IdentityAgent=none.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    BadAuthenticationType,
    PasswordRequiredException,
    SSHException,
)

from .ssh import (
    HOST_KEY_CHANGED,
    AuthRequired,
    BaseConnection,
    NetworkError,
    SSHError,
    ssh_args,
)

CONNECT_TIMEOUT = 12
KEEPALIVE = 30
MAX_KEYS = 5  # stay under the server's MaxAuthTries, as the password may follow
DEFAULT_KEYS = ("id_ed25519", "id_ecdsa", "id_rsa")
# known_hosts records RSA keys as "ssh-rsa"; paramiko negotiates them as rsa-sha2-*.
_HOSTKEY_ALGOS = {"ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256")}


def _options() -> dict:
    """The OpenSSH options this backend understands, from SOC_PRINT_SSH_ARGS."""
    opts: dict = {"identityfile": []}
    args = ssh_args()
    i = 0
    while i < len(args):
        flag, value = args[i], None
        if flag in ("-o", "-i") and i + 1 < len(args):
            value, i = args[i + 1], i + 2
        elif flag[:2] in ("-o", "-i") and len(flag) > 2:
            flag, value, i = flag[:2], flag[2:], i + 1
        else:
            i += 1
            continue
        if flag == "-i":
            opts["identityfile"].append(value)
            continue
        key, _, val = value.partition("=")
        key, val = key.strip().lower(), val.strip()
        if key == "identityfile":
            opts["identityfile"].append(val)
        else:
            opts[key] = val
    return opts


def _host_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


class _KnownHosts:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _entry(self, name: str):
        keys = paramiko.HostKeys()
        if self.path.is_file():
            with contextlib.suppress(OSError, UnicodeDecodeError):
                keys.load(str(self.path))
        return keys.lookup(name)

    def preferred_algorithms(self, name: str, supported: tuple[str, ...]) -> tuple[str, ...]:
        """Ask for the key types already pinned for this host first, as OpenSSH does."""
        entry = self._entry(name)
        if not entry:
            return supported
        first = []
        for key_type in entry.keys():
            first += [a for a in _HOSTKEY_ALGOS.get(key_type, (key_type,)) if a in supported]
        return tuple(dict.fromkeys([*first, *supported]))

    def check(self, name: str, key: paramiko.PKey) -> None:
        with self._lock:
            entry = self._entry(name)
            if entry:
                pinned = entry.get(key.get_name())
                if pinned is None or pinned.asbytes() != key.asbytes():
                    raise SSHError(HOST_KEY_CHANGED)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(self.path.parent, 0o700)
            needs_newline = False
            if self.path.is_file() and self.path.stat().st_size:
                with self.path.open("rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    needs_newline = fh.read(1) != b"\n"
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(("\n" if needs_newline else "")
                         + f"{name} {key.get_name()} {key.get_base64()}\n")


class ParamikoConnection(BaseConnection):
    """A persistent connection held inside this process."""

    backend = "paramiko"

    def __init__(self, runtime_dir: Path):
        super().__init__()
        self.runtime_dir = runtime_dir
        self._opts = _options()
        known = self._opts.get("userknownhostsfile", "").split()
        self._known = _KnownHosts(Path(os.path.expanduser(known[0])) if known
                                  else Path.home() / ".ssh" / "known_hosts")
        self._main: paramiko.Transport | None = None
        self._jump: paramiko.Transport | None = None

    # ----------------------------------------------------------- lifecycle

    def is_connected(self) -> bool:
        main, jump = self._main, self._jump
        return bool(self.target and main and main.is_active() and main.is_authenticated()
                    and (jump is None or jump.is_active()))

    def connect(self, password: str | None) -> None:
        """Open the connection. Raises AuthRequired / AuthRejected / NetworkError."""
        t = self.target
        if not t:
            raise AuthRequired("Sign in with your SoC account to print")
        with self._connect_lock:
            if self.is_connected():
                return
            self.disconnect()
            route = self._pick_route()
            jump = main = None
            try:
                if route == "jump":
                    jump = self._open(self._socket(t.jump_host, t.jump_port),
                                      _host_name(t.jump_host, t.jump_port))
                    self._login(jump, password, route)
                    try:
                        sock = jump.open_channel("direct-tcpip", (t.host, t.port),
                                                 ("127.0.0.1", 0), timeout=CONNECT_TIMEOUT)
                    except (SSHException, OSError, EOFError) as exc:
                        raise self._unreachable(route, f"jump host refused: {exc}") from None
                else:
                    sock = self._socket(t.host, t.port)
                main = self._open(sock, _host_name(t.host, t.port))
                self._login(main, password, route)
            except SSHError:
                self._close(main, jump)
                raise
            except (OSError, EOFError, SSHException) as exc:
                self._close(main, jump)
                raise self._unreachable(route, str(exc) or type(exc).__name__) from None
            self._main, self._jump = main, jump
            self.route_used = route

    @staticmethod
    def _socket(host: str, port: int) -> socket.socket:
        return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)

    def _open(self, sock, name: str) -> paramiko.Transport:
        transport = paramiko.Transport(sock)
        try:
            transport.banner_timeout = CONNECT_TIMEOUT
            transport.handshake_timeout = CONNECT_TIMEOUT
            transport.auth_timeout = 30
            options = transport.get_security_options()
            options.key_types = self._known.preferred_algorithms(name, tuple(options.key_types))
            transport.start_client(timeout=CONNECT_TIMEOUT)
            self._known.check(name, transport.get_remote_server_key())
            transport.set_keepalive(KEEPALIVE)
        except BaseException:
            transport.close()
            raise
        return transport

    def _login(self, transport: paramiko.Transport, password: str | None, route: str) -> None:
        """Authenticate one hop. A password is offered at most once and never retried.

        With a password, the password goes first: offering every key beforehand can
        exhaust the server's attempt limit before it is asked for. Keys come next
        only for a server that won't take a password alone (key, then password).
        """
        user = self.target.user
        if password:
            try:
                transport.auth_password(user, password)  # falls back to keyboard-interactive
                return
            except BadAuthenticationType:
                pass  # the server wants something else first
            except AuthenticationException:
                raise self._rejected(route) from None
        for key in self._keys():
            try:
                remaining = transport.auth_publickey(user, key)
            except AuthenticationException:
                continue
            if transport.is_authenticated():
                return
            if password and "password" in remaining:
                try:
                    transport.auth_password(user, password)
                    return
                except AuthenticationException:
                    raise self._rejected(route) from None
        raise AuthRequired("Sign in with your SoC account to print")

    def _keys(self) -> list[paramiko.PKey]:
        if self._opts.get("pubkeyauthentication", "yes").lower() == "no":
            return []
        keys: list[paramiko.PKey] = []
        if self._opts.get("identityagent", "").lower() != "none":
            with contextlib.suppress(Exception):  # no agent running is normal
                keys += list(paramiko.Agent().get_keys())
        ssh_dir = Path.home() / ".ssh"
        files = [Path(os.path.expanduser(p)) for p in self._opts["identityfile"]]
        files += [ssh_dir / name for name in DEFAULT_KEYS]
        for path in dict.fromkeys(files):
            if not path.is_file():
                continue
            try:
                keys.append(paramiko.PKey.from_path(path))
            except (PasswordRequiredException, SSHException, OSError, ValueError):
                continue  # passphrase-protected or unreadable: the agent may hold it
        return keys[:MAX_KEYS]

    @staticmethod
    def _close(*transports) -> None:
        for transport in transports:
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()

    def disconnect(self) -> None:
        main, jump = self._main, self._jump
        self._main = self._jump = None
        self._close(main, jump)

    def close(self) -> None:
        self.disconnect()

    # ------------------------------------------------------------- running

    def run(self, remote_cmd: str, *, stdin: Path | None = None,
            timeout: float = 90) -> subprocess.CompletedProcess:
        def failed(message: str) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(remote_cmd, 255, "", message)

        main = self._main
        if main is None or not main.is_active():
            return failed("Not connected")
        try:
            channel = main.open_session(timeout=15)
        except (SSHException, OSError, EOFError) as exc:
            return failed(str(exc))

        out: list[bytes] = []
        err: list[bytes] = []

        def drain_stderr() -> None:
            with contextlib.suppress(Exception):
                while data := channel.recv_stderr(32768):
                    err.append(data)

        deadline = time.monotonic() + timeout
        try:
            with channel:
                channel.settimeout(timeout)
                channel.exec_command(remote_cmd)
                reader = threading.Thread(target=drain_stderr, name="ssh-stderr", daemon=True)
                reader.start()
                if stdin:
                    with stdin.open("rb") as fh:
                        while chunk := fh.read(65536):
                            channel.sendall(chunk)
                channel.shutdown_write()
                while data := channel.recv(32768):
                    out.append(data)
                reader.join(max(0.0, deadline - time.monotonic()))
                if not channel.status_event.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError
                status = channel.exit_status
        except (TimeoutError, socket.timeout):
            raise NetworkError("The server stopped responding") from None
        except (SSHException, OSError, EOFError) as exc:
            return failed(str(exc))
        return subprocess.CompletedProcess(
            remote_cmd, 255 if status < 0 else status,
            b"".join(out).decode("utf-8", "replace"), b"".join(err).decode("utf-8", "replace"),
        )

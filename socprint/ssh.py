"""SSH transport to the SoC login server, built on the system OpenSSH client.

One multiplexed master connection (ControlMaster) stays open and every print
command rides on it, so authentication happens once per connection rather than
once per command.

When ssh wants a password it runs a tiny helper (askpass.py) which asks this
process for the answer over a private unix socket. The password therefore never
appears on a command line, in an environment variable, or in a file.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$", re.IGNORECASE)
HOST_RE = re.compile(r"^(?!-)[a-z0-9.-]{1,253}$", re.IGNORECASE)

CONTROL_PERSIST = "30m"
REMOTE_PATH = 'PATH="$PATH:/usr/local/bin:/usr/bin:/bin"; export PATH; '


class SSHError(Exception):
    kind = "error"


class AuthRequired(SSHError):
    """No usable credentials: the user has to sign in."""

    kind = "auth"


class AuthRejected(SSHError):
    """A password was offered and the server refused it."""

    kind = "rejected"


class NetworkError(SSHError):
    kind = "network"


@dataclass(frozen=True)
class Target:
    user: str
    host: str
    port: int = 22
    jump_host: str = ""
    jump_port: int = 22
    route: str = "auto"  # "auto" | "direct" | "jump"

    def validate(self) -> None:
        if not USER_RE.fullmatch(self.user):
            raise ValueError("That doesn't look like an SoC username")
        for host in filter(None, (self.host, self.jump_host)):
            if not HOST_RE.fullmatch(host):
                raise ValueError(f"Invalid host name “{host}”")
        if self.route not in ("auto", "direct", "jump"):
            raise ValueError("Invalid connection route")
        if self.route == "jump" and not self.jump_host:
            raise ValueError("No jump host configured")


def sh_command(script: str, *args: str) -> str:
    """A remote command line that runs `script` under POSIX sh with positional args.

    Going through `sh -c` keeps this working whatever the account's login shell is.
    """
    assert "'" not in script and "\n" not in script
    return " ".join(["sh", "-c", shlex.quote(script), "sh", *map(shlex.quote, args)])


class _Askpass:
    """Answers ssh's password prompts, only while a connection attempt is under way."""

    def __init__(self, runtime_dir: Path):
        # Named per process so a second instance can't take over the first one's socket.
        self._sweep_stale(runtime_dir)
        self.sock_path = runtime_dir / f"askpass-{os.getpid()}.sock"
        self.helper_path = runtime_dir / f"askpass-{os.getpid()}"
        self._lock = threading.Lock()
        self._armed: dict | None = None

        self.sock_path.unlink(missing_ok=True)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        self._srv.listen(8)

        helper = Path(__file__).with_name("askpass.py")
        self.helper_path.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} \"$@\"\n"
        )
        os.chmod(self.helper_path, stat.S_IRWXU)
        threading.Thread(target=self._serve, name="askpass", daemon=True).start()

    @staticmethod
    def _sweep_stale(runtime_dir: Path) -> None:
        """Remove helper files left by instances that are no longer running."""
        for leftover in runtime_dir.glob("askpass-*"):
            pid = leftover.name.removeprefix("askpass-").removesuffix(".sock")
            try:
                os.kill(int(pid), 0)
            except (ValueError, ProcessLookupError):
                leftover.unlink(missing_ok=True)
            except PermissionError:
                pass  # alive, and not ours

    @contextlib.contextmanager
    def armed(self, password: str | None, principals: list[str]):
        """Allow one answer per `user@host` principal for the duration of the block."""
        state = {"password": password, "pending": {p.lower() for p in principals}, "sent": False}
        with self._lock:
            self._armed = state
        try:
            yield state
        finally:
            with self._lock:
                self._armed = None

    def _answer(self, prompt: str) -> str | None:
        low = prompt.lower()
        with self._lock:
            state = self._armed
            if not state or not state["password"]:
                return None
            if "password" not in low or "passphrase" in low:
                return None
            for principal in state["pending"]:
                if principal in low:
                    # Answer each hop once, so a wrong password is never retried.
                    state["pending"].discard(principal)
                    state["sent"] = True
                    return state["password"]
        return None

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(5)
                    if not self._peer_is_me(conn):
                        continue
                    chunks = []
                    while chunk := conn.recv(4096):
                        chunks.append(chunk)
                    answer = self._answer(b"".join(chunks).decode("utf-8", "replace"))
                    conn.sendall(b"\x00" if answer is None else b"\x01" + answer.encode())
                except OSError:
                    continue

    @staticmethod
    def _peer_is_me(conn: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return True  # the 0700 runtime directory is the access control
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid == os.getuid()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._srv.close()
        self.sock_path.unlink(missing_ok=True)
        self.helper_path.unlink(missing_ok=True)


class Connection:
    """A persistent, multiplexed SSH connection to one login server."""

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.control_dir = runtime_dir / "cm"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime_dir, 0o700)
        self._askpass = _Askpass(runtime_dir)
        self._connect_lock = threading.Lock()
        self.target: Target | None = None
        self.route_used = ""
        self._extra = shlex.split(os.environ.get("SOC_PRINT_SSH_ARGS", ""))

    # ------------------------------------------------------------ plumbing

    def _common(self, methods: str = "publickey") -> list[str]:
        return [
            *self._extra,
            "-o", f"PreferredAuthentications={methods}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "ConnectTimeout=12",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
        ]

    def _base(self, *, proxy: bool = False, methods: str = "publickey") -> list[str]:
        t = self.target
        args = [
            "ssh", *self._common(methods),
            "-o", f"ControlPath={self.control_dir}/%C",
            "-o", f"ControlPersist={CONTROL_PERSIST}",
            "-p", str(t.port),
        ]
        if proxy:
            hop = [
                "ssh", *self._common(methods),
                "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-p", str(t.jump_port), "-W", "%h:%p", f"{t.user}@{t.jump_host}",
            ]
            args += ["-o", "ProxyCommand=" + shlex.join(hop)]
        return args

    def _dest(self) -> str:
        return f"{self.target.user}@{self.target.host}"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SSH_ASKPASS"] = str(self._askpass.helper_path)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["SOC_PRINT_ASKPASS_SOCK"] = str(self._askpass.sock_path)
        env.setdefault("DISPLAY", ":0")  # OpenSSH < 8.4 only uses askpass when DISPLAY is set
        return env

    # ----------------------------------------------------------- lifecycle

    def set_target(self, target: Target) -> None:
        target.validate()
        if target != self.target:
            self.disconnect()
            self.target = target
            self.route_used = ""

    def is_connected(self) -> bool:
        if not self.target:
            return False
        if not any(self.control_dir.iterdir()):
            return False
        proc = subprocess.run(
            [*self._base(), "-O", "check", self._dest()],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
        )
        return proc.returncode == 0

    def _direct_reachable(self) -> bool:
        try:
            with socket.create_connection((self.target.host, self.target.port), timeout=3):
                return True
        except OSError:
            return False

    def connect(self, password: str | None) -> None:
        """Open the master connection. Raises AuthRequired / AuthRejected / NetworkError."""
        t = self.target
        if not t:
            raise AuthRequired("Sign in with your SoC account to print")
        with self._connect_lock:
            if self.is_connected():
                return
            route = t.route
            if route == "auto":
                route = "direct" if self._direct_reachable() or not t.jump_host else "jump"
            principals = [f"{t.user}@{t.host}"]
            if route == "jump":
                principals.append(f"{t.user}@{t.jump_host}")

            # Which authentication methods ssh may use, in order of attempts:
            #  - with a password, just the password first: offering every key in the agent
            #    beforehand can exhaust the server's attempt limit before it is asked for;
            #  - then keys followed by the password, for a server that wants a key first;
            #  - without a password, keys only. ssh sends an empty password when a prompt
            #    goes unanswered, and failed attempts count against the account.
            plans = ["password", "publickey,password"] if password else ["publickey"]
            for methods in plans:
                returncode, stderr, offered = self._attempt(route, methods, password, principals)
                if returncode == 0:
                    self.route_used = route
                    return
                if offered or "permission denied" not in stderr.lower():
                    break  # a rejected password is never offered twice
            raise self._classify(stderr, offered, route)

    def _attempt(self, route: str, methods: str, password: str | None,
                 principals: list[str]) -> tuple[int, str, bool]:
        argv = [*self._base(proxy=(route == "jump"), methods=methods),
                "-o", "ControlMaster=auto", self._dest(), "true"]
        # stderr goes to a file, not a pipe: the master that stays behind would
        # otherwise hold a pipe open and keep us waiting.
        with tempfile.TemporaryFile() as err, self._askpass.armed(password, principals) as st:
            try:
                proc = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                    env=self._env(), start_new_session=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                raise NetworkError(f"Timed out connecting to {self.target.host}") from None
            err.seek(0)
            return proc.returncode, err.read().decode("utf-8", "replace"), st["sent"]

    def _classify(self, stderr: str, offered_password: bool, route: str) -> SSHError:
        t = self.target
        low = stderr.lower()
        lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
        last = lines[-1] if lines else "ssh failed without a message"
        if "host key verification failed" in low or "identification has changed" in low:
            return SSHError(
                "The server's SSH host key doesn't match the one saved in ~/.ssh/known_hosts. "
                "Nothing was sent. Check with SoC Technical Services before removing the old key."
            )
        if "permission denied" in low or "too many authentication failures" in low:
            if offered_password:
                hint = ""
                if route == "jump":
                    hint = (f". If the password is right, check that this device's SSH key is "
                            f"registered with skeys, which {t.jump_host} requires on NUS Wi-Fi")
                return AuthRejected(f"The server rejected that username or password{hint}")
            return AuthRequired("Sign in with your SoC account to print")
        network_signs = ("could not resolve", "timed out", "no route to host", "refused",
                         "network is unreachable", "connection closed", "connection reset",
                         "kex_exchange_identification", "banner exchange")
        if any(sign in low for sign in network_signs):
            if route == "jump":
                hint = (f"Couldn't reach {t.host} through {t.jump_host}. The jump host only "
                        "accepts connections from inside NUS (use NUS VPN elsewhere), and on "
                        "NUS Wi-Fi this device's SSH key must be registered with skeys.")
            else:
                hint = (f"Couldn't reach {t.host}. It is only reachable directly from the SoC "
                        "network; elsewhere, set the connection to “via jump host”.")
            return NetworkError(f"{hint} ({last})")
        return SSHError(f"SSH connection failed: {last}")

    def disconnect(self) -> None:
        if not self.target:
            return
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([*self._base(), "-O", "exit", self._dest()],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        for leftover in self.control_dir.iterdir():
            leftover.unlink(missing_ok=True)

    def close(self) -> None:
        self.disconnect()
        self._askpass.close()

    # ------------------------------------------------------------- running

    def run(self, remote_cmd: str, *, stdin: Path | None = None,
            timeout: float = 90) -> subprocess.CompletedProcess:
        """Run a command over the open master connection (never prompts)."""
        argv = [*self._base(), "-o", "ControlMaster=no", "-o", "BatchMode=yes",
                self._dest(), remote_cmd]
        handle = stdin.open("rb") if stdin else None
        try:
            return subprocess.run(
                argv, stdin=handle or subprocess.DEVNULL, capture_output=True,
                text=True, errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise NetworkError("The server stopped responding") from None
        finally:
            if handle:
                handle.close()

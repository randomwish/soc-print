"""End-to-end check of a standalone build against the fake SoC server.

    uv run python tests/smoke_exe.py dist/soc-print

Starts the executable with throwaway settings, loads the page and its assets,
signs in (through the real ssh client and the build's own password helper on
Linux and macOS), uploads a PDF, prints it and checks the fake server got it.
Not collected by pytest; CI runs it on every build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.conftest import PASSWORD, USER, FakeSoC, _short_tmp, free_port, make_pdf  # noqa: E402


def request(port, method, path, body=None, *, token=None, raw=None, timeout=60):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token:
        req.add_header("X-SOC-Print-Token", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def stop(proc: subprocess.Popen) -> None:
    """Stop the build and the child process a one-file build runs in."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    elif proc.poll() is None:
        proc.terminate()  # the bootloader passes the signal on to its child
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    root = _short_tmp()
    soc = FakeSoC(root)
    proc = None
    try:
        dirs = {name: root / name for name in ("config", "cache", "run", "data")}
        for d in dirs.values():
            d.mkdir()
        (dirs["config"] / "soc-print").mkdir()
        (dirs["config"] / "soc-print" / "config.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": soc.port, "jump_host": "localhost",
             "jump_port": soc.port}))
        env = {**os.environ,
               "XDG_CONFIG_HOME": str(dirs["config"]), "XDG_CACHE_HOME": str(dirs["cache"]),
               "XDG_RUNTIME_DIR": str(dirs["run"]), "XDG_DATA_HOME": str(dirs["data"]),
               "SOC_PRINT_SSH_ARGS":
                   f"-F /dev/null -o UserKnownHostsFile={(root / 'known_hosts').as_posix()} "
                   "-o GlobalKnownHostsFile=/dev/null -o PubkeyAuthentication=no "
                   "-o IdentityAgent=none"}
        for var in ("PYTHON_KEYRING_BACKEND", "SOC_PRINT_SSH_BACKEND"):
            env.pop(var, None)  # the build must find its own keyring and backend

        version = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                                 env=env, timeout=120)
        print("version:", version.stdout.strip() or version.stderr.strip())
        assert version.returncode == 0, version

        port = free_port()
        # Output goes to a file: a one-file build runs as a bootloader plus a child, and
        # a child that outlives the bootloader would keep a pipe open forever.
        log = (root / "build-output.txt").open("w+")
        proc = subprocess.Popen([str(exe), "--no-open", "--port", str(port)], env=env,
                                cwd=root, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                status, body = request(port, "GET", "/api/ping", timeout=2)
                if status == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                raise SystemExit("the build exited:\n" + (root / "build-output.txt").read_text())
            time.sleep(0.5)
        else:
            raise SystemExit("the build never answered /api/ping")
        print("ping:", body.decode())

        status, page = request(port, "GET", "/")
        assert status == 200, status
        token = re.search(rb'name="soc-print-token" content="([^"]+)"', page)[1].decode()
        for asset in ("app.js", "style.css", "icon.svg"):
            assert request(port, "GET", f"/static/{asset}")[0] == 200, asset

        status, body = request(port, "GET", "/api/state", token=token)
        state = json.loads(body)
        print("keyring:", state["auth"].get("keyringName") or "none found")

        status, body = request(port, "POST", "/api/signin", {
            "username": USER, "password": PASSWORD, "host": "127.0.0.1", "route": "direct",
            "remember": False}, token=token)
        assert status == 200, body
        auth = json.loads(body)["auth"]
        assert auth["connected"], auth
        print("signed in, route:", auth["route"])

        pdf = make_pdf(root / "smoke.pdf", 3)
        status, body = request(port, "POST", "/api/files?name=Smoke%20test.pdf",
                               raw=pdf.read_bytes(), token=token)
        assert status == 200, body
        file = json.loads(body)
        assert file["pages"] == 3 and not file["error"], file

        status, body = request(port, "POST", "/api/print", {
            "printer": "psts", "sides": "duplex", "banner": True, "nup": 1, "copies": 1,
            "files": [{"id": file["id"], "pages": ""}]}, token=token)
        result = json.loads(body)
        assert status == 200 and result["results"][0]["ok"], result
        jobs = [p.name for p in soc.jobs()]
        assert jobs == ["100__psts__Smoke_test.pdf"], jobs
        print("printed:", jobs[0])

        status, body = request(port, "GET", "/api/quota", token=token)
        assert "47 pages left" in json.loads(body)["text"], body
        print("smoke test passed")
        return 0
    finally:
        if proc:
            stop(proc)
            output = (root / "build-output.txt").read_text(errors="replace")
            if output.strip():
                print("--- build output\n" + output)
        soc.stop()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

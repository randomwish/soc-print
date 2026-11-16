"""A stand-in for the SoC login server, for tests and for trying the UI safely.

It speaks real SSH, so the OpenSSH client, password prompts, connection sharing
and jump-host forwarding are all exercised for real. Commands run locally with
fake lpr / lpq / lprm / pusage that record jobs in a folder instead of printing.

    uv run python tests/fake_soc.py --port 2222 --state /tmp/fake-soc

Then point SoC Print at it (see README, "Trying it without printing").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import asyncssh

STUB = r'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path

sys.stdout.reconfigure(newline="\n")  # a real server sends unix line endings
tool = os.environ.get("FAKE_SOC_TOOL") or Path(sys.argv[0]).name
args = sys.argv[1:]
jobs = Path(os.environ["FAKE_SOC_STATE"]) / "jobs"
user = os.environ.get("FAKE_SOC_USER", "tester")

def take_queue():
    for i, arg in enumerate(args):
        if arg == "-P":
            queue = args[i + 1]
            del args[i:i + 2]
            return queue
        if arg.startswith("-P"):
            del args[i]
            return arg[2:]
    sys.exit(f"{tool}: no queue given")

if tool == "pusage":
    print(f"Print quota for {user}")
    print("  PS (b/w)      : 47 pages left this month")
    print("  CP-A4 (colour): 0 pages")
    sys.exit(0)

queue = take_queue()
if queue.startswith("nosuch"):
    sys.exit(f"{tool}: {queue}: unknown printer")

if tool == "lpr":
    for path in args:
        number = 100 + len(list(jobs.iterdir()))
        shutil.copyfile(path, jobs / f"{number}__{queue}__{Path(path).name}")
elif tool == "lpq":
    mine = sorted(p for p in jobs.iterdir() if p.name.split("__")[1] == queue)
    print(f"Printer: {queue}@printhost")
    print(f" Queue: {len(mine)} printable job" + ("" if len(mine) == 1 else "s"))
    if mine:
        print(" Rank   Owner/ID                  Class Job Files                 Size Time")
    for n, p in enumerate(mine):
        number, _, name = p.name.split("__")
        rank = "active" if n == 0 else str(n + 1)
        print(f"{rank:<7}{user + '@stu+' + number:<26}  A  {number} {name:<20} {p.stat().st_size} 12:00:00")
elif tool == "lprm":
    hit = [p for p in jobs.iterdir() if p.name.startswith(f"{args[0]}__{queue}__")]
    if not hit:
        sys.exit(f"lprm: job {args[0]} not found on {queue}")
    for p in hit:
        p.unlink()
    print(f"Printer {queue}@printhost: dequeued job {args[0]}")
'''


def find_sh() -> str | None:
    """On Windows, Git for Windows' sh runs the remote commands, as a login server would."""
    if os.name != "nt":
        return None
    for candidate in (shutil.which("sh"), r"C:\Program Files\Git\bin\sh.exe",
                      r"C:\Program Files\Git\usr\bin\sh.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    sys.exit("fake_soc.py needs Git for Windows (its sh) on Windows")


def write_stubs(state: Path) -> Path:
    bin_dir = state / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (state / "jobs").mkdir(exist_ok=True)
    if os.name == "nt":
        # sh can't run a Windows Python by shebang path; wrap the stub in sh scripts.
        stub = bin_dir / "fake_tool.py"
        stub.write_text(STUB)
        python, stub_path = Path(sys.executable).as_posix(), stub.as_posix()
        for tool in ("lpr", "lpq", "lprm", "pusage"):
            (bin_dir / tool).write_text(
                f'#!/bin/sh\nFAKE_SOC_TOOL={tool} exec "{python}" "{stub_path}" "$@"\n',
                newline="\n")
        return bin_dir
    script = STUB.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
    for tool in ("lpr", "lpq", "lprm", "pusage"):
        path = bin_dir / tool
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--state", type=Path, required=True, help="folder for jobs and logs")
    parser.add_argument("--user", default="tester")
    parser.add_argument("--password", default="correct horse battery")
    opts = parser.parse_args()

    state: Path = opts.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    bin_dir = write_stubs(state)
    sh = find_sh()
    log = state / "auth.log"

    def record(**event) -> None:
        with log.open("a") as fh:
            fh.write(json.dumps({"t": time.time(), **event}) + "\n")

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            ok = username == opts.user and password == opts.password
            record(event="password", user=username, ok=ok)
            return ok

        def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
            record(event="forward", dest=f"{dest_host}:{dest_port}")
            return True  # act as a jump host: `ssh -W` / ProxyJump

    async def handle(process: asyncssh.SSHServerProcess) -> None:
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_SOC_STATE"] = str(state)
        env["FAKE_SOC_USER"] = process.get_extra_info("username")
        record(event="exec", command=process.command)
        # Every command here is batch-style: take all of stdin, run, report. Sending the
        # output and exit status ourselves (rather than redirecting pipes) keeps the
        # status ahead of the channel close, as a real sshd does.
        stdin = await process.stdin.read()
        pipes = dict(env=env, cwd=state, stdin=asyncio.subprocess.PIPE,
                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        if sh:
            proc = await asyncio.create_subprocess_exec(sh, "-c", process.command or "true", **pipes)
        else:
            proc = await asyncio.create_subprocess_shell(process.command or "true", **pipes)
        out, err = await proc.communicate(stdin)
        process.stdout.write(out)
        process.stderr.write(err)
        process.exit(proc.returncode)

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        await asyncssh.create_server(
            Server, "127.0.0.1", opts.port, server_host_keys=[key],
            process_factory=handle, encoding=None,
        )
        print(f"READY fake SoC login server on 127.0.0.1:{opts.port} "
              f"(user {opts.user!r}, state in {state})", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

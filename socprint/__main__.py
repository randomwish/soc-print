"""Command line entry point: `python -m socprint` (or the ./soc-print launcher)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__

DEFAULT_PORT = 6310
PROJECT = Path(__file__).resolve().parent.parent
UNIT = "soc-print.service"


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as resp:
            return json.load(resp).get("app") == "soc-print"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def serve(port: int, open_browser: bool, verbose: bool) -> int:
    url = f"http://127.0.0.1:{port}/"
    if already_running(port):
        print(f"SoC Print is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0

    from .app import App
    from .server import make_server

    app = App()
    try:
        server = make_server(app, port, verbose=verbose)
    except OSError as exc:
        app.close()
        print(f"Can't listen on port {port} ({exc.strerror}). Try --port.", file=sys.stderr)
        return 1

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    print(f"SoC Print {__version__} is running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _desktop_file() -> Path:
    return Path.home() / ".local" / "share" / "applications" / "soc-print.desktop"


# ------------------------------------------------------------------ Windows

def _windows_programs() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _windows_shortcuts() -> tuple[Path, Path]:
    programs = _windows_programs()
    return programs / "Startup" / "SoC Print.lnk", programs / "SoC Print.lnk"


def _pythonw() -> str:
    """The console-less interpreter, so nothing flashes up at login."""
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return str(windowless if windowless.is_file() else sys.executable)


def _windows_shortcut(link: Path, arguments: str) -> None:
    # Paths travel in environment variables, so nothing needs PowerShell quoting.
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SP_LINK); "
              "$s.TargetPath = $env:SP_TARGET; $s.Arguments = $env:SP_ARGS; "
              "$s.WorkingDirectory = $env:SP_DIR; $s.Description = 'SoC Print'; $s.Save()")
    link.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SP_LINK": str(link), "SP_TARGET": _pythonw(), "SP_ARGS": arguments,
           "SP_DIR": str(PROJECT)}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, capture_output=True)


def install_windows(port: int) -> int:
    """Start at login (Startup folder) and add a Start menu shortcut."""
    startup, menu = _windows_shortcuts()
    try:
        _windows_shortcut(startup, f"-m socprint --no-open --port {port}")
        _windows_shortcut(menu, f"-m socprint --port {port}")
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        print(f"Couldn't create the shortcuts: {detail.decode(errors='replace') or exc}",
              file=sys.stderr)
        return 1
    if not already_running(port):
        subprocess.Popen([_pythonw(), "-m", "socprint", "--no-open", "--port", str(port)],
                         cwd=PROJECT, close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    print(f"SoC Print now starts when you sign in to Windows. Open http://127.0.0.1:{port}/ "
          "or “SoC Print” in the Start menu.")
    return 0


def uninstall_windows() -> int:
    for link in _windows_shortcuts():
        link.unlink(missing_ok=True)
    print("Removed the SoC Print startup entry and Start menu shortcut. If it is running, "
          "it stops when you sign out.")
    return 0


# -------------------------------------------------------------------- Linux

def install(port: int) -> int:
    """Start at login (systemd user service) and add an app-menu shortcut."""
    if os.name == "nt":
        return install_windows(port)
    if not shutil.which("systemctl"):
        print("systemd isn't available here; start SoC Print with ./soc-print instead.",
              file=sys.stderr)
        return 1
    unit = _systemd_dir() / UNIT
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\n"
        "Description=SoC Print (local drag-and-drop print site)\n\n"
        "[Service]\n"
        f"WorkingDirectory={PROJECT}\n"
        f"ExecStart={sys.executable} -m socprint --no-open --port {port}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    desktop = _desktop_file()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SoC Print\n"
        "Comment=Drag and drop files to print on NUS SoC printers\n"
        f"Exec=xdg-open http://127.0.0.1:{port}/\n"
        f"Icon={PROJECT / 'socprint' / 'static' / 'icon.svg'}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    done = subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=False)
    if done.returncode != 0:
        return done.returncode
    print(f"SoC Print now starts at login. Open http://127.0.0.1:{port}/ or the "
          "“SoC Print” entry in your app menu.")
    return 0


def uninstall() -> int:
    if os.name == "nt":
        return uninstall_windows()
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False,
                       capture_output=True)
    (_systemd_dir() / UNIT).unlink(missing_ok=True)
    _desktop_file().unlink(missing_ok=True)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("Removed the SoC Print login service and app-menu shortcut.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="soc-print", description="Drag-and-drop printing for NUS SoC printers.")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "install", "uninstall"],
                        help="serve (default), or install/uninstall the start-at-login service")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if args.command == "install":
        return install(args.port)
    if args.command == "uninstall":
        return uninstall()
    return serve(args.port, not args.no_open, args.verbose)


if __name__ == "__main__":
    sys.exit(main())

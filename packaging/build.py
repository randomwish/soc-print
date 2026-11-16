"""Build a standalone SoC Print executable for this system with PyInstaller.

    uv run --with "pyinstaller>=6.10" python packaging/build.py

Writes dist/soc-print (soc-print.exe on Windows) and a release archive next to it:
soc-print-<os>-<arch>.tar.gz on Linux and macOS (keeps the executable bit),
soc-print-windows-<arch>.zip on Windows.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def release_name() -> str:
    system = {"linux": "linux", "darwin": "macos", "win32": "windows"}[sys.platform]
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine)
    return f"soc-print-{system}-{arch}"


def main() -> int:
    os.chdir(ROOT)
    shutil.rmtree(DIST, ignore_errors=True)
    args = [
        str(ROOT / "packaging" / "entry.py"),
        "--name", "soc-print",
        "--onefile",
        "--console",  # its console window is the "running" indicator; closing it stops it
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'socprint' / 'static'}{os.pathsep}socprint/static",
        "--collect-submodules", "socprint",
        # keyring finds the OS keyring through its entry points, so keep its metadata.
        "--copy-metadata", "keyring",
        "--collect-submodules", "keyring.backends",
    ]
    # Never imported by SoC Print, but pulled in by dependencies: pikepdf's XMP
    # metadata support (lxml) and Pillow's AVIF codec (SoC Print doesn't take AVIF).
    for module in ("lxml", "PIL.AvifImagePlugin", "PIL._avif"):
        args += ["--exclude-module", module]
    if sys.platform == "win32":
        args += ["--collect-submodules", "paramiko"]
    else:
        # The paramiko SSH backend (and its crypto) is only used on Windows, and
        # debug symbols only cost space (they doubled the size of libpython).
        for module in ("socprint.ssh_paramiko", "paramiko", "nacl", "bcrypt"):
            args += ["--exclude-module", module]
        args.append("--strip")
    PyInstaller.__main__.run(args)

    exe = DIST / ("soc-print.exe" if sys.platform == "win32" else "soc-print")
    name = release_name()
    if sys.platform == "win32":
        archive = DIST / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(exe, f"{name}/soc-print.exe")
            zf.write(ROOT / "README.md", f"{name}/README.md")
    else:
        archive = DIST / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(exe, f"{name}/soc-print")
            tf.add(ROOT / "README.md", f"{name}/README.md")
    print(f"built {exe} ({exe.stat().st_size // 2**20} MB) and {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

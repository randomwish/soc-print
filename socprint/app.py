"""Application state and the operations the web UI can ask for."""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, pdfprep, printers
from .creds import PasswordStore
from .ssh import (
    REMOTE_PATH,
    AuthRejected,
    AuthRequired,
    Connection,
    SSHError,
    Target,
    sh_command,
)

SERVERS = {
    "stu.comp.nus.edu.sg": {"label": "stu (students)", "jump": "stujump.comp.nus.edu.sg"},
    "stf.comp.nus.edu.sg": {"label": "stf (staff)", "jump": "stfjump.comp.nus.edu.sg"},
}

DEFAULTS = {
    "username": "",
    "host": "stu.comp.nus.edu.sg",
    "port": 22,
    "jump_host": "stujump.comp.nus.edu.sg",
    "jump_port": 22,
    "route": "auto",
    "remember": True,
    "printer": "psts",
    "custom_queue": "",
    "sides": "duplex",
    "banner": True,
    "nup": 1,
    "fast_heavy": True,
}

SIGN_IN_PROMPT = "Sign in with your SoC account to print"

# Remote scripts run under `sh -c` on the login server; "$1", "$2" are arguments.
# The file arrives on stdin, is spooled with the documented `lpr -P queue file`
# and removed again (lpr has handed it to the print server by the time it returns).
PRINT_SCRIPT = (
    'umask 077; d=$(mktemp -d "${TMPDIR:-/tmp}/socprint.XXXXXX") || exit 97; '
    'f="$d/$2"; cat > "$f" && lpr -P "$1" "$f"; rc=$?; rm -rf "$d"; exit $rc'
)
QUEUE_SCRIPT = 'lpq -P "$1"'
CANCEL_SCRIPT = 'lprm -P "$1" "$2"'
QUOTA_SCRIPT = "pusage"

MAX_UPLOAD = 1024**3  # the print servers cap a job at 1 GB


class UserError(Exception):
    """A request the user can fix (bad page range, unknown file, …)."""


@dataclass
class Paths:
    config: Path
    cache: Path
    runtime: Path

    @classmethod
    def default(cls) -> Paths:
        home = Path.home()
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "soc-print"
        cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "soc-print"
        runtime_root = os.environ.get("XDG_RUNTIME_DIR")
        runtime = Path(runtime_root) / "soc-print" if runtime_root else cache / "run"
        return cls(config, cache, runtime)


@dataclass
class FileEntry:
    id: str
    name: str
    size: int
    workdir: Path
    kind: str = ""
    pdf: Path | None = None
    pages: int = 0
    error: str = ""
    sent: dict | None = None
    preview_id: str = ""
    costs: list = field(default_factory=list)  # pdfprep.PageCost per page
    prepared: dict = field(default_factory=dict)  # layout key -> (sheets pdf, info)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def heavy_pages(self) -> int:
        return sum(cost.heavy for cost in self.costs)

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "kindLabel": pdfprep.KIND_LABELS.get(self.kind, ""),
            "pages": self.pages,
            "error": self.error,
            "sent": self.sent,
            "heavyPages": self.heavy_pages,
        }


def guess_username(hosts: list[str]) -> str:
    """Pre-fill the SoC username from ~/.ssh/config, if it names one for these hosts."""
    local = getpass.getuser()
    for host in hosts:
        try:
            out = subprocess.run(["ssh", "-G", host], capture_output=True, text=True,
                                 timeout=5, stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"^user (\S+)$", out, re.MULTILINE)
        if m and m[1] != local:
            return m[1]
    return ""


def safe_remote_name(name: str) -> str:
    """A job name for lpr that needs no shell quoting and reads well on the banner page."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._-")[:80]
    return (stem or "document") + ".pdf"


def parse_lpq(text: str, username: str) -> list[dict]:
    """Pull job rows out of `lpq` output (works for the LPRng, BSD and CUPS layouts)."""
    jobs: list[dict] = []
    job_col = owner_col = None
    for line in text.splitlines():
        cells = line.split()
        if "Rank" in cells and "Job" in cells:
            job_col = cells.index("Job")
            owner_col = next((i for i, c in enumerate(cells) if c.startswith("Owner")), 1)
            continue
        if job_col is None or len(cells) <= job_col or not cells[job_col].isdigit():
            continue
        owner = cells[owner_col]
        jobs.append({
            "rank": cells[0],
            "owner": owner,
            "job": cells[job_col],
            "detail": " ".join(cells[job_col + 1:]),
            "mine": re.split(r"[@+]", owner)[0] == username,
        })
    return jobs


class App:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or Paths.default()
        for directory in (self.paths.config, self.paths.cache, self.paths.runtime):
            directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.config, 0o700)
        os.chmod(self.paths.runtime, 0o700)
        self.work = self.paths.cache / "work"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)
        os.chmod(self.work, 0o700)

        self.token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._auth_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._files: dict[str, FileEntry] = {}

        self.config = self._load_config()
        self.store = PasswordStore()
        self.conn = Connection(self.paths.runtime)

        if not self.config["username"]:
            self.config["username"] = guess_username(
                [self.config["jump_host"], self.config["host"]])
        self._password: str | None = self.store.get(self.config["username"])
        self._saved = self._password is not None
        self._needs_sign_in = False
        self._auth_message = ""
        self._apply_target()

    # -------------------------------------------------------------- config

    def _config_file(self) -> Path:
        return self.paths.config / "config.json"

    def _load_config(self) -> dict:
        config = dict(DEFAULTS)
        try:
            stored = json.loads(self._config_file().read_text())
        except (OSError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            config.update({k: v for k, v in stored.items() if k in DEFAULTS})
        return config

    def _save_config(self) -> None:
        tmp = self._config_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2) + "\n")
        tmp.replace(self._config_file())

    def update_settings(self, req: dict) -> None:
        """Remember the print settings last used."""
        with self._lock:
            if req.get("printer") in printers.BY_ID or req.get("printer") == "custom":
                self.config["printer"] = req["printer"]
            if isinstance(req.get("queue"), str):
                self.config["custom_queue"] = req["queue"].strip().lower()[:32]
            if req.get("sides") in ("duplex", "simplex"):
                self.config["sides"] = req["sides"]
            if isinstance(req.get("banner"), bool):
                self.config["banner"] = req["banner"]
            if req.get("nup") in pdfprep.GRIDS:
                self.config["nup"] = req["nup"]
            if isinstance(req.get("fastHeavy"), bool):
                self.config["fast_heavy"] = req["fastHeavy"]
            self._save_config()

    def _apply_target(self) -> None:
        c = self.config
        if not c["username"]:
            return
        try:
            self.conn.set_target(Target(c["username"], c["host"], int(c["port"]),
                                        c["jump_host"], int(c["jump_port"]), c["route"]))
        except ValueError:
            self.conn.target = None

    # ---------------------------------------------------------------- auth

    def state(self) -> dict:
        connected = self.conn.is_connected()
        c = self.config
        with self._lock:
            files = [f.public() for f in self._files.values()]
        servers = [{"host": host, "label": info["label"]} for host, info in SERVERS.items()]
        if c["host"] not in SERVERS:  # a host set by hand in config.json
            servers.insert(0, {"host": c["host"], "label": c["host"]})
        return {
            "version": __version__,
            "auth": {
                "username": c["username"],
                "connected": connected,
                "ready": connected or (self._password is not None and not self._needs_sign_in),
                "saved": self._saved,
                "route": self.conn.route_used if connected else "",
                "message": self._auth_message,
                "keyring": self.store.available,
                "keyringName": self.store.name,
            },
            "settings": {
                "host": c["host"], "jumpHost": c["jump_host"], "route": c["route"],
                "remember": c["remember"], "printer": c["printer"],
                "queue": c["custom_queue"], "sides": c["sides"], "banner": c["banner"],
                "nup": c["nup"], "fastHeavy": c["fast_heavy"],
            },
            "servers": servers,
            "printers": printers.catalogue(),
            "accessLabels": printers.ACCESS_LABELS,
            "tools": {"office": bool(pdfprep.find_office()),
                      "postscript": bool(pdfprep.find_ghostscript())},
            "files": files,
        }

    def sign_in(self, req: dict) -> None:
        username = str(req.get("username", "")).strip()
        password = str(req.get("password", "")) or None
        remember = bool(req.get("remember", True))
        host = req.get("host") or self.config["host"]
        route = req.get("route") or self.config["route"]
        if not username:
            raise UserError("Enter your SoC username")
        jump_host = SERVERS.get(host, {}).get("jump", self.config["jump_host"])
        try:
            target = Target(username, host, int(self.config["port"]), jump_host,
                            int(self.config["jump_port"]), route)
            target.validate()
        except ValueError as exc:
            raise UserError(str(exc)) from None

        with self._auth_lock:
            previous = self.config["username"]
            if previous and previous != username:
                self.store.delete(previous)
                self._password, self._saved = None, False
            self.config.update(username=username, host=host, jump_host=jump_host,
                               route=route, remember=remember)
            self.conn.set_target(target)
            if password:
                # Verify the password that was typed rather than an older open session.
                self.conn.disconnect()
            try:
                self.conn.connect(password or self._password)
            except AuthRejected as exc:
                self._forget_password()
                self._needs_sign_in, self._auth_message = True, str(exc)
                raise
            except AuthRequired:
                self._needs_sign_in = True
                self._auth_message = "Enter your SoC password to sign in"
                raise AuthRequired(self._auth_message) from None
            self._needs_sign_in, self._auth_message = False, ""
            if password:
                self._password = password
            if remember and self._password:
                self._saved = self.store.set(username, self._password)
            elif not remember:
                self.store.delete(username)
                self._saved = False
            self._save_config()

    def sign_out(self) -> None:
        with self._auth_lock:
            self.conn.disconnect()
            self._forget_password()
            self._needs_sign_in, self._auth_message = True, ""

    def _forget_password(self) -> None:
        self.store.delete(self.config["username"])
        self._password, self._saved = None, False

    def _ensure_connected(self) -> None:
        if self.conn.is_connected():
            return
        with self._auth_lock:
            if self.conn.is_connected():
                return
            if self._needs_sign_in or not self.conn.target:
                raise AuthRequired(self._auth_message or SIGN_IN_PROMPT)
            try:
                self.conn.connect(self._password)
            except AuthRejected as exc:
                # A saved password that stopped working is dropped, never retried.
                self._forget_password()
                self._needs_sign_in = True
                self._auth_message = f"{exc}. Sign in again."
                raise AuthRejected(self._auth_message) from None
            except AuthRequired:
                self._needs_sign_in, self._auth_message = True, SIGN_IN_PROMPT
                raise

    def _remote(self, script: str, *args: str, stdin: Path | None = None,
                timeout: float = 90, retry: bool = True):
        command = sh_command(REMOTE_PATH + script, *args)
        self._ensure_connected()
        proc = self.conn.run(command, stdin=stdin, timeout=timeout)
        if proc.returncode == 255 and not self.conn.is_connected():
            if not retry:
                raise SSHError("The connection dropped while sending. Check the queue "
                               "before printing again.")
            self._ensure_connected()
            proc = self.conn.run(command, stdin=stdin, timeout=timeout)
        return proc

    # --------------------------------------------------------------- files

    def new_file(self, name: str, size: int) -> FileEntry:
        if size > MAX_UPLOAD:
            raise UserError("That file is over the 1 GB limit for a print job")
        name = Path(name.replace("\\", "/")).name[:200] or "document"
        entry = FileEntry(uuid.uuid4().hex[:12], name, size, self.work)
        entry.workdir = self.work / entry.id
        entry.workdir.mkdir()
        with self._lock:
            self._files[entry.id] = entry
        return entry

    def ingest(self, entry: FileEntry) -> dict:
        """Convert a freshly uploaded file; failed files are dropped again."""
        upload = entry.workdir / "upload"
        try:
            entry.pdf, entry.kind = pdfprep.to_pdf(
                upload, entry.name, entry.workdir, self.paths.cache / "lo-profile")
            entry.pages = pdfprep.count_pages(entry.pdf)
            try:
                entry.costs = pdfprep.page_costs(entry.pdf)
            except Exception:  # analysis is only an optimisation; print the file regardless
                entry.costs = []
        except pdfprep.ConversionError as exc:
            entry.error = str(exc)
        except Exception as exc:  # an unexpected converter failure should not kill the upload
            entry.error = f"Couldn't process this file ({type(exc).__name__})"
        result = entry.public()
        if entry.error:
            self.remove_file(entry.id)
        return result

    def remove_file(self, file_id: str) -> None:
        with self._lock:
            entry = self._files.pop(file_id, None)
        if entry:
            shutil.rmtree(entry.workdir, ignore_errors=True)

    def reactivate(self, file_id: str) -> None:
        self._entry(file_id).sent = None

    def _entry(self, file_id) -> FileEntry:
        with self._lock:
            entry = self._files.get(str(file_id))
        if entry is None or entry.pdf is None:
            raise UserError("That file is no longer in the list. Add it again.")
        return entry

    # ------------------------------------------------------------ printing

    def _destination(self, req: dict) -> printers.Destination:
        try:
            if req.get("printer") == "custom":
                return printers.resolve_custom(str(req.get("queue", "")))
            return printers.resolve(str(req.get("printer", "")), str(req.get("sides", "")),
                                    bool(req.get("banner", True)))
        except ValueError as exc:
            raise UserError(str(exc)) from None

    @staticmethod
    def _fast(req: dict) -> bool:
        return bool(req.get("fastHeavy", True))

    @staticmethod
    def _layout_options(req: dict) -> tuple[int, int]:
        try:
            nup, copies = int(req.get("nup", 1)), int(req.get("copies", 1))
        except (TypeError, ValueError):
            raise UserError("Invalid print settings") from None
        return nup, copies

    def plan(self, req: dict) -> dict:
        """Work out queue, paper use and quota for the UI, without printing."""
        try:
            dest = self._destination(req)
            nup, copies = self._layout_options(req)
        except UserError as exc:
            return {"ok": False, "error": str(exc), "files": {}}

        files: dict[str, dict] = {}
        totals = {"files": 0, "pages": 0, "sides": 0, "sheets": 0, "quota": 0, "heavy": 0}
        ok = True
        for item in req.get("files", []):
            try:
                entry = self._entry(item.get("id"))
                p = pdfprep.plan(entry.pages, str(item.get("pages", "")), nup=nup,
                                 copies=copies, duplex=dest.duplex)
            except (UserError, ValueError) as exc:
                files[str(item.get("id"))] = {"error": str(exc)}
                ok = False
                continue
            heavy = self._heavy_count(entry, str(item.get("pages", "")), nup)
            files[entry.id] = {"pages": p.pages, "sides": p.total_sides, "sheets": p.sheets,
                               "heavy": heavy}
            totals["heavy"] += heavy
            totals["files"] += 1
            totals["pages"] += p.pages * copies
            totals["sides"] += p.total_sides
            totals["sheets"] += p.sheets
            totals["quota"] += pdfprep.quota_estimate(
                p.total_sides, duplex=dest.duplex, kind=dest.kind)
        return {
            "ok": ok, "error": "", "queue": dest.queue, "location": dest.label,
            "duplex": dest.duplex, "kind": dest.kind, "paper": dest.paper,
            "fastHeavy": self._fast(req), "canSpeedUp": bool(pdfprep.find_ghostscript()),
            "files": files, "totals": totals,
        }

    @staticmethod
    def _heavy_count(entry: FileEntry, pages: str, nup: int) -> int:
        """Heavy sheets (one copy) this selection makes; 0 if the file wasn't analysed."""
        if not entry.costs:
            return 0
        sheets = pdfprep.sheets_for(pages, entry.pages, nup)
        return len(pdfprep.heavy_sheets(entry.costs, sheets))

    def _prepare(self, entry: FileEntry, pages: str, *, nup: int,
                  dest: printers.Destination, fast: bool) -> tuple[Path, dict]:
        """One copy of the sheets, with heavy sheets turned into images if asked.

        Cached per layout, so Preview and Print share the work.
        """
        margin = pdfprep.IMAGE_MARGIN if entry.kind == "image" else 0.0
        key = (pages.strip(), nup, dest.paper, margin, fast, dest.kind if fast else "")
        with entry.lock:
            cached = entry.prepared.get(key)
            if cached and cached[0].exists():
                return cached
            tag = uuid.uuid4().hex[:8]
            sheets_pdf = entry.workdir / f"sheets-{tag}.pdf"
            try:
                sheets = pdfprep.compose(entry.pdf, sheets_pdf, pages=pages, nup=nup,
                                         paper=dest.paper, margin=margin)
            except ValueError as exc:
                raise UserError(str(exc)) from None
            info = {"rasterized": 0, "note": ""}
            heavy = pdfprep.heavy_sheets(entry.costs, sheets) if fast and entry.costs else []
            if heavy:
                fast_pdf = entry.workdir / f"fast-{tag}.pdf"
                try:
                    pdfprep.rasterize_sheets(sheets_pdf, fast_pdf, heavy,
                                             colour=dest.kind == "colour")
                    sheets_pdf.unlink(missing_ok=True)
                    sheets_pdf = fast_pdf
                    info["rasterized"] = len(heavy)
                except pdfprep.ConversionError as exc:
                    info["note"] = f"Heavy pages were sent unchanged: {exc}"
            # Keep only the two most recent layouts of a file.
            while len(entry.prepared) >= 2:
                old_key = next(iter(entry.prepared))
                entry.prepared.pop(old_key)[0].unlink(missing_ok=True)
            entry.prepared[key] = (sheets_pdf, info)
            return sheets_pdf, info

    def _render(self, entry: FileEntry, out_name: str, pages: str, *, nup: int, copies: int,
                dest: printers.Destination, fast: bool) -> tuple[Path, pdfprep.Plan, dict]:
        try:
            result = pdfprep.plan(entry.pages, pages, nup=nup, copies=copies, duplex=dest.duplex)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        sheets_pdf, info = self._prepare(entry, pages, nup=nup, dest=dest, fast=fast)
        out = entry.workdir / out_name
        pdfprep.repeat_copies(sheets_pdf, out, copies=copies, duplex=dest.duplex)
        return out, result, info

    def preview(self, req: dict) -> dict:
        """Build the exact PDF that would be sent for one file (one copy).

        Returns a one-off id under which the browser can fetch it.
        """
        dest = self._destination(req)
        nup, _ = self._layout_options(req)
        entry = self._entry(req.get("id"))
        _, _, info = self._render(entry, "preview.pdf", str(req.get("pages", "")),
                                  nup=nup, copies=1, dest=dest, fast=self._fast(req))
        entry.preview_id = secrets.token_urlsafe(24)
        return {"id": entry.preview_id, **info}

    def preview_file(self, preview_id: str) -> tuple[Path, str]:
        with self._lock:
            entry = next((f for f in self._files.values() if f.preview_id == preview_id), None)
        if entry is None:
            raise UserError("That preview has expired. Open it again from SoC Print.")
        return entry.workdir / "preview.pdf", safe_remote_name(entry.name)

    def print(self, req: dict) -> dict:
        dest = self._destination(req)
        nup, copies = self._layout_options(req)
        items = req.get("files") or []
        if not items:
            raise UserError("Add a file to print first")
        # Validate everything before sending anything.
        jobs = []
        for item in items:
            entry = self._entry(item.get("id"))
            pages = str(item.get("pages", ""))
            try:
                pdfprep.plan(entry.pages, pages, nup=nup, copies=copies, duplex=dest.duplex)
            except ValueError as exc:
                raise UserError(f"{entry.name}: {exc}") from None
            jobs.append((entry, pages))

        results, failure = [], None
        with self._print_lock:
            for entry, pages in jobs:
                try:
                    out, laid, info = self._render(entry, "print.pdf", pages, nup=nup,
                                                   copies=copies, dest=dest,
                                                   fast=self._fast(req))
                    size = out.stat().st_size
                except Exception as exc:  # one unreadable PDF must not stop the other files
                    results.append({"id": entry.id, "ok": False,
                                    "message": f"Couldn't prepare this file ({exc})"})
                    continue
                try:
                    proc = self._remote(PRINT_SCRIPT, dest.queue, safe_remote_name(entry.name),
                                        stdin=out, timeout=900, retry=False)
                except SSHError as exc:
                    failure = {"kind": exc.kind, "message": str(exc)}
                    break
                finally:
                    out.unlink(missing_ok=True)
                output = (proc.stdout + proc.stderr).strip()
                if proc.returncode == 0:
                    entry.sent = {"queue": dest.queue, "location": dest.label,
                                  "sheets": laid.sheets, "at": time.time(), "bytes": size,
                                  "rasterized": info["rasterized"]}
                    results.append({"id": entry.id, "ok": True, "message": output,
                                    "rasterized": info["rasterized"], "note": info["note"]})
                else:
                    results.append({
                        "id": entry.id, "ok": False,
                        "message": output or f"lpr exited with status {proc.returncode}",
                    })
        return {"queue": dest.queue, "results": results, "error": failure}

    # ---------------------------------------------------- queue and quota

    def jobs(self, queue: str) -> dict:
        if not printers.QUEUE_RE.fullmatch(queue):
            raise UserError("Invalid queue name")
        proc = self._remote(QUEUE_SCRIPT, queue)
        text = (proc.stdout + proc.stderr).strip()
        return {"queue": queue, "ok": proc.returncode == 0, "raw": text,
                "jobs": parse_lpq(proc.stdout, self.config["username"])}

    def cancel(self, queue: str, job: str) -> dict:
        if not printers.QUEUE_RE.fullmatch(queue) or not re.fullmatch(r"\d{1,9}", job):
            raise UserError("Invalid job")
        proc = self._remote(CANCEL_SCRIPT, queue, job, retry=False)
        return {"ok": proc.returncode == 0, "message": (proc.stdout + proc.stderr).strip()}

    def quota(self) -> dict:
        proc = self._remote(QUOTA_SCRIPT)
        if proc.returncode == 127:
            return {"ok": False, "text": "The pusage command isn't available on this server."}
        return {"ok": proc.returncode == 0, "text": (proc.stdout + proc.stderr).strip()}

    # ------------------------------------------------------------ shutdown

    def close(self) -> None:
        self.conn.close()
        shutil.rmtree(self.work, ignore_errors=True)

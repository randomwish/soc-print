"""The local web server: static UI plus a small JSON API.

It listens on 127.0.0.1 only. Because any web page you visit can try to send
requests to localhost, every API call must carry a token that only our own page
knows, and the Host header must be ours (which defeats DNS rebinding).
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .app import MAX_UPLOAD, App, UserError
from .ssh import AuthRejected, AuthRequired, NetworkError, SSHError

STATIC = Path(__file__).with_name("static")
STATIC_TYPES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TOKEN_HEADER = "X-SOC-Print-Token"


class Handler(BaseHTTPRequestHandler):
    server_version = f"soc-print/{__version__}"
    app: App  # set by make_server
    allowed_hosts: set[str]
    verbose = False

    # ----------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _error(self, status: int, kind: str, message: str):
        self._json({"error": {"kind": kind, "message": message}}, status)

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise UserError("Request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise UserError("Malformed request") from None
        if not isinstance(data, dict):
            raise UserError("Malformed request")
        return data

    def _authorised(self) -> bool:
        return self.headers.get(TOKEN_HEADER, "") == self.app.token

    # ------------------------------------------------------------ routing

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if self.headers.get("Host", "") not in self.allowed_hosts:
                return self._error(HTTPStatus.FORBIDDEN, "forbidden", "Unexpected Host header")
            if method == "GET" and path == "/":
                return self._index()
            if method == "GET" and path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if method == "GET" and path == "/api/ping":
                return self._json({"app": "soc-print", "version": __version__})
            if method == "GET" and (m := re.fullmatch(r"/preview/([\w-]{20,})/[\w.-]+", path)):
                # The unguessable id is the capability: a new tab can't send our token header.
                # The trailing file name only gives the tab a readable title.
                pdf, name = self.app.preview_file(m[1])
                return self._send(200, pdf.read_bytes(), "application/pdf",
                                  {"Content-Disposition": f'inline; filename="{name}"'})
            if not path.startswith("/api/"):
                return self._error(HTTPStatus.NOT_FOUND, "not-found", "Not found")
            if not self._authorised():
                return self._error(HTTPStatus.FORBIDDEN, "forbidden",
                                   "This page is out of date. Reload it.")
            self._api(method, path, query)
        except UserError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid", str(exc))
        except (AuthRequired, AuthRejected) as exc:
            self._error(HTTPStatus.UNAUTHORIZED, exc.kind, str(exc))
        except NetworkError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, exc.kind, str(exc))
        except SSHError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, exc.kind, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal",
                        "Something went wrong inside SoC Print. See its log for details.")

    def _api(self, method: str, path: str, query: dict):
        app = self.app
        route = (method, path)
        if route == ("GET", "/api/state"):
            return self._json(app.state())
        if route == ("POST", "/api/signin"):
            app.sign_in(self._body_json())
            return self._json(app.state())
        if route == ("POST", "/api/signout"):
            app.sign_out()
            return self._json(app.state())
        if route == ("POST", "/api/settings"):
            app.update_settings(self._body_json())
            return self._json({"ok": True})
        if route == ("POST", "/api/files"):
            return self._upload(query)
        if route == ("POST", "/api/plan"):
            return self._json(app.plan(self._body_json()))
        if route == ("POST", "/api/preview"):
            info = app.preview(self._body_json())
            name = app.preview_file(info["id"])[1]
            return self._json({"url": f"/preview/{info['id']}/{name}",
                               "rasterized": info["rasterized"], "note": info["note"]})
        if route == ("POST", "/api/print"):
            return self._json(app.print(self._body_json()))
        if route == ("GET", "/api/quota"):
            return self._json(app.quota())
        if route == ("GET", "/api/jobs"):
            return self._json(app.jobs((query.get("queue") or [""])[0]))
        if route == ("POST", "/api/jobs/cancel"):
            body = self._body_json()
            return self._json(app.cancel(str(body.get("queue", "")), str(body.get("job", ""))))
        if m := re.fullmatch(r"/api/files/([0-9a-f]{12})", path):
            if method == "DELETE":
                app.remove_file(m[1])
                return self._json({"ok": True})
        if m := re.fullmatch(r"/api/files/([0-9a-f]{12})/reactivate", path):
            if method == "POST":
                app.reactivate(m[1])
                return self._json({"ok": True})
        self._error(HTTPStatus.NOT_FOUND, "not-found", "Not found")

    # ----------------------------------------------------------- handlers

    def _index(self):
        html = (STATIC / "index.html").read_text().replace("__TOKEN__", self.app.token)
        self._send(200, html.encode(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": CSP})

    def _static(self, name: str):
        if name not in STATIC_TYPES:
            return self._error(HTTPStatus.NOT_FOUND, "not-found", "Not found")
        self._send(200, (STATIC / name).read_bytes(), STATIC_TYPES[name])

    def _upload(self, query: dict):
        name = (query.get("name") or ["document"])[0]
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise UserError("Upload is missing its size") from None
        if length <= 0:
            raise UserError(f"{name} is empty")
        if length > MAX_UPLOAD:
            raise UserError("That file is over the 1 GB limit for a print job")
        entry = self.app.new_file(name, length)
        try:
            with (entry.workdir / "upload").open("wb") as out:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise UserError("Upload was interrupted")
                    out.write(chunk)
                    remaining -= len(chunk)
        except BaseException:
            self.app.remove_file(entry.id)
            raise
        self._json(self.app.ingest(entry))


def make_server(app: App, port: int, *, verbose: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "app": app,
        "verbose": verbose,
        "allowed_hosts": {f"127.0.0.1:{port}", f"localhost:{port}"},
    })
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


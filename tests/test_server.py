"""The HTTP layer: token and Host checks, uploads, previews."""

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from socprint.server import TOKEN_HEADER, make_server
from tests.conftest import free_port, make_pdf


@pytest.fixture
def site(make_app):
    app = make_app()
    port = free_port()
    server = make_server(app, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None, *, token=app.token, host=None, raw=False):
        headers = {}
        if token:
            headers[TOKEN_HEADER] = token
        if host:
            headers["Host"] = host
        data = body
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                return resp.status, payload if raw else json.loads(payload), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read()), err.headers

    call.app = app
    yield call
    server.shutdown()
    server.server_close()


def test_page_carries_the_token_and_a_strict_csp(site):
    status, html, headers = site("GET", "/", token=None, raw=True)
    assert status == 200
    token = re.search(rb'name="soc-print-token" content="([^"]+)"', html)[1].decode()
    assert token == site.app.token
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert b"__TOKEN__" not in html

    for name in ("app.js", "style.css", "icon.svg"):
        status, body, _ = site("GET", f"/static/{name}", token=None, raw=True)
        assert status == 200 and body
    status, _, _ = site("GET", "/static/../app.py", token=None)
    assert status == 404


def test_api_needs_the_token(site):
    assert site("GET", "/api/state")[0] == 200
    assert site("GET", "/api/state", token=None)[0] == 403
    assert site("GET", "/api/state", token="guess")[0] == 403
    assert site("POST", "/api/print", {"files": []}, token=None)[0] == 403
    # The liveness probe is the only API route without it, and it reveals nothing.
    status, body, _ = site("GET", "/api/ping", token=None)
    assert status == 200 and set(body) == {"app", "version"}


def test_foreign_host_header_is_refused(site):
    # DNS rebinding: a hostile page resolves its own name to 127.0.0.1.
    status, body, _ = site("GET", "/api/state", host="evil.example:6310")
    assert status == 403 and body["error"]["kind"] == "forbidden"
    assert site("GET", "/", token=None, host="evil.example")[0] == 403


def test_state_never_contains_the_password(site, soc):
    from tests.conftest import PASSWORD, sign_in
    sign_in(site.app)
    status, state, _ = site("GET", "/api/state")
    assert status == 200 and state["auth"]["connected"]
    assert PASSWORD not in json.dumps(state)


def test_upload_plan_preview_roundtrip(site, tmp_path):
    pdf = make_pdf(tmp_path / "notes.pdf", 4, (612, 792)).read_bytes()
    status, entry, _ = site("POST", "/api/files?name=My%20notes.pdf", pdf)
    assert status == 200 and entry["pages"] == 4 and entry["name"] == "My notes.pdf"

    req = {"printer": "psts", "sides": "duplex", "banner": True, "nup": 2, "copies": 1,
           "files": [{"id": entry["id"], "pages": "1-3"}]}
    status, plan, _ = site("POST", "/api/plan", req)
    assert status == 200 and plan["ok"] and plan["totals"]["sheets"] == 1

    status, preview, _ = site("POST", "/api/preview", {**req, "id": entry["id"], "pages": "1-3"})
    assert status == 200
    # A new browser tab can't send the token header; the one-off URL is enough.
    status, body, headers = site("GET", preview["url"], token=None, raw=True)
    assert status == 200 and body.startswith(b"%PDF-")
    assert headers["Content-Type"] == "application/pdf"
    assert preview["url"].endswith("/My_notes.pdf")
    assert site("GET", "/preview/" + "x" * 32 + "/My_notes.pdf", token=None)[0] == 400

    assert site("DELETE", f"/api/files/{entry['id']}")[0] == 200
    assert site("GET", "/api/state")[1]["files"] == []
    assert site("GET", preview["url"], token=None)[0] == 400


def test_bad_requests_get_clear_errors(site):
    status, body, _ = site("POST", "/api/files?name=empty.pdf", b"")
    assert status == 400 and "empty" in body["error"]["message"]

    status, body, _ = site("POST", "/api/files?name=tool.bin", b"\x7fELF\0\0binary")
    assert status == 200 and "can't be printed" in body["error"]

    status, body, _ = site("POST", "/api/print", {"printer": "psts", "sides": "duplex", "files": []})
    assert status == 400 and body["error"]["kind"] == "invalid"

    status, body, _ = site("GET", "/api/quota")
    assert status == 401 and body["error"]["kind"] == "auth"

    assert site("GET", "/api/nope")[0] == 404
    assert site("POST", "/api/plan", b"{not json")[0] == 400

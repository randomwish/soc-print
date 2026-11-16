"""End-to-end behaviour against the fake SoC login server, through the real ssh client."""

import pikepdf
import pytest

from socprint.app import UserError, parse_lpq, safe_remote_name
from socprint.creds import SERVICE
from socprint.ssh import AuthRejected, AuthRequired, NetworkError, sh_command
from tests.conftest import PASSWORD, USER, add_file, make_pdf, sign_in


def job_request(file, **overrides):
    req = {"printer": "psts", "sides": "duplex", "banner": True, "nup": 1, "copies": 1,
           "files": [{"id": file["id"], "pages": ""}]}
    req.update(overrides)
    return req


def passwords(soc):
    return [event["ok"] for event in soc.events("password")]


# ---------------------------------------------------------------- signing in


def test_wrong_password_is_tried_exactly_once_and_not_saved(make_app, soc):
    app = make_app()
    with pytest.raises(AuthRejected):
        sign_in(app, "not the password")
    assert passwords(soc) == [False]
    assert app.store.get(USER) is None
    state = app.state()["auth"]
    assert not state["connected"] and not state["ready"] and not state["saved"]


def test_sign_in_without_a_password_makes_no_password_attempt(make_app, soc):
    app = make_app()
    with pytest.raises(AuthRequired):
        sign_in(app, "")
    assert passwords(soc) == []


def test_sign_in_connects_and_saves_to_the_keyring(make_app, soc):
    app = make_app()
    sign_in(app)
    state = app.state()["auth"]
    assert state["connected"] and state["ready"] and state["saved"]
    assert state["route"] == "direct"
    assert app.store.get(USER) == PASSWORD
    assert passwords(soc) == [True]

    # Everything after the first login rides the same connection.
    app.quota()
    app.jobs("psts")
    assert passwords(soc) == [True]


def test_remember_off_keeps_the_password_out_of_the_keyring(make_app):
    app = make_app()
    sign_in(app, remember=False)
    assert app.state()["auth"]["connected"]
    assert not app.state()["auth"]["saved"]
    assert app.store.get(USER) is None


def test_password_never_reaches_disk_or_process_arguments(make_app, env, soc):
    app = make_app()
    sign_in(app, remember=False)
    leaked = [p for p in env.rglob("*") if p.is_file() and PASSWORD.encode() in p.read_bytes()]
    assert leaked == []
    assert all(PASSWORD not in event["command"] for event in soc.events("exec"))


def test_saved_login_survives_a_restart(make_app, soc):
    first = make_app()
    sign_in(first)
    first.close()  # drops the connection, as quitting the app does

    second = make_app()
    state = second.state()["auth"]
    assert state["ready"] and state["saved"] and not state["connected"]
    assert "47 pages left" in second.quota()["text"]  # reconnects by itself
    assert second.state()["auth"]["connected"]
    assert passwords(soc) == [True, True]


def test_stale_saved_password_is_dropped_not_retried(make_app, soc):
    first = make_app()
    sign_in(first)
    first.close()
    first.store.set(USER, "changed on the server since")

    second = make_app()
    with pytest.raises(AuthRejected):
        second.quota()
    assert second.store.get(USER) is None
    with pytest.raises(AuthRequired):
        second.quota()
    with pytest.raises(AuthRequired):
        second.jobs("psts")
    assert passwords(soc) == [True, False]  # one failed attempt, then it stops


def test_sign_out_disconnects_and_forgets(make_app, soc):
    app = make_app()
    sign_in(app)
    app.sign_out()
    state = app.state()["auth"]
    assert not state["connected"] and not state["ready"] and not state["saved"]
    assert app.store.get(USER) is None
    with pytest.raises(AuthRequired):
        app.quota()
    assert passwords(soc) == [True]


def test_jump_host_route_authenticates_both_hops(make_app, soc):
    app = make_app()  # the fake server doubles as its own jump host ("localhost")
    sign_in(app, route="jump")
    assert app.state()["auth"]["route"] == "jump"
    assert passwords(soc) == [True, True]
    assert soc.events("forward")[0]["dest"] == f"127.0.0.1:{soc.port}"
    assert "47 pages left" in app.quota()["text"]


def test_unreachable_server_is_a_network_error(make_app):
    from tests.conftest import free_port
    app = make_app()
    app.config["port"] = free_port()  # nothing listens here
    with pytest.raises(NetworkError):
        sign_in(app)


def test_usernames_that_could_be_ssh_options_are_refused(make_app):
    app = make_app()
    for bad in ("-oProxyCommand=id", "a b", "x;y", ""):
        with pytest.raises(UserError):
            app.sign_in({"username": bad, "password": "x", "host": "127.0.0.1"})


# ------------------------------------------------------------------ printing


def test_print_sends_the_laid_out_pdf_to_the_queue(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    file = add_file(app, make_pdf(tmp_path / "Lecture 5 (final).pdf", 3, (612, 792)))
    assert file["pages"] == 3 and not file["error"]

    result = app.print(job_request(file, sides="simplex", copies=2))
    assert result["error"] is None
    assert result["results"] == [
        {"id": file["id"], "ok": True, "message": "", "rasterized": 0, "note": ""}]

    (job,) = soc.jobs()
    assert job.name == "100__psts-sx__Lecture_5_final.pdf"
    with pikepdf.Pdf.open(job) as pdf:
        assert len(pdf.pages) == 6
        assert [round(float(v)) for v in pdf.pages[0].mediabox] == [0, 0, 595, 842]

    sent = app.state()["files"][0]["sent"]
    assert sent["queue"] == "psts-sx" and sent["sheets"] == 6


def test_print_leaves_nothing_behind_on_the_server(make_app, soc, tmp_path):
    import glob
    import tempfile
    app = make_app()
    sign_in(app)
    before = set(glob.glob(f"{tempfile.gettempdir()}/socprint.*"))
    app.print(job_request(add_file(app, make_pdf(tmp_path / "a.pdf", 1))))
    assert set(glob.glob(f"{tempfile.gettempdir()}/socprint.*")) == before


def test_several_files_and_page_ranges(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    a = add_file(app, make_pdf(tmp_path / "a.pdf", 10))
    b = add_file(app, make_pdf(tmp_path / "b.pdf", 4))
    req = {"printer": "psts", "sides": "duplex", "banner": False, "nup": 2, "copies": 1,
           "files": [{"id": a["id"], "pages": "1-6"}, {"id": b["id"], "pages": ""}]}

    plan = app.plan(req)
    assert plan["ok"] and plan["queue"] == "psts-nb"
    assert plan["totals"] == {"files": 2, "pages": 10, "sides": 5, "sheets": 3, "quota": 4,
                              "heavy": 0}

    result = app.print(req)
    assert [r["ok"] for r in result["results"]] == [True, True]
    names = [job.name for job in soc.jobs()]
    assert names == ["100__psts-nb__a.pdf", "101__psts-nb__b.pdf"]
    with pikepdf.Pdf.open(soc.jobs()[0]) as pdf:
        assert len(pdf.pages) == 3


def test_rejected_job_is_reported_per_file(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    file = add_file(app, make_pdf(tmp_path / "a.pdf", 1))
    result = app.print(job_request(file, printer="custom", queue="nosuch1"))
    (row,) = result["results"]
    assert not row["ok"] and "unknown printer" in row["message"]
    assert app.state()["files"][0]["sent"] is None
    assert soc.jobs() == []


def test_bad_page_range_stops_before_anything_is_sent(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    good = add_file(app, make_pdf(tmp_path / "good.pdf", 2))
    bad = add_file(app, make_pdf(tmp_path / "bad.pdf", 2))
    req = job_request(good)
    req["files"].append({"id": bad["id"], "pages": "5-9"})
    plan = app.plan(req)
    assert not plan["ok"] and "outside" in plan["files"][bad["id"]]["error"]
    with pytest.raises(UserError, match="bad.pdf"):
        app.print(req)
    assert soc.jobs() == []


def test_printing_while_signed_out_asks_for_sign_in(make_app, soc, tmp_path):
    app = make_app()
    file = add_file(app, make_pdf(tmp_path / "a.pdf", 1))
    result = app.print(job_request(file))
    assert result["results"] == [] and result["error"]["kind"] == "auth"
    assert passwords(soc) == []


def test_unprintable_upload_is_reported_and_dropped(make_app, tmp_path):
    app = make_app()
    blob = tmp_path / "tool.bin"
    blob.write_bytes(b"\x7fELF\0\0\0binary")
    result = add_file(app, blob)
    assert "can't be printed" in result["error"]
    assert app.state()["files"] == []
    assert list(app.work.iterdir()) == []


def test_preview_is_the_pdf_that_would_be_sent(make_app, tmp_path):
    app = make_app()
    file = add_file(app, make_pdf(tmp_path / "deck.pdf", 5, (960, 540)))
    info = app.preview({"printer": "psts", "sides": "duplex", "banner": True, "nup": 4,
                        "id": file["id"], "pages": ""})
    assert info["rasterized"] == 0
    path, name = app.preview_file(info["id"])
    assert name == "deck.pdf"
    with pikepdf.Pdf.open(path) as pdf:
        assert len(pdf.pages) == 2
    with pytest.raises(UserError):
        app.preview_file("x" * 32)


# ------------------------------------------------------------ queue and quota


def test_queue_listing_and_cancel(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    app.print(job_request(add_file(app, make_pdf(tmp_path / "a.pdf", 1))))
    listing = app.jobs("psts")
    assert listing["ok"]
    (job,) = listing["jobs"]
    assert job["job"] == "100" and job["mine"] and job["rank"] == "active"

    assert app.cancel("psts", "100")["ok"]
    assert app.jobs("psts")["jobs"] == []
    assert not app.cancel("psts", "100")["ok"]


def test_queue_and_job_arguments_are_validated(make_app):
    app = make_app()
    for queue, job in (("psts; id", "1"), ("psts", "1; id"), ("-Pfoo", "1"), ("psts", "")):
        with pytest.raises(UserError):
            app.cancel(queue, job)
    with pytest.raises(UserError):
        app.jobs("$(id)")


def test_settings_persist_between_runs(make_app):
    first = make_app()
    first.update_settings({"printer": "psc008", "sides": "simplex", "banner": False, "nup": 4})
    first.update_settings({"printer": "no-such-printer", "nup": 3})  # ignored
    second = make_app()
    settings = second.state()["settings"]
    assert (settings["printer"], settings["sides"], settings["banner"], settings["nup"]) == \
        ("psc008", "simplex", False, 4)


# ------------------------------------------------------------------- helpers


def test_lpq_parsing_handles_lprng_and_bsd_layouts():
    lprng = """Printer: psts@print1 'COM1 L1'
 Queue: 2 printable jobs
 Server: pid 4242 active
 Rank   Owner/ID                  Class Job Files                 Size Time
active  wyenzhe@stu1+144            A   144 report.pdf           73012 20:01:07
2       someone@stu2+145            A   145 notes.pdf            11000 20:01:30
"""
    jobs = parse_lpq(lprng, "wyenzhe")
    assert [(j["job"], j["rank"], j["mine"]) for j in jobs] == [("144", "active", True), ("145", "2", False)]
    assert jobs[0]["detail"].startswith("report.pdf")

    bsd = """psts is ready and printing
Rank    Owner   Job     File(s)                         Total Size
active  wyenzhe 12      report.pdf                      73012 bytes
1st     other   13      (stdin)                         1024 bytes
"""
    jobs = parse_lpq(bsd, "wyenzhe")
    assert [(j["job"], j["mine"]) for j in jobs] == [("12", True), ("13", False)]
    assert parse_lpq("no entries\n", "wyenzhe") == []


@pytest.mark.parametrize("name, expected", [
    ("Lecture 5 (final).pptx", "Lecture_5_final.pdf"),
    ("report.pdf", "report.pdf"),
    ("../../etc/passwd", "passwd.pdf"),
    ("-rf.pdf", "rf.pdf"),
    ("讲义.pdf", "document.pdf"),
    ("a'b\"c$(id).txt", "a_b_c_id.pdf"),
])
def test_remote_names_need_no_quoting(name, expected):
    assert safe_remote_name(name) == expected


def test_remote_command_quoting():
    command = sh_command('lpq -P "$1"', "psts")
    assert command == """sh -c 'lpq -P "$1"' sh psts"""
    assert SERVICE == "soc-print"

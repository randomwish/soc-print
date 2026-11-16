"""Heavy-page detection and the 'send as image' treatment."""

import shutil
import subprocess

import pikepdf
import pytest

from socprint import pdfprep
from tests.conftest import add_file, sign_in
from tests.heavy import build

pytestmark = pytest.mark.skipif(not pdfprep.find_ghostscript(), reason="Ghostscript not installed")


@pytest.mark.parametrize("kind, heavy", [
    ("text", False),
    ("group-only", False),
    ("plain2", False),     # one ordinary screenshot-sized image
    ("photo7", False),
    ("photo12", True),     # a 12 MP photo
    ("blend2", True),      # a 2 MP image drawn semi-transparently
    ("softmask", True),    # glow / fade effects
    ("form12", True),      # the photo hidden inside a form XObject still counts
])
def test_page_classification(tmp_path, kind, heavy):
    (cost,) = pdfprep.page_costs(build(tmp_path / "p.pdf", [kind]))
    assert cost.heavy is heavy


def test_sheet_heaviness_adds_up_across_pages(tmp_path):
    costs = pdfprep.page_costs(build(tmp_path / "p.pdf", ["photo7", "photo7", "text"]))
    assert pdfprep.heavy_sheets(costs, pdfprep.sheets_for("", 3, 1)) == []
    assert pdfprep.heavy_sheets(costs, pdfprep.sheets_for("", 3, 2)) == [0]  # 14 MP together
    assert pdfprep.sheets_for("3,1", 3, 2) == [[2, 0]]


def _poppler(tool):
    if not shutil.which(tool):
        pytest.skip(f"{tool} (poppler) not installed")


def images_on(pdf_path):
    _poppler("pdfimages")
    out = subprocess.run(["pdfimages", "-list", str(pdf_path)], capture_output=True, text=True,
                         check=True).stdout.splitlines()[2:]
    return [line.split() for line in out]


def text_of(pdf_path, page):
    _poppler("pdftotext")
    return subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), str(pdf_path), "-"],
                          capture_output=True, text=True, check=True).stdout


def test_only_heavy_sheets_become_images(tmp_path):
    src = build(tmp_path / "deck.pdf", ["text", "photo12", "text", "softmask"])
    sheets = pdfprep.compose(src, tmp_path / "one.pdf")
    heavy = pdfprep.heavy_sheets(pdfprep.page_costs(src), sheets)
    assert heavy == [1, 3]
    pdfprep.rasterize_sheets(tmp_path / "one.pdf", tmp_path / "fast.pdf", heavy, colour=False)

    with pikepdf.Pdf.open(tmp_path / "fast.pdf") as pdf:
        assert len(pdf.pages) == 4
        for page in pdf.pages:
            box = [float(v) for v in page.mediabox]
            assert abs(box[2] - 595.3) < 1 and abs(box[3] - 841.9) < 1
    assert "Page 1 text" in text_of(tmp_path / "fast.pdf", 1)  # still real text
    assert "Page 3 text" in text_of(tmp_path / "fast.pdf", 3)
    assert text_of(tmp_path / "fast.pdf", 2).strip() == ""  # now a picture

    rows = images_on(tmp_path / "fast.pdf")
    assert [(r[0], r[3], r[4], r[5], r[8]) for r in rows] == [
        ("2", "2480", "3508", "gray", "jpeg"), ("4", "2480", "3508", "gray", "jpeg")]
    assert (tmp_path / "fast.pdf").stat().st_size < (tmp_path / "one.pdf").stat().st_size
    assert pdfprep.heavy_sheets(pdfprep.page_costs(tmp_path / "fast.pdf"),
                                pdfprep.sheets_for("", 4, 1)) == []


def test_colour_queues_keep_colour(tmp_path):
    src = build(tmp_path / "deck.pdf", ["photo12"])
    pdfprep.compose(src, tmp_path / "one.pdf")
    pdfprep.rasterize_sheets(tmp_path / "one.pdf", tmp_path / "fast.pdf", [0], colour=True)
    assert images_on(tmp_path / "fast.pdf")[0][5] == "rgb"


# ------------------------------------------------------------- through the app


def req(file, **overrides):
    base = {"printer": "psts", "sides": "duplex", "banner": True, "nup": 1, "copies": 1,
            "fastHeavy": True, "files": [{"id": file["id"], "pages": ""}]}
    base.update(overrides)
    return base


def test_upload_reports_heavy_pages_and_plan_counts_sheets(make_app, tmp_path):
    app = make_app()
    file = add_file(app, build(tmp_path / "d.pdf", ["text", "photo7", "photo7", "softmask"]))
    assert file["heavyPages"] == 1
    assert app.plan(req(file))["totals"]["heavy"] == 1
    assert app.plan(req(file, nup=2))["totals"]["heavy"] == 1  # only [7 MP + soft mask]
    assert app.plan(req(file, nup=4))["totals"]["heavy"] == 1
    assert app.plan(req(file, fastHeavy=False))["totals"]["heavy"] == 1  # still reported


def test_print_sends_heavy_sheets_as_images(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    file = add_file(app, build(tmp_path / "deck.pdf", ["text", "photo12", "photo12"]))
    result = app.print(req(file, copies=2))
    (row,) = result["results"]
    assert row["ok"] and row["rasterized"] == 2 and row["note"] == "", row["note"]
    (job,) = soc.jobs()
    with pikepdf.Pdf.open(job) as pdf:
        assert len(pdf.pages) == 7  # 3 sheets, a blank back, 3 sheets
    sent = app.state()["files"][0]["sent"]
    assert sent["rasterized"] == 2 and sent["bytes"] == job.stat().st_size


def test_speed_up_can_be_turned_off(make_app, soc, tmp_path):
    app = make_app()
    sign_in(app)
    src = build(tmp_path / "deck.pdf", ["photo12"])
    file = add_file(app, src)
    (row,) = app.print(req(file, fastHeavy=False))["results"]
    assert row["rasterized"] == 0
    assert len(images_on(soc.jobs()[0])) == 1
    assert images_on(soc.jobs()[0])[0][3] == "4000"


def test_preview_and_print_share_the_work(make_app, soc, tmp_path, monkeypatch):
    app = make_app()
    sign_in(app)
    file = add_file(app, build(tmp_path / "deck.pdf", ["photo12", "text"]))
    calls = []
    real = pdfprep.rasterize_sheets
    monkeypatch.setattr(pdfprep, "rasterize_sheets",
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    info = app.preview({**req(file), "id": file["id"], "pages": ""})
    assert info["rasterized"] == 1, info.get("note")
    app.print(req(file))
    assert len(calls) == 1


def test_missing_ghostscript_still_prints(make_app, soc, tmp_path, monkeypatch):
    app = make_app()
    sign_in(app)
    file = add_file(app, build(tmp_path / "deck.pdf", ["photo12"]))
    monkeypatch.setattr(pdfprep, "find_ghostscript", lambda: None)
    (row,) = app.print(req(file))["results"]
    assert row["ok"] and row["rasterized"] == 0
    assert "Ghostscript" in row["note"]
    assert len(soc.jobs()) == 1


def test_settings_remember_the_choice(make_app):
    first = make_app()
    assert first.state()["settings"]["fastHeavy"] is True
    first.update_settings({"fastHeavy": False})
    assert make_app().state()["settings"]["fastHeavy"] is False

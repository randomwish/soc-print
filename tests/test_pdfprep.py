import shutil

import pikepdf
import pytest
from PIL import Image

from socprint import pdfprep
from tests.conftest import make_pdf

A4 = (595.276, 841.89)
LETTER = (612, 792)
SLIDE = (960, 540)


def sizes(path):
    with pikepdf.Pdf.open(path) as pdf:
        return [tuple(round(float(v)) for v in page.mediabox)[2:] for page in pdf.pages]


@pytest.mark.parametrize("spec, expected", [
    ("", [0, 1, 2, 3, 4]),
    ("all", [0, 1, 2, 3, 4]),
    ("2", [1]),
    ("1-3", [0, 1, 2]),
    ("4-", [3, 4]),
    ("-2", [0, 1]),
    ("1-2, 5", [0, 1, 4]),
    ("3,1", [2, 0]),
    ("2–3", [1, 2]),  # en dash, as pasted from documents
])
def test_page_ranges(spec, expected):
    assert pdfprep.parse_page_ranges(spec, 5) == expected


@pytest.mark.parametrize("spec", ["0", "6", "3-2", "a", "1-9", "-", "1;2", ","])
def test_page_ranges_rejected(spec):
    with pytest.raises(ValueError):
        pdfprep.parse_page_ranges(spec, 5)


def test_plan_counts():
    p = pdfprep.plan(10, "", nup=1, copies=1, duplex=True)
    assert (p.pages, p.sides, p.sheets) == (10, 10, 5)
    p = pdfprep.plan(10, "1-5", nup=2, copies=3, duplex=True)
    assert (p.pages, p.sides, p.sheets, p.total_sides) == (5, 3, 6, 9)
    p = pdfprep.plan(7, "", nup=4, copies=1, duplex=False)
    assert (p.sides, p.sheets) == (2, 2)


def test_plan_rejects_bad_options():
    with pytest.raises(ValueError):
        pdfprep.plan(3, "", nup=3, copies=1, duplex=True)
    with pytest.raises(ValueError):
        pdfprep.plan(3, "", nup=1, copies=0, duplex=True)


def test_quota_matches_the_published_examples():
    # https://dochub.comp.nus.edu.sg/cf/guides/printing/quota/start
    assert pdfprep.quota_estimate(102, duplex=True, kind="bw") == 77
    assert pdfprep.quota_estimate(119, duplex=True, kind="bw") == 89
    assert pdfprep.quota_estimate(102, duplex=False, kind="bw") == 102
    assert pdfprep.quota_estimate(10, duplex=True, kind="colour") == 10


def test_a4_pages_pass_through_untouched(tmp_path):
    src = make_pdf(tmp_path / "a4.pdf", 3)
    pdfprep.layout(src, tmp_path / "out.pdf")
    assert sizes(tmp_path / "out.pdf") == [(595, 842)] * 3


def test_other_sizes_are_scaled_onto_a4(tmp_path):
    src = make_pdf(tmp_path / "letter.pdf", 2, LETTER)
    pdfprep.layout(src, tmp_path / "out.pdf")
    assert sizes(tmp_path / "out.pdf") == [(595, 842)] * 2

    src = make_pdf(tmp_path / "slides.pdf", 2, SLIDE)
    pdfprep.layout(src, tmp_path / "out.pdf")
    assert sizes(tmp_path / "out.pdf") == [(842, 595)] * 2  # landscape pages stay landscape


def test_a3_queue_gets_a3_paper(tmp_path):
    src = make_pdf(tmp_path / "a4.pdf", 1)
    pdfprep.layout(src, tmp_path / "out.pdf", paper="A3")
    assert sizes(tmp_path / "out.pdf") == [(842, 1191)]


@pytest.mark.parametrize("page_size, nup, sheet, count", [
    (A4, 2, (842, 595), 3),     # portrait pages side by side on a landscape sheet
    (A4, 4, (595, 842), 2),
    (A4, 6, (842, 595), 1),
    (SLIDE, 2, (595, 842), 3),  # slides stacked on a portrait sheet
    (SLIDE, 4, (842, 595), 2),
    (SLIDE, 6, (842, 595), 1),  # 16:9 slides come out largest as 2 x 3 on a landscape sheet
])
def test_pages_per_sheet(tmp_path, page_size, nup, sheet, count):
    src = make_pdf(tmp_path / "in.pdf", 5, page_size)
    result = pdfprep.layout(src, tmp_path / "out.pdf", nup=nup)
    assert sizes(tmp_path / "out.pdf") == [sheet] * count
    assert result.sides == count


def test_copies_start_on_a_fresh_sheet_when_double_sided(tmp_path):
    src = make_pdf(tmp_path / "in.pdf", 3)
    result = pdfprep.layout(src, tmp_path / "out.pdf", copies=3, duplex=True)
    assert len(sizes(tmp_path / "out.pdf")) == 3 + 1 + 3 + 1 + 3
    assert result.sheets == 6

    pdfprep.layout(src, tmp_path / "out.pdf", copies=3, duplex=False)
    assert len(sizes(tmp_path / "out.pdf")) == 9

    even = make_pdf(tmp_path / "even.pdf", 4)
    pdfprep.layout(even, tmp_path / "out.pdf", copies=2, duplex=True)
    assert len(sizes(tmp_path / "out.pdf")) == 8


def test_page_selection_is_applied(tmp_path):
    src = tmp_path / "in.pdf"
    with pikepdf.Pdf.new() as pdf:
        for width in (100, 200, 300, 400):  # tell pages apart by size
            pdf.add_blank_page(page_size=(width, 500))
        pdf.save(src)
    result = pdfprep.layout(src, tmp_path / "out.pdf", pages="4, 1-2")
    assert result.pages == 3 and len(sizes(tmp_path / "out.pdf")) == 3


def test_rotated_page_keeps_its_orientation(tmp_path):
    src = tmp_path / "in.pdf"
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=LETTER)
        pdf.pages[0].obj["/Rotate"] = 90  # displayed landscape
        pdf.save(src)
    pdfprep.layout(src, tmp_path / "out.pdf")
    assert sizes(tmp_path / "out.pdf") == [(842, 595)]


def test_sniffing(tmp_path):
    def kind(name, data):
        path = tmp_path / "blob"
        path.write_bytes(data)
        return pdfprep.sniff_kind(path, name)

    assert kind("report.pdf", b"%PDF-1.7\n...") == "pdf"
    assert kind("mislabelled.docx", b"%PDF-1.4\n...") == "pdf"
    assert kind("fig.ps", b"%!PS-Adobe-3.0\n") == "postscript"
    assert kind("photo.JPG", b"\xff\xd8\xff\xe0binary\0") == "image"
    assert kind("notes.docx", b"PK\x03\x04\0\0") == "office"
    assert kind("main.py", "print('héllo')\n".encode()) == "text"
    assert kind("README", b"plain words\n") == "text"
    with pytest.raises(pdfprep.ConversionError):
        kind("tool.exe", b"MZ\x90\0\x03\0")
    with pytest.raises(pdfprep.ConversionError):
        kind("broken.pdf", b"not a pdf at all")


def test_images_become_a4_pages_with_a_margin(tmp_path):
    src = tmp_path / "shot.png"
    Image.new("RGBA", (1600, 900), (200, 30, 30, 128)).save(src)
    pdf, kind = pdfprep.to_pdf(src, "shot.png", tmp_path, tmp_path / "profile")
    assert kind == "image" and pdfprep.count_pages(pdf) == 1
    pdfprep.layout(pdf, tmp_path / "out.pdf", margin=pdfprep.IMAGE_MARGIN)
    assert sizes(tmp_path / "out.pdf") == [(842, 595)]


def test_unreadable_image_is_reported(tmp_path):
    src = tmp_path / "bad.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
    with pytest.raises(pdfprep.ConversionError):
        pdfprep.to_pdf(src, "bad.png", tmp_path, tmp_path / "profile")


def test_encrypted_pdf_is_reported(tmp_path):
    src = make_pdf(tmp_path / "plain.pdf", 1)
    locked = tmp_path / "locked.pdf"
    with pikepdf.Pdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="secret", owner="secret"))
    with pytest.raises(pdfprep.ConversionError, match="password"):
        pdfprep.count_pages(locked)


@pytest.mark.skipif(not pdfprep.find_office(), reason="LibreOffice not installed")
def test_text_is_converted_with_libreoffice(tmp_path):
    src = tmp_path / "upload"
    src.write_text("def héllo():\n\treturn '✓'\n" * 3)
    pdf, kind = pdfprep.to_pdf(src, "snippet.py", tmp_path, tmp_path / "profile")
    assert kind == "text" and pdfprep.count_pages(pdf) == 1
    if shutil.which("pdftotext"):
        import subprocess
        text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
        assert "héllo" in text and "✓" in text

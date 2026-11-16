"""Build small PDFs whose pages are heavy, or light, in specific ways."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
from PIL import Image
from pikepdf import Dictionary, Name, Stream

PAGE = (595.276, 841.89)


def _jpeg(pdf, width, height, *, mode="L"):
    img = Image.effect_noise((width, height), 40).convert(mode)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return Stream(pdf, buf.getvalue(), Type=Name.XObject, Subtype=Name.Image, Width=width,
                  Height=height, ColorSpace=Name.DeviceGray if mode == "L" else Name.DeviceRGB,
                  BitsPerComponent=8, Filter=Name.DCTDecode)


def _font(pdf):
    return pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))


def add_page(pdf, kind: str, *, label: str = "") -> None:
    """kind: text | photo12 | photo7 | blend2 | plain2 | softmask | form12 | group-only"""
    w, h = PAGE
    resources = Dictionary(Font=Dictionary(F1=_font(pdf)))
    draw = f"BT /F1 24 Tf 72 760 Td ({label or kind}) Tj ET\n"
    if kind in ("photo12", "photo7", "plain2", "blend2"):
        size = {"photo12": (4000, 3000), "photo7": (3000, 2400), "plain2": (1500, 1500),
                "blend2": (1500, 1500)}[kind]
        resources.XObject = Dictionary(Im=_jpeg(pdf, *size))
        draw = f"q {w - 144} 0 0 {h - 288} 72 72 cm /Im Do Q\n" + draw
        if kind == "blend2":
            resources.ExtGState = Dictionary(A=Dictionary(Type=Name.ExtGState, ca=0.5))
            draw = "q /A gs " + draw + "Q\n"
    elif kind == "softmask":
        group = Stream(pdf, b"0 g 0 0 100 100 re f", Type=Name.XObject, Subtype=Name.Form,
                       BBox=[0, 0, 100, 100], Group=Dictionary(S=Name.Transparency))
        resources.ExtGState = Dictionary(M=Dictionary(
            Type=Name.ExtGState, SMask=Dictionary(Type=Name.Mask, S=Name.Luminosity, G=group)))
        draw = "q /M gs 0.5 g 72 72 200 200 re f Q\n" + draw
    elif kind == "form12":
        form = Stream(pdf, b"q 400 0 0 300 0 0 cm /Im Do Q", Type=Name.XObject,
                      Subtype=Name.Form, BBox=[0, 0, 400, 300],
                      Resources=Dictionary(XObject=Dictionary(Im=_jpeg(pdf, 4000, 3000))))
        resources.XObject = Dictionary(Fm=form)
        draw = "q 1 0 0 1 72 300 cm /Fm Do Q\n" + draw
    page = pdf.add_blank_page(page_size=PAGE)
    page.obj.Resources = resources
    page.obj.Contents = Stream(pdf, draw.encode())
    if kind == "group-only":  # PowerPoint sets this on every page; it costs nothing by itself
        page.obj.Group = Dictionary(Type=Name.Group, S=Name.Transparency, CS=Name.DeviceRGB)


def build(path: Path, kinds: list[str]) -> Path:
    with pikepdf.Pdf.new() as pdf:
        for n, kind in enumerate(kinds):
            add_page(pdf, kind, label=f"Page {n + 1} {kind}")
        pdf.save(path)
    return path

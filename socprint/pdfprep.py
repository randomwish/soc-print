"""Turn dropped files into PDFs and lay them out for printing.

The SoC print servers accept only PDF, PostScript and plain text, so everything
is converted to PDF locally first. That also gives an exact page count and a
preview of what will come out of the printer.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from PIL import Image, ImageOps

PAPER = {"A4": (595.276, 841.890), "A3": (841.890, 1190.551)}

# How N pages may be arranged on a sheet, as (columns, rows).
GRIDS = {1: [(1, 1)], 2: [(1, 2), (2, 1)], 4: [(2, 2)], 6: [(2, 3), (3, 2)]}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
OFFICE_EXT = {
    ".doc", ".docx", ".odt", ".rtf",
    ".ppt", ".pptx", ".pps", ".ppsx", ".odp",
    ".xls", ".xlsx", ".ods", ".csv",
}
KIND_LABELS = {
    "pdf": "PDF",
    "postscript": "PostScript",
    "image": "Image",
    "office": "Office document",
    "text": "Text",
}

IMAGE_MARGIN = 28.0  # points (~1 cm) so photos are not cut off by the unprintable edge
CELL_GAP = 10.0  # points between pages when several share a sheet
SHEET_MARGIN = 18.0  # outer margin when several pages share a sheet

_office_lock = threading.Lock()


class ConversionError(Exception):
    """The file could not be turned into a printable PDF."""


def find_office() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def find_ghostscript() -> str | None:
    return shutil.which("gs")


# ---------------------------------------------------------------- conversion


def sniff_kind(path: Path, name: str) -> str:
    """Classify a file by content first, then by extension."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    ext = Path(name).suffix.lower()
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"%!"):
        return "postscript"
    if ext in IMAGE_EXT:
        return "image"
    if ext in OFFICE_EXT:
        return "office"
    if ext in (".pdf", ".ps", ".eps"):
        raise ConversionError(f"This doesn't look like a valid {ext[1:].upper()} file")
    if b"\0" not in head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character cut off at the end of the sample is fine.
            if exc.start < len(head) - 4:
                raise ConversionError(_unsupported(ext)) from None
        return "text"
    raise ConversionError(_unsupported(ext))


def _unsupported(ext: str) -> str:
    what = f"“{ext}” files" if ext else "This file type"
    return f"{what} can't be printed. Use PDF, Word, PowerPoint, Excel, images or text."


def to_pdf(src: Path, name: str, workdir: Path, office_profile: Path) -> tuple[Path, str]:
    """Convert an uploaded file to PDF. Returns (pdf path, kind)."""
    kind = sniff_kind(src, name)
    if kind == "pdf":
        return src, kind
    out = workdir / "converted.pdf"
    if kind == "image":
        _image_to_pdf(src, out)
    elif kind == "postscript":
        _postscript_to_pdf(src, out)
    else:
        _office_to_pdf(src, out, workdir, office_profile, as_text=(kind == "text"))
    return out, kind


def _image_to_pdf(src: Path, out: Path) -> None:
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, "white")
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
            elif im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            # One pixel becomes one point here; layout() scales the page to the paper.
            im.save(out, "PDF", resolution=72.0, quality=92)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ConversionError(f"Couldn't read this image ({exc})") from None


def _postscript_to_pdf(src: Path, out: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        raise ConversionError("Ghostscript (gs) is needed to print PostScript files")
    cmd = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
           f"-sOutputFile={out}", str(src)]
    _run_converter(cmd, out, "Ghostscript")


def _office_to_pdf(src: Path, out: Path, workdir: Path, profile: Path, *, as_text: bool) -> None:
    office = find_office()
    if not office:
        raise ConversionError(
            "LibreOffice is needed to convert this file. Install it, or export to PDF first."
        )
    # LibreOffice picks its import filter from the extension, so give it a clean one.
    staged = workdir / ("input.txt" if as_text else f"input{src.suffix.lower()}")
    shutil.copyfile(src, staged)
    cmd = [office, "--headless", "--norestore", "--nolockcheck",
           f"-env:UserInstallation={profile.resolve().as_uri()}"]
    if as_text:
        cmd.append("--infilter=Text (encoded):UTF8,LF,Liberation Mono,en-US")
    cmd += ["--convert-to", "pdf", "--outdir", str(workdir), str(staged)]
    produced = workdir / "input.pdf"
    with _office_lock:  # one LibreOffice instance per profile at a time
        _run_converter(cmd, produced, "LibreOffice")
    produced.replace(out)
    staged.unlink(missing_ok=True)


def _run_converter(cmd: list[str], expect: Path, tool: str, timeout: float = 180) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"{tool} took too long converting this file") from None
    if not expect.exists() or expect.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        hint = f": {detail[-1]}" if detail else ""
        raise ConversionError(f"{tool} couldn't convert this file{hint}")


def count_pages(pdf_path: Path) -> int:
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            pages = len(pdf.pages)
    except pikepdf.PasswordError:
        raise ConversionError("This PDF is password-protected. Remove the password first.") from None
    except pikepdf.PdfError as exc:
        raise ConversionError(f"This PDF is damaged and can't be read ({exc})") from None
    if pages == 0:
        raise ConversionError("This document has no pages")
    return pages


# -------------------------------------------------------------------- layout


def parse_page_ranges(spec: str, total: int) -> list[int]:
    """“1-3, 7, 10-” → zero-based page indexes, in the order written."""
    spec = (spec or "").strip()
    if not spec or spec.lower() == "all":
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*[-–]\s*(\d*)", part)
        if m and (m[1] or m[2]):
            first = int(m[1]) if m[1] else 1
            last = int(m[2]) if m[2] else total
        elif part.isdigit():
            first = last = int(part)
        else:
            raise ValueError(f"Can't read “{part}”. Try something like 1-3, 7")
        if first < 1 or last > total or first > last:
            plural = "s" if total != 1 else ""
            raise ValueError(f"“{part}” is outside this document ({total} page{plural})")
        out.extend(range(first - 1, last))
    if not out:
        raise ValueError("No pages selected")
    return out


@dataclass(frozen=True)
class Plan:
    """What a job will consume, worked out without touching the PDF."""

    pages: int  # document pages selected
    sides: int  # printed sides per copy
    sheets: int  # sheets of paper for all copies
    copies: int

    @property
    def total_sides(self) -> int:
        return self.sides * self.copies


def plan(total_pages: int, spec: str, *, nup: int, copies: int, duplex: bool) -> Plan:
    if nup not in GRIDS:
        raise ValueError("Pages per sheet must be 1, 2, 4 or 6")
    if not 1 <= copies <= 50:
        raise ValueError("Copies must be between 1 and 50")
    selected = len(parse_page_ranges(spec, total_pages))
    sides = math.ceil(selected / nup)
    sheets_each = math.ceil(sides / 2) if duplex else sides
    return Plan(selected, sides, sheets_each * copies, copies)


def quota_estimate(total_sides: int, *, duplex: bool, kind: str) -> int:
    """Quota charged per the SoC policy: double-sided B/W gets 25% off, rounded."""
    if kind == "bw" and duplex:
        return math.floor(total_sides * 0.75 + 0.5)
    return total_sides


def _rotation(page: pikepdf.Page) -> int:
    node = page.obj
    for _ in range(64):  # /Rotate may be inherited from a parent node
        if "/Rotate" in node:
            return int(node["/Rotate"]) % 360
        if "/Parent" not in node:
            break
        node = node["/Parent"]
    return 0


def _visible_size(page: pikepdf.Page) -> tuple[float, float]:
    x0, y0, x1, y1 = (float(v) for v in page.cropbox)
    w, h = abs(x1 - x0), abs(y1 - y0)
    return (h, w) if _rotation(page) in (90, 270) else (w, h)


def _best_grid(nup: int, page_size: tuple[float, float], paper: tuple[float, float], margin: float):
    """Choose sheet orientation and grid that show the pages largest."""
    pw, ph = page_size
    best = None
    for sheet_w, sheet_h in (paper, paper[::-1]):
        for cols, rows in GRIDS[nup]:
            cell_w = (sheet_w - 2 * margin - (cols - 1) * CELL_GAP) / cols
            cell_h = (sheet_h - 2 * margin - (rows - 1) * CELL_GAP) / rows
            scale = min(cell_w / pw, cell_h / ph)
            if best is None or scale > best[0] + 1e-9:
                best = (scale, sheet_w, sheet_h, cols, rows, cell_w, cell_h)
    return best[1:]


def compose(
    src_pdf: Path,
    out_pdf: Path,
    *,
    pages: str = "",
    nup: int = 1,
    paper: str = "A4",
    margin: float = 0.0,
) -> list[list[int]]:
    """Write one copy of the sheets to print. Returns the source pages on each sheet.

    Selects pages, scales everything onto the printer's paper size (a Letter or
    slide-shaped page would otherwise stall the printer on "wrong paper size")
    and puts several pages on a sheet if asked.
    """
    paper_size = PAPER[paper]
    sheets: list[list[int]] = []
    with pikepdf.Pdf.open(src_pdf) as src, pikepdf.Pdf.new() as out:
        indexes = parse_page_ranges(pages, len(src.pages))
        if nup not in GRIDS:
            raise ValueError("Pages per sheet must be 1, 2, 4 or 6")

        if nup == 1:
            for i in indexes:
                sheets.append([i])
                page = src.pages[i]
                w, h = _visible_size(page)
                fits = any(
                    abs(w - pw) < 2 and abs(h - ph) < 2 for pw, ph in (paper_size, paper_size[::-1])
                )
                if fits and margin == 0:
                    out.pages.append(page)
                    continue
                sheet_w, sheet_h, *_ = _best_grid(1, (w, h), paper_size, margin)
                sheet = out.add_blank_page(page_size=(sheet_w, sheet_h))
                sheet.add_overlay(
                    page, pikepdf.Rectangle(margin, margin, sheet_w - margin, sheet_h - margin)
                )
        else:
            m = max(margin, SHEET_MARGIN)
            first = _visible_size(src.pages[indexes[0]])
            sheet_w, sheet_h, cols, _rows, cell_w, cell_h = _best_grid(nup, first, paper_size, m)
            for start in range(0, len(indexes), nup):
                group = indexes[start:start + nup]
                sheets.append(group)
                sheet = out.add_blank_page(page_size=(sheet_w, sheet_h))
                for slot, i in enumerate(group):
                    col, row = slot % cols, slot // cols
                    x = m + col * (cell_w + CELL_GAP)
                    y_top = sheet_h - m - row * (cell_h + CELL_GAP)
                    sheet.add_overlay(
                        src.pages[i], pikepdf.Rectangle(x, y_top - cell_h, x + cell_w, y_top)
                    )

        out.remove_unreferenced_resources()
        out.save(out_pdf)
    return sheets


def repeat_copies(src_pdf: Path, out_pdf: Path, *, copies: int, duplex: bool) -> None:
    """Repeat the sheets for each copy, starting every copy on a fresh sheet of paper."""
    with pikepdf.Pdf.open(src_pdf) as out:
        per_copy = len(out.pages)
        pad = duplex and per_copy % 2 == 1
        last_w, last_h = _visible_size(out.pages[per_copy - 1])
        for _ in range(copies - 1):
            if pad:
                out.add_blank_page(page_size=(last_w, last_h))
            for k in range(per_copy):
                out.pages.append(out.pages[k])
        out.save(out_pdf)


def layout(
    src_pdf: Path,
    out_pdf: Path,
    *,
    pages: str = "",
    nup: int = 1,
    copies: int = 1,
    duplex: bool = True,
    paper: str = "A4",
    margin: float = 0.0,
) -> Plan:
    """compose() followed by repeat_copies(), without the heavy-page treatment."""
    with pikepdf.Pdf.open(src_pdf) as src:
        total = len(src.pages)
    result = plan(total, pages, nup=nup, copies=copies, duplex=duplex)
    one = out_pdf.with_name(out_pdf.stem + ".one.pdf")
    compose(src_pdf, one, pages=pages, nup=nup, paper=paper, margin=margin)
    repeat_copies(one, out_pdf, copies=copies, duplex=duplex)
    one.unlink(missing_ok=True)
    return result


# ------------------------------------------------------------- heavy pages
#
# Exported decks (AI slide generators, Canva, Gamma, ...) tend to put a 4K photo
# behind every slide and draw glows and glass effects with transparency. Laser
# printers and the PostScript the print server feeds them have no cheap way to
# handle that: every such page is composited at full resolution inside the
# printer, which is where the minutes go. Sending those pages as one ready-made
# 300 dpi image each takes that work off the printer.

HEAVY_PIXELS = 12_000_000  # more image pixels than a full A4 page at ~350 dpi
HEAVY_BLEND_PIXELS = 2_000_000  # images this large under transparency
HEAVY_CONTENT_BYTES = 3_000_000  # e.g. a scatter plot with a million points
RASTER_DPI = 300


@dataclass(frozen=True)
class PageCost:
    pixels: int = 0  # image pixels drawn on the page (each image counted once)
    blend: bool = False  # transparency: alpha, soft-masked images
    soft_mask: bool = False  # luminosity / alpha soft masks in the graphics state
    content: int = 0  # bytes of drawing instructions

    def __add__(self, other: "PageCost") -> "PageCost":
        return PageCost(self.pixels + other.pixels, self.blend or other.blend,
                        self.soft_mask or other.soft_mask, self.content + other.content)

    @property
    def heavy(self) -> bool:
        return (
            self.pixels >= HEAVY_PIXELS
            or self.soft_mask
            or (self.blend and self.pixels >= HEAVY_BLEND_PIXELS)
            or self.content >= HEAVY_CONTENT_BYTES
        )


def _stream_bytes(obj) -> int:
    try:
        return int(obj.get("/Length", 0))
    except (TypeError, ValueError):
        return 0


def _resources_cost(resources, seen: set) -> PageCost:
    cost = PageCost()
    if not isinstance(resources, pikepdf.Dictionary):
        return cost
    states = resources.get("/ExtGState")
    if isinstance(states, pikepdf.Dictionary):
        for _, state in states.items():
            if not isinstance(state, pikepdf.Dictionary):
                continue
            mask = state.get("/SMask")
            if mask is not None and mask != pikepdf.Name.None_:
                cost += PageCost(soft_mask=True)
            for key in ("/ca", "/CA"):
                try:
                    if key in state and float(state[key]) < 0.999:
                        cost += PageCost(blend=True)
                except (TypeError, ValueError):
                    pass
    xobjects = resources.get("/XObject")
    if isinstance(xobjects, pikepdf.Dictionary):
        for _, xobj in xobjects.items():
            if not isinstance(xobj, pikepdf.Stream):
                continue
            key = xobj.objgen if xobj.is_indirect else id(xobj)
            if key in seen:
                continue
            seen.add(key)
            subtype = xobj.get("/Subtype")
            if subtype == pikepdf.Name.Image:
                try:
                    pixels = int(xobj.get("/Width", 0)) * int(xobj.get("/Height", 0))
                except (TypeError, ValueError):
                    pixels = 0
                masked = "/SMask" in xobj or isinstance(xobj.get("/Mask"), pikepdf.Stream)
                cost += PageCost(pixels=pixels, blend=masked)
            elif subtype == pikepdf.Name.Form:
                cost += PageCost(content=_stream_bytes(xobj))
                cost += _resources_cost(xobj.get("/Resources"), seen)
    return cost


def page_costs(pdf_path: Path) -> list[PageCost]:
    """How much rendering work each page asks of a printer. Cheap: nothing is decoded."""
    costs = []
    with pikepdf.Pdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            try:
                contents = page.obj.get("/Contents")
                if isinstance(contents, pikepdf.Array):
                    content = sum(_stream_bytes(c) for c in contents)
                else:
                    content = _stream_bytes(contents) if contents is not None else 0
                cost = PageCost(content=content) + _resources_cost(page.resources, set())
            except (pikepdf.PdfError, AttributeError, KeyError, TypeError, ValueError):
                cost = PageCost()  # unreadable structure: leave the page alone
            costs.append(cost)
    return costs


def heavy_sheets(costs: list[PageCost], sheets: list[list[int]]) -> list[int]:
    """Indexes of sheets whose pages, taken together, are heavy."""
    heavy = []
    for n, group in enumerate(sheets):
        total = PageCost()
        for i in group:
            total += costs[i]
        if total.heavy:
            heavy.append(n)
    return heavy


def sheets_for(spec: str, total: int, nup: int) -> list[list[int]]:
    """The source pages compose() will put on each sheet, without building anything."""
    indexes = parse_page_ranges(spec, total)
    return [indexes[i:i + nup] for i in range(0, len(indexes), nup)]


def rasterize_sheets(src_pdf: Path, out_pdf: Path, sheets: list[int], *, colour: bool,
                     dpi: int = RASTER_DPI) -> None:
    """Replace the given sheets (0-based) by single images, grayscale unless colour."""
    gs = find_ghostscript()
    if not gs:
        raise ConversionError("Ghostscript (gs) is needed to speed up heavy pages")
    images = out_pdf.with_name(out_pdf.stem + ".images.pdf")
    cmd = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dUseCropBox",
           "-sDEVICE=" + ("pdfimage24" if colour else "pdfimage8"), f"-r{dpi}",
           "-sCompression=JPEG", "-dJPEGQ=85",
           "-sPageList=" + ",".join(str(n + 1) for n in sheets),
           f"-sOutputFile={images}", str(src_pdf)]
    try:
        _run_converter(cmd, images, "Ghostscript", timeout=900)
        with pikepdf.Pdf.open(src_pdf) as out, pikepdf.Pdf.open(images) as raster:
            if len(raster.pages) != len(sheets):
                raise ConversionError("Ghostscript returned the wrong number of pages")
            for n, page in zip(sheets, raster.pages, strict=True):
                out.pages[n] = page
            out.remove_unreferenced_resources()
            out.save(out_pdf)
    finally:
        images.unlink(missing_ok=True)

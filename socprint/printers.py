"""SoC print queue catalogue.

Transcribed from https://dochub.comp.nus.edu.sg/cf/guides/printing/print-queues
(page revision 2026-10-24). Queue naming, per that page:

* B/W ("ps…") queues print double-sided by default; "-sx" is single-sided and
  "-nb" skips the banner page.
* Colour ("cp…") queues print single-sided by default; "-dx" is double-sided.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

QUEUE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

ACCESS_LABELS = {
    "open": "Open to all SoC users",
    "staff": "Staff only",
    "staff-grad": "Staff and graduate students",
    "lab": "Research and teaching labs (restricted)",
}


@dataclass(frozen=True)
class Printer:
    id: str
    location: str
    model: str
    kind: str  # "bw" | "colour"
    paper: str  # "A4" | "A3"
    access: str  # key of ACCESS_LABELS
    banner: bool  # the standard queue prints a banner page
    queues: dict[str, str]  # "duplex" | "simplex" | "duplex-nb" | "simplex-nb" -> queue


def _bw(id, location, model="Lexmark MS810DN", *, access, banner=True, nb=None):
    """nb: None, "duplex" (only a double-sided no-banner queue) or "both"."""
    queues = {"duplex": id, "simplex": f"{id}-sx"}
    if nb in ("duplex", "both"):
        queues["duplex-nb"] = f"{id}-nb"
    if nb == "both":
        queues["simplex-nb"] = f"{id}-nb-sx"
    return Printer(id, location, model, "bw", "A4", access, banner, queues)


def _colour(id, location, model, *, access, paper="A4", duplex=True):
    queues = {"simplex": id}
    if duplex:
        queues["duplex"] = f"{id}-dx"
    return Printer(id, location, model, "colour", paper, access, False, queues)


_COM1_B1 = "COM1 Basement, outside Programming Lab 3"
_COM1_L1 = "COM1 Level 1, Printer Area"
_TECH = "COM1-01-06, Technical Services"

PRINTERS: list[Printer] = [
    # -- Print queues without user restrictions
    _bw("psts", _COM1_L1, "Lexmark MS821DN", access="open", nb="duplex"),
    _bw("pstsb", _COM1_L1, "Lexmark MS821DN", access="open", nb="duplex"),
    _bw("pstsc", _COM1_L1, "Lexmark MS821DN", access="open", nb="duplex"),
    _bw("psc008", _COM1_B1, "Lexmark MS821DN", access="open", nb="duplex"),
    _bw("psc011", _COM1_B1, "Lexmark MS821DN", access="open", nb="duplex"),
    _bw("psf204", "COM4 Level 2, Printer Area (corridor)", "Lexmark MS810", access="open"),
    _colour("cptsc", _TECH, "Lexmark CS921DE", access="open"),
    _colour("cptsc-a3", _TECH, "Lexmark CS921DE", access="open", paper="A3"),
    # -- Staff only
    _bw("psc106", _TECH, access="staff", nb="both"),
    _bw("psc313", "COM1-03-13, Mail Room", access="staff", nb="both"),
    _colour("cpc313", "COM1-03-13, Mail Room", "Lexmark CS521DN", access="staff"),
    _bw("psgob", "COM1-03-68, Dean's Office", access="staff"),
    _bw("psgoc", "COM1-03-38, Equipment Room", access="staff"),
    _colour("cpgoc", "COM1-03-38, Equipment Room", "HP LaserJet Enterprise M751dn", access="staff"),
    _colour("cpgoc-a3", "COM1-03-38, Equipment Room", "HP LaserJet Enterprise M751dn",
            access="staff", paper="A3", duplex=False),
    _bw("psd238", "COM2-02-38, Equipment Room", access="staff", nb="both"),
    _bw("psd263", "COM2-02-63, Equipment Room", "Lexmark MS821DN", access="staff", nb="both"),
    _bw("psd313", "COM2-03-13, Equipment Room", access="staff", nb="both"),
    _colour("cpd313", "COM2-03-13, Equipment Room", "Lexmark C950", access="staff"),
    _bw("psd405", "COM2-04-05, Equipment Room", access="staff", nb="both"),
    _bw("pse202", "COM3-02-02, Staff Equipment Room", "Lexmark MS821DN", access="staff"),
    _bw("pse017", "COM3-B1-17, Staff Equipment Room", access="staff"),
    _bw("psa427", "AS6-04-27, TA Office", access="staff"),
    _bw("psa518", "AS6-05-18, Staff Equipment Room", access="staff", nb="both"),
    _colour("cpa518", "AS6-05-18, Staff Equipment Room", "Lexmark CS521DN", access="staff"),
    # -- Staff and graduate students
    _bw("pse124", "COM3-01-24, Research and TA Mail/Parcel Room", "Lexmark MS810",
        access="staff-grad"),
    # -- Restricted access (labs)
    _bw("psc107", "COM1-01-07, Database Research Lab 3", access="lab", banner=False),
    _bw("psc108", "COM1-01-08, Database Research Lab 1", access="lab", banner=False),
    _bw("psc109", "COM1-01-09, Artificial Intelligence Research Lab 2", access="lab", banner=False),
    _bw("psc111", "COM1-01-11, Database Research Lab 2", access="lab", banner=False),
    _bw("psc113", "COM1-01-13, Embedded Systems Teaching Lab 2", access="lab"),
    _bw("psc115", "COM1-01-15, Computational Biology Research Lab 2", access="lab", banner=False),
    _bw("psc121", "COM1-01-21, Computational Biology Research Lab 1", access="lab", banner=False),
    _bw("psd002", "COM2-B1-02, TA/GT Cluster 2", access="lab", banner=False),
    _bw("psd003", "COM2-B1-03, TA/GT Cluster 3", access="lab", banner=False),
    _bw("psd107", "COM2-01-07, Information Systems Research Lab 3", access="lab", banner=False),
    _bw("psd404", "COM2-04-04, Programming Languages and Software Engineering Research Lab 2",
        access="lab", banner=False),
    _bw("pse033", "COM3-B1-34, Health Informatics Research Lab (formerly COM3-B1-33)",
        access="lab", banner=False),
    _bw("pse217", "COM3-02-17, Systems and Networking Lab", access="lab", banner=False),
    _bw("pse218", "COM3-02-18, Security Research Lab 1", "Lexmark MS821DN",
        access="lab", banner=False),
    _bw("pse220", "COM3-02-20, Programming Languages and Software Engineering Lab",
        access="lab", banner=False),
    _bw("pse221", "COM3-02-21, Artificial Intelligence Research Lab 1", access="lab", banner=False),
    _bw("pse265", "COM3-02-65, Information Systems and Analytics Research Lab 1",
        access="lab", banner=False),
    _bw("psf501", "COM4-05-01, Artificial Intelligence Research Lab 3", "Lexmark MS821DN",
        access="lab", banner=False),
    _bw("psa403", "AS6-04-03, Media Research Lab 2", access="lab", banner=False),
    _bw("psa411", "AS6-04-11, Media Research Lab 4B", access="lab", banner=False),
    _bw("psa413", "AS6-04-13, Media Research Lab 1", access="lab", banner=False),
    _bw("psa421", "AS6-04-21, Media Teaching Lab 1", access="lab"),
    _bw("psa425", "AS6-04-25, Systems & Networking Research Lab 9", access="lab", banner=False),
    _bw("psa426", "AS6-04-26, Media Teaching Lab 2A", access="lab"),
    _bw("psa501", "AS6-05-01, Media Research Lab 3", access="lab", banner=False),
    _bw("psa502", "AS6-05-02, Media Research Lab 4", access="lab", banner=False),
    _bw("psa517", "AS6-05-17, Media Research Lab 5", "Lexmark MS821DN", access="lab", banner=False),
    _bw("psa521", "AS6-05-21, Media Research Lab 6", access="lab", banner=False),
    _bw("psa522", "AS6-05-22, Media Research Lab 7", access="lab", banner=False),
    _bw("psa523", "AS6-05-23, Media Research Lab 12", "Lexmark MS821DN", access="lab", banner=False),
    _bw("psa525", "AS6-05-25, Media Research Lab 8", "Lexmark MS821DN", access="lab", banner=False),
    _bw("psk100", "4KR Level 1, Media Research Lab @ 4KR", "Lexmark MS821DN",
        access="lab", banner=False),
    _bw("psk200", "4KR Level 2, Razer-NUS Joint AI Research Lab @ 4KR", access="lab", banner=False),
]

BY_ID = {p.id: p for p in PRINTERS}


@dataclass(frozen=True)
class Destination:
    """A fully resolved place to send a job."""

    queue: str
    duplex: bool
    kind: str  # "bw" | "colour"
    paper: str
    label: str  # human-readable location, "" for a custom queue


def resolve(printer_id: str, sides: str, banner: bool) -> Destination:
    """Pick the queue of a catalogued printer for the wanted sides and banner."""
    printer = BY_ID.get(printer_id)
    if printer is None:
        raise ValueError(f"Unknown printer “{printer_id}”")
    if sides not in ("duplex", "simplex"):
        raise ValueError("Sides must be double-sided or single-sided")
    want_no_banner = printer.banner and not banner
    key = f"{sides}-nb" if want_no_banner else sides
    queue = printer.queues.get(key)
    if queue is None:
        side_word = "double-sided" if sides == "duplex" else "single-sided"
        if want_no_banner:
            raise ValueError(f"{printer.id} has no {side_word} queue without a banner page")
        raise ValueError(f"{printer.id} has no {side_word} queue")
    return Destination(queue, sides == "duplex", printer.kind, printer.paper, printer.location)


def resolve_custom(queue: str) -> Destination:
    """Describe a queue typed in by hand, inferring its mode from the naming rules."""
    queue = queue.strip().lower()
    if not QUEUE_RE.fullmatch(queue):
        raise ValueError("Queue names use lowercase letters, digits and dashes, e.g. psts-sx")
    parts = queue.split("-")
    colour = queue.startswith("cp")
    duplex = "dx" in parts[1:] if colour else "sx" not in parts[1:]
    paper = "A3" if "a3" in parts[1:] else "A4"
    return Destination(queue, duplex, "colour" if colour else "bw", paper, "")


def catalogue() -> list[dict]:
    return [asdict(p) for p in PRINTERS]

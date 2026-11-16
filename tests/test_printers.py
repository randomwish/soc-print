import pytest

from socprint import printers


def test_catalogue_is_well_formed():
    ids = [p.id for p in printers.PRINTERS]
    assert len(ids) == len(set(ids))
    queues = [q for p in printers.PRINTERS for q in p.queues.values()]
    assert len(queues) == len(set(queues))
    for queue in queues:
        assert printers.QUEUE_RE.fullmatch(queue), queue
    for p in printers.PRINTERS:
        assert p.access in printers.ACCESS_LABELS
        assert p.id.startswith("cp" if p.kind == "colour" else "ps")


@pytest.mark.parametrize("printer, sides, banner, queue", [
    # B/W: double-sided is the plain queue
    ("psts", "duplex", True, "psts"),
    ("psts", "simplex", True, "psts-sx"),
    ("psts", "duplex", False, "psts-nb"),
    ("psc106", "simplex", False, "psc106-nb-sx"),
    # Colour: single-sided is the plain queue
    ("cptsc", "simplex", True, "cptsc"),
    ("cptsc", "duplex", True, "cptsc-dx"),
    ("cptsc-a3", "duplex", True, "cptsc-a3-dx"),
    # Lab printers never print a banner, so the checkbox makes no difference
    ("psc107", "simplex", False, "psc107-sx"),
    ("psc107", "duplex", True, "psc107"),
])
def test_resolve(printer, sides, banner, queue):
    dest = printers.resolve(printer, sides, banner)
    assert dest.queue == queue
    assert dest.duplex == (sides == "duplex")


@pytest.mark.parametrize("printer, sides, banner", [
    ("psts", "simplex", False),      # psts-nb is double-sided only
    ("psf204", "duplex", False),     # no banner-free queue at all
    ("cpgoc-a3", "duplex", True),    # A3 on cpgoc is single-sided only
    ("nope", "duplex", True),
    ("psts", "sideways", True),
])
def test_resolve_rejects_combinations_the_printer_lacks(printer, sides, banner):
    with pytest.raises(ValueError):
        printers.resolve(printer, sides, banner)


@pytest.mark.parametrize("queue, duplex, kind, paper", [
    ("psc107", True, "bw", "A4"),
    ("psc107-sx", False, "bw", "A4"),
    ("psd238-nb-sx", False, "bw", "A4"),
    ("PSTS-NB", True, "bw", "A4"),
    ("cpc313", False, "colour", "A4"),
    ("cptsc-a3-dx", True, "colour", "A3"),
])
def test_custom_queue_inference(queue, duplex, kind, paper):
    dest = printers.resolve_custom(queue)
    assert (dest.duplex, dest.kind, dest.paper) == (duplex, kind, paper)
    assert dest.queue == queue.lower()


@pytest.mark.parametrize("queue", ["", "-P", "psts; rm -rf ~", "a", "psts sx", "$(id)"])
def test_custom_queue_rejects_unsafe_names(queue):
    with pytest.raises(ValueError):
        printers.resolve_custom(queue)

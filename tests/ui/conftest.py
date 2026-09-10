"""Native event fixtures adapted to the Tk version used by each test session."""

import pytest


@pytest.fixture
def scroll_event():
    def generate(widget, sequence, *, delta, **kwargs):
        version = widget.tk.call("package", "provide", "Tk")
        legacy = int(widget.tk.call("package", "vcompare", version, "9.0")) < 0
        if sequence == "<TouchpadScroll>" and legacy:
            pytest.skip("Precise scroll events require Tk 9")
        if sequence == "<MouseWheel>" and legacy:
            if widget.tk.call("tk", "windowingsystem") == "aqua":
                delta = int(delta / 120)
        widget.event_generate(sequence, delta=delta, **kwargs)

    return generate

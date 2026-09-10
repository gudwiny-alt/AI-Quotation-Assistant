"""Wheel and Tk 9 precise-scroll support for the native custom scroll surfaces."""

from functools import partial
import tkinter as tk


def bind_scrolling(widget, callback):
    """Keep bindings local; native Text/Listbox class bindings remain untouched."""
    widget.bind("<MouseWheel>", callback, add="+")
    version = widget.tk.call("package", "provide", "Tk")
    if int(widget.tk.call("package", "vcompare", version, "9.0")) >= 0:
        widget.bind("<TouchpadScroll>", partial(callback, precise=True), add="+")
    if widget.tk.call("tk", "windowingsystem") == "x11":
        widget.bind("<Button-4>", callback, add="+")
        widget.bind("<Button-5>", callback, add="+")


def wheel_pixels(widget, event, *, precise=False):
    """Return content motion in pixels, decoding Tk's signed packed touch deltas."""
    if precise:
        dx, dy = widget.tk.call("tk::PreciseScrollDeltas", event.delta)
        return -int(dx), -int(dy)
    button = getattr(event, "num", None)
    if button in (4, 5):
        distance = -40 if button == 4 else 40
    else:
        version = widget.tk.call("package", "provide", "Tk")
        legacy_aqua = (
            widget.tk.call("tk", "windowingsystem") == "aqua"
            and int(widget.tk.call("package", "vcompare", version, "9.0")) < 0
        )
        distance = -event.delta * (20 if legacy_aqua else 40 / 120)
    return (distance, 0) if event.state & 1 else (0, distance)


def scroll_canvas(canvas: tk.Canvas, event, *, precise=False):
    dx, dy = wheel_pixels(canvas, event, precise=precise)
    region = canvas.tk.splitlist(canvas.cget("scrollregion"))
    if len(region) == 4:
        x1, y1, x2, y2 = map(float, region)
        if dx and x2 > x1:
            canvas.xview_moveto(canvas.xview()[0] + dx / (x2 - x1))
        if dy and y2 > y1:
            canvas.yview_moveto(canvas.yview()[0] + dy / (y2 - y1))
    return "break"

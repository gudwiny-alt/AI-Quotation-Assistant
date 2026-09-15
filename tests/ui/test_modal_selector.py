"""Real macOS events: a modal editor must not swallow its selector's clicks."""

import os
import sys
import time
import tkinter as tk

import pytest
from PIL import Image

from tests.ui.test_review_recovery_ui import walk
from tests.test_review_support import session as base_session

pytestmark = pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1" or sys.platform != "darwin",
    reason="Needs native macOS mouse events",
)


@pytest.fixture
def desktop():
    import Quartz
    from AppKit import NSApplication

    root = tk.Tk()
    root.geometry("700x900+40+40")
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def pump():
        for _ in range(6):
            root.update()
            time.sleep(0.03)

    def click(widget, x=None, y=None):
        point = (
            widget.winfo_rootx() + (x if x is not None else widget.winfo_width() / 2),
            widget.winfo_rooty() + (y if y is not None else widget.winfo_height() / 2),
        )
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            pump()

    yield root, pump, click
    root.destroy()
    assert not errors


def test_editor_native_channel_click_reloads_price_link_and_picture(desktop, tmp_path):
    from quote_app.desktop_controls import SoftSelect, SoftEntry
    from quote_app.desktop_evidence import open_evidence_editor
    from quote_app.desktop_state import TaskRow

    root, pump, click = desktop
    session = base_session.__wrapped__(tmp_path)
    product = session.products[0]
    for channel, price, color in [
        ("official", "1799", "red"),
        ("tmall", "1699", "blue"),
        ("jd", "1599", "green"),
    ]:
        picture = tmp_path / f"{channel}.png"
        Image.new("RGB", (1000, 700), color).save(picture)
        product.channels.append(
            TaskRow(
                channel,
                product.title,
                channel,
                price=price,
                url=f"https://{channel}.example/product",
                evidence_path=picture,
            )
        )
    original = session.quote_path.read_bytes()
    dialog = open_evidence_editor(root, session, product, lambda: None)
    dialog.geometry("+90+70")
    dialog.focus_force()
    pump()
    selector = next(w for w in walk(dialog) if isinstance(w, SoftSelect))
    fields = [w for w in walk(dialog) if isinstance(w, SoftEntry)]
    for index, title, channel, price in [
        (1, "天猫", "tmall", "1699"),
        (2, "京东", "jd", "1599"),
        (0, "官网", "official", "1799"),
    ]:
        click(selector)
        assert selector.popup is not None
        box = selector.listbox
        x, y, width, height = box.bbox(index)
        click(box, 20, y + height / 2)
        assert selector.variable.get() == title
        assert fields[0].variable.get() == price
        assert fields[1].variable.get() == f"https://{channel}.example/product"
        assert any(
            isinstance(w, tk.Label) and w.cget("text") == f"{channel}.png" for w in walk(dialog)
        )
        assert root.grab_current() is dialog
    assert session.quote_path.read_bytes() == original
    dialog.destroy()
    pump()
    assert root.grab_current() is None


@pytest.mark.parametrize("dismiss", ["outside", "escape", "destroy"])
def test_modal_popup_dismissal_restores_input(desktop, dismiss):
    from quote_app.desktop_controls import SoftSelect

    root, pump, click = desktop
    dialog = tk.Toplevel(root)
    dialog.geometry("400x230+100+100")
    var = tk.StringVar(dialog, "官网")
    selector = SoftSelect(dialog, textvariable=var, values=("官网", "天猫", "京东"))
    selector.pack()
    entry = tk.Entry(dialog)
    entry.pack(pady=(150, 0))
    dialog.grab_set()
    dialog.focus_force()
    pump()
    click(selector)
    if dismiss == "destroy":
        dialog.destroy()
        pump()
        assert root.grab_current() is None
        return
    if dismiss == "escape":
        selector.listbox.event_generate("<Escape>")
        pump()
    else:
        click(entry)
    assert selector.popup is None
    assert var.get() == "官网"
    assert root.grab_current() is dialog
    click(entry)
    assert root.focus_get() is entry
    dialog.destroy()


def test_modal_overflow_options_accept_native_mouse_wheel(desktop):
    import Quartz
    from quote_app.desktop_controls import SoftSelect

    root, pump, click = desktop
    dialog = tk.Toplevel(root)
    dialog.geometry("400x230+100+100")
    var = tk.StringVar(dialog, "选项0")
    selector = SoftSelect(dialog, textvariable=var, values=tuple(f"选项{i}" for i in range(20)))
    selector.pack()
    dialog.grab_set()
    dialog.focus_force()
    pump()
    click(selector)
    box = selector.listbox
    point = (box.winfo_rootx() + 30, box.winfo_rooty() + 30)
    move = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    pump()
    wheel = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, -3)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, wheel)
    pump()
    assert box.yview()[0] > 0
    assert var.get() == "选项0"
    selector.close_popup()
    pump()
    assert root.grab_current() is dialog
    dialog.destroy()

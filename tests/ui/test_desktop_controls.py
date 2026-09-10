"""Native form interaction regressions; no business run or network access."""

import os
import tkinter as tk

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Needs native desktop"
)


@pytest.fixture
def root():
    window = tk.Tk()
    window.geometry("640x480")
    errors = []
    window.report_callback_exception = lambda *args: errors.append(args)
    yield window
    window.destroy()
    assert not errors


def test_month_picker_cancel_validation_and_apply_preserve_actual_variables(root):
    from quote_app.desktop_controls import MonthPicker

    year, month = tk.StringVar(root, "2026"), tk.StringVar(root, "9")
    control = MonthPicker(root, yearvariable=year, monthvariable=month)
    control.pack()
    root.update()
    control.open_popup()
    control.draft_year.set("2027")
    control.close_popup()
    assert (year.get(), month.get()) == ("2026", "9")
    control.open_popup()
    control.draft_year.set("invalid")
    control.choose_month(2)
    assert (year.get(), month.get()) == ("2026", "9")
    assert control.popup is not None
    control.draft_year.set("2027")
    control.choose_month(2)
    root.update()
    assert (year.get(), month.get()) == ("2027", "2")
    assert control.cget("text") == "2027年2月"
    assert control.popup is None


def test_select_emits_selection_and_dismisses_without_editing_value(root):
    from quote_app.desktop_controls import SoftSelect

    variable = tk.StringVar(root, "全品牌")
    control = SoftSelect(root, textvariable=variable, values=("全品牌", "华为", "荣耀"))
    control.pack()
    seen = []
    control.bind("<<ComboboxSelected>>", lambda e: seen.append(variable.get()))
    root.update()
    control.open_popup()
    control.choose("荣耀")
    root.update()
    assert variable.get() == "荣耀"
    assert seen == ["荣耀"]
    control.open_popup()
    control.close_popup()
    assert variable.get() == "荣耀"
    control.configure(state="disabled")
    control.open_popup()
    assert control.popup is None


def test_placeholder_is_not_inserted_in_input_or_shared_variable(root):
    from quote_app.desktop_controls import SoftEntry

    variable = tk.StringVar(root)
    control = SoftEntry(root, textvariable=variable, placeholder="输入任务编号搜索")
    control.pack()
    root.update()
    assert variable.get() == ""
    assert control.entry.get() == ""
    control.entry.insert(0, "任务-123")
    root.update()
    assert variable.get() == "任务-123"
    assert not control.hint.winfo_ismapped()
    control.entry.selection_range(0, "end")
    assert control.entry.selection_get() == "任务-123"


@pytest.mark.parametrize("sequence,down,up", [("<MouseWheel>", -120, 120), ("<TouchpadScroll>", 65516, 20)])
@pytest.mark.parametrize("surface", ["rows", "bar", "header"])
def test_table_wheel_moves_in_both_directions_without_skipping_entire_table(
    root, scroll_event, sequence, down, up, surface
):
    from quote_app.desktop_widgets import RichTable

    table = RichTable(root, [("商品", 300)])
    table.pack(fill="both", expand=True)
    for index in range(100):
        table.insert("", "end", iid=str(index), values=(f"商品 {index}",))
    root.update()
    target = {"rows": table.canvas, "bar": table.scrollbar, "header": table.header}[surface]
    before = table.canvas.yview()[0]
    scroll_event(target, sequence, delta=down)
    root.update()
    after = table.canvas.yview()[0]
    assert before < after < 0.1
    scroll_event(target, sequence, delta=up)
    root.update()
    assert table.canvas.yview()[0] < after


@pytest.mark.parametrize("sequence,delta", [("<MouseWheel>", -120), ("<TouchpadScroll>", 65516)])
def test_log_scrolls_when_pointer_is_over_slim_scrollbar(root, scroll_event, sequence, delta):
    from quote_app.desktop_controls import SoftScrolledText

    log = SoftScrolledText(root)
    log.pack(fill="both", expand=True)
    log.insert("end", "log line\n" * 100)
    log.configure(state="disabled")
    root.update()
    scroll_event(log.vbar, sequence, delta=delta)
    root.update()
    assert 0 < log.yview()[0] < 0.1


def test_precise_scroll_preserves_small_deltas_and_separates_axes(root, scroll_event):
    from quote_app.desktop_scrolling import bind_scrolling, scroll_canvas

    canvas = tk.Canvas(root, scrollregion=(0, 0, 4000, 4000), highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    bind_scrolling(canvas, lambda event, **kw: scroll_canvas(canvas, event, **kw))
    root.update()
    canvas.xview_moveto(0)
    canvas.yview_moveto(0)
    # Tk packs signed X into the high word and signed Y into the low word.
    scroll_event(canvas, "<TouchpadScroll>", delta=65535)  # dy=-1
    root.update()
    assert canvas.yview()[0] == pytest.approx(1 / 4000)
    assert canvas.xview()[0] == 0
    scroll_event(canvas, "<TouchpadScroll>", delta=-1310720)  # dx=-20, dy=0
    root.update()
    assert canvas.xview()[0] == pytest.approx(20 / 4000)
    assert canvas.yview()[0] == pytest.approx(1 / 4000)
    scroll_event(canvas, "<MouseWheel>", delta=-120, state=1)  # Shift: horizontal
    root.update()
    assert canvas.xview()[0] == pytest.approx(60 / 4000)
    assert canvas.yview()[0] == pytest.approx(1 / 4000)


def test_horizontal_bar_precise_scroll_is_bounded_and_empty_track_is_inert(root, scroll_event):
    from quote_app.desktop_widgets import SlimScrollbar

    canvas = tk.Canvas(root, scrollregion=(0, 0, 4000, 400), highlightthickness=0)
    bar = SlimScrollbar(root, orient="horizontal", command=canvas.xview)
    bar.pack(side="bottom", fill="x")
    canvas.pack(fill="both", expand=True)
    canvas.configure(xscrollcommand=bar.set)
    root.update()
    canvas.xview_moveto(0)
    scroll_event(bar, "<TouchpadScroll>", delta=-1310720)  # dx=-20
    root.update()
    assert 0 < canvas.xview()[0] < 0.1
    canvas.xview_moveto(1)
    root.update()
    before = canvas.xview()
    scroll_event(bar, "<MouseWheel>", delta=-120)
    root.update()
    assert canvas.xview() == before
    canvas.configure(scrollregion=(0, 0, 10, 10))
    root.update()
    before = canvas.xview()
    scroll_event(bar, "<TouchpadScroll>", delta=-1310720)
    root.update()
    assert canvas.xview() == before


@pytest.mark.parametrize("orient", ["vertical", "horizontal"])
def test_slim_scrollbar_drag_reaches_end_without_exceeding_view(root, orient):
    from quote_app.desktop_widgets import SlimScrollbar

    calls = []
    bar = SlimScrollbar(root, orient=orient, command=lambda *args: calls.append(args))
    bar.pack(fill="y" if orient == "vertical" else "x", expand=True)
    root.update()
    bar.set(0, 0.25)
    bar.event_generate("<Button-1>", x=4, y=4)
    bar.event_generate("<B1-Motion>", x=10000, y=10000)
    root.update()
    assert calls[-1][0] == "moveto"
    assert calls[-1][1] == pytest.approx(0.75)
    bar.set(0, 1)
    assert not bar.find_all()


def test_popup_cleanup_and_keyboard_selection(root):
    from quote_app.desktop_controls import SoftSelect

    original_binding = root.bind("<ButtonPress-1>")
    variable = tk.StringVar(root, "全品牌")
    first = SoftSelect(root, textvariable=variable, values=("全品牌", "华为"))
    second = SoftSelect(root, textvariable=variable, values=("全品牌", "华为"))
    first.pack()
    second.pack()
    root.update()
    first.open_popup()
    root.update()
    assert first.popup and first.popup.winfo_ismapped()
    first.listbox.selection_clear(0, "end")
    first.listbox.selection_set(1)
    first.listbox.event_generate("<Return>")
    root.update()
    assert variable.get() == "华为"
    first.open_popup()
    second.open_popup()
    assert first.popup is None
    assert second.popup is not None
    second.destroy()
    root.update()
    assert root.bind("<ButtonPress-1>") == original_binding
    assert getattr(root, "_form_popup_owner", None) is None
    variable.set("全品牌")


def test_clicking_open_selector_closes_instead_of_reopening(root):
    from quote_app.desktop_controls import SoftSelect

    control = SoftSelect(root, textvariable=tk.StringVar(root, "全部"), values=("全部", "完成"))
    control.pack()
    root.update()
    control.open_popup()
    control.listbox.focus_force()
    root.update()
    control.event_generate("<Button-1>", x=20, y=20)
    root.update()
    control.event_generate("<ButtonRelease-1>", x=20, y=20)
    root.update()
    assert control.popup is None


def test_escape_returns_focus_to_selector_and_tab_reaches_next_field(root):
    from quote_app.desktop_controls import MonthPicker, SoftSelect

    first = MonthPicker(
        root, yearvariable=tk.StringVar(root, "2026"), monthvariable=tk.StringVar(root, "9")
    )
    second = SoftSelect(root, textvariable=tk.StringVar(root, "全部"), values=("全部", "完成"))
    first.pack()
    second.pack()
    root.update()
    first.open_popup()
    first.year_entry.entry.focus_force()
    root.update()
    first.year_entry.entry.event_generate("<Escape>")
    root.update()
    assert first.popup is None
    assert root.focus_get() is first
    first.event_generate("<Tab>")
    root.update()
    assert root.focus_get() is second

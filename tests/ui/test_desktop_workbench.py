"""Native navigation and output availability; opt in on a real desktop session."""

import os
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from quote_app.desktop_ui import DesktopWorkbench
from quote_app.desktop_state import TaskRow
from quote_app.paths import build_app_paths

pytestmark = pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Requires desktop WindowServer"
)


@pytest.fixture
def workbench(tmp_path):
    root = tk.Tk()
    root.withdraw()
    app = SimpleNamespace(root=root, app_paths=build_app_paths("Darwin", home=tmp_path))
    for name in ("base", "marketing", "bop", "output_dir", "year", "month", "brand_mode"):
        setattr(
            app,
            name + "_var",
            tk.StringVar(
                root,
                value="2026"
                if name == "year"
                else "9"
                if name == "month"
                else "全品牌"
                if name == "brand_mode"
                else "",
            ),
        )
    for name in (
        "run",
        "continue_current_task",
        "cancel_manual_action",
        "open_output_directory",
        "open_login_browser",
        "check_readiness",
        "_choose_file",
        "_choose_directory",
    ):
        setattr(app, name, lambda *args: None)
    view = DesktopWorkbench(
        app, title="Native test", credit="Design by Gudwin", modes=("全品牌", "荣耀")
    )
    yield view
    root.destroy()


def test_overview_tabs_preserve_inputs_and_running_channel_state(workbench):
    """Break caught: navigating rebuilt tabs loses imported paths or active rows."""
    view = workbench
    view.app.base_var.set("/tmp/base.xlsx")
    view.model.rows = [TaskRow("t1", channel="jd", price="0", outcome="price_found")]
    view.show_overview_tab("data")
    view.show_page("intelligence")
    view.show_page("overview")
    view.show_overview_tab("reports")
    view.show_overview_tab("tasks")
    assert view.app.base_var.get() == "/tmp/base.xlsx"
    assert view.model.rows[0].price == "0"
    assert view.overview_tab == "tasks"
    assert str(view.app.continue_button["state"]) == "disabled"


def test_report_actions_require_actual_files_and_clear_on_new_run(workbench, tmp_path: Path):
    """Break caught: a stale/missing output remains clickable after a new run."""
    view = workbench
    view.show_overview_tab("reports")
    assert str(view.report_buttons["quote"]["state"]) == "disabled"
    output = tmp_path / "quote.xlsx"
    output.write_bytes(b"test fixture")
    view.model.quote_path = output
    view.refresh()
    assert str(view.report_buttons["quote"]["state"]) == "normal"
    view.begin_run()
    view.show_page("overview")
    view.show_overview_tab("reports")
    assert str(view.report_buttons["quote"]["state"]) == "disabled"


def test_channel_dashboard_does_not_count_saved_price_as_saved_screenshot(workbench):
    """Break caught: partial observation is displayed as a completed capture."""
    view = workbench
    view.model.rows = [
        TaskRow("t1", channel="jd", price="2599", outcome="price_found", state="running")
    ]
    view.refresh()
    assert view.channel_counts["jd"].cget("text") == "0 / 1"
    assert view.reminder_counts["evidence"].cget("text") == "1 条"


def test_history_filter_keeps_persisted_state_and_only_matches_selected_status(workbench):
    """Break caught: filtering history changes saved task state or shows unrelated runs."""
    import sqlite3

    view = workbench
    path = view.app.app_paths.task_database
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE quotation_runs (run_id TEXT, state TEXT, created_at TEXT, updated_at TEXT)"
        )
        db.executemany(
            "INSERT INTO quotation_runs VALUES (?, ?, ?, ?)",
            [
                ("run-active", "running", "2026-09-09", "2026-09-09"),
                ("run-done", "completed", "2026-09-08", "2026-09-08"),
            ],
        )
    before = path.read_bytes()
    view.show_page("history")
    view.history_query.set("run-")
    view.history_state.set("已完成")
    view._filter_history()
    assert view.history_table.get_children() == ("run-done",)
    assert path.read_bytes() == before


def test_native_shortcuts_switch_pages_without_running_task(workbench):
    """Break caught: keyboard page navigation triggers a business action or the wrong view."""
    view = workbench
    view.root.deiconify()
    view.root.update()
    view.root.focus_force()
    view.root.event_generate("<Command-Key-2>")
    view.root.update()
    assert view.page == "intelligence"
    view.root.event_generate("<Command-Shift-Key-2>")
    view.root.update()
    assert view.page == "overview"
    assert view.overview_tab == "data"
    assert not view.model.running


@pytest.mark.parametrize("size", ["1280x850", "1000x720", "1280x1020"])
@pytest.mark.parametrize("populated", [False, True])
def test_native_pages_fit_window_and_scrolled_content_is_reachable(workbench, size, populated):
    """Break caught: actual widget geometry hides controls or clips content at supported sizes."""
    from quote_app.domain.models import QuoteRow, WebQuery

    view = workbench
    if populated:
        view.model.rows = [
            TaskRow(
                "fixture-1",
                "华为畅享 90 Pro Max",
                "official",
                "2199",
                "price_found",
                "succeeded",
                "complete",
                specification="256GB / 曜金黑",
            ),
            TaskRow(
                "fixture-2",
                "荣耀 500 Pro",
                "jd",
                "2599",
                "price_found",
                "running",
                error="CAPTURE_PERMISSION",
            ),
            TaskRow("fixture-3", "vivo X300", "tmall", state="waiting_for_login"),
        ]
        view.model.quote_rows = (
            QuoteRow(
                2,
                "fixture-material",
                cells={"AI": 2299, "AJ": 2249, "AK": 2199, "AH": 2199},
                web_query=WebQuery(
                    model_name="华为畅享 90 Pro Max", storage="256GB", color="曜金黑"
                ),
            ),
        )
        view.app.base_var.set("/tmp/本月报价基础表.xlsx")
    view.root.deiconify()
    view.root.geometry(size)
    view.root.update()
    for page in (
        "tasks",
        "data",
        "reports",
        "intelligence",
        "decision",
        "audit",
        "history",
        "settings",
    ):
        if page in ("tasks", "data", "reports"):
            view.show_overview_tab(page)
        else:
            view.show_page(page)
        view.root.update()
        issues = []
        _check_geometry(view.root, issues)
        assert not issues, f"{size}/{page}/populated={populated}: {issues}"
        for canvas in _scroll_canvases(view.body):
            content = canvas.winfo_children()[0]
            if page == "tasks" and size == "1280x850":
                assert content.winfo_height() <= canvas.winfo_height(), (
                    "Default overview must show all three agent cards"
                )
            canvas.yview_moveto(1.0)
            view.root.update_idletasks()
            assert canvas.winfo_height() >= 40, f"{page} scroll viewport is unusably small"
            assert content.winfo_y() + content.winfo_height() <= canvas.winfo_height() + 1
            canvas.yview_moveto(0)


def _scroll_canvases(widget):
    for child in widget.winfo_children():
        if child.winfo_class() == "Canvas" and child.winfo_children():
            yield child
        yield from _scroll_canvases(child)


@pytest.mark.parametrize("sequence,delta", [("<MouseWheel>", -120), ("<TouchpadScroll>", 65516)])
def test_page_scrolls_over_child_label_and_scrollbar(workbench, scroll_event, sequence, delta):
    view = workbench
    view.root.geometry("1000x720")
    view.show_overview_tab("data")
    view.root.deiconify()
    view.root.update()
    canvas = next(_scroll_canvases(view.body))
    content = canvas.winfo_children()[0]
    target = tk.Label(content, text="Wheel regression")
    target.grid(row=99, column=0)
    view.root.update()
    for widget in (target, canvas.master.grid_slaves(row=0, column=1)[0]):
        canvas.yview_moveto(0)
        view.root.update()
        before = canvas.yview()[0]
        scroll_event(widget, sequence, delta=delta)
        view.root.update()
        assert before < canvas.yview()[0] < 0.5


def test_scrolling_nested_log_does_not_also_move_outer_page(workbench, scroll_event):
    from quote_app.desktop_controls import SoftScrolledText

    view = workbench
    view.root.geometry("1000x720")
    view.show_overview_tab("data")
    view.root.deiconify()
    canvas = next(_scroll_canvases(view.body))
    content = canvas.winfo_children()[0]
    log = SoftScrolledText(content, height=3)
    log.grid(row=99, column=0)
    log.insert("end", "log line\n" * 100)
    view.root.update()
    canvas.yview_moveto(0)
    before = canvas.yview()
    scroll_event(log, "<MouseWheel>", delta=-120)
    view.root.update()
    assert log.yview()[0] > 0
    assert canvas.yview() == before


def _check_geometry(widget, issues):
    for child in widget.winfo_children():
        if not child.winfo_ismapped():
            continue
        if widget.winfo_class() != "Canvas":
            if (
                child.winfo_x() < -1
                or child.winfo_y() < -1
                or child.winfo_x() + child.winfo_width() > widget.winfo_width() + 1
                or child.winfo_y() + child.winfo_height() > widget.winfo_height() + 1
            ):
                issues.append((str(child), "outside parent"))
        if child.winfo_class() in ("Label", "TButton") and child.cget("text"):
            if (
                child.winfo_reqwidth() > child.winfo_width() + 2
                or child.winfo_reqheight() > child.winfo_height() + 2
            ):
                issues.append((child.cget("text"), "clipped text"))
        _check_geometry(child, issues)


def test_report_header_keeps_run_month_when_next_run_inputs_change(workbench, tmp_path):
    """Break caught: editing next month's inputs relabels an existing month's output."""
    view = workbench
    view.begin_run()
    view.model.running = False
    output = tmp_path / "September.xlsx"
    output.write_bytes(b"fixture")
    view.model.quote_path = output
    view.app.month_var.set("10")
    view.show_overview_tab("reports")
    assert "2026年9月" in view.period.cget("text")
    assert "2026年10月" not in view.period.cget("text")
    view.show_overview_tab("data")
    assert "2026年10月" in view.period.cget("text")
    view.begin_run()
    assert "2026年10月" in view.period.cget("text")


def test_native_card_background_covers_the_full_padded_frame(workbench):
    """Break caught: Tk frame padding insets and clips a decorative card background."""
    view = workbench
    view.root.deiconify()
    view.root.update()
    panel = view.metrics_frame.winfo_children()[0]
    assert panel.border.winfo_x() == 0
    assert panel.border.winfo_y() == 0
    assert panel.border.winfo_width() == panel.winfo_width()
    assert panel.border.winfo_height() == panel.winfo_height()


def test_taller_window_gives_activity_log_more_room_without_losing_state(workbench):
    view = workbench
    view.root.deiconify()
    view.root.geometry("1280x850")
    view.root.update()
    view.append_log("保留当前运行记录")
    before_text = view.app.status.get("1.0", "end")
    before_height = view.app.status.winfo_height()
    view.app.continue_button.configure(state="normal")
    view.root.geometry("1280x1020")
    view.root.update()
    assert view.app.status.winfo_height() > before_height
    if view.root.winfo_height() >= 1000:
        assert view.app.status.winfo_height() >= before_height * 3
    assert view.app.status.get("1.0", "end") == before_text
    assert str(view.app.continue_button["state"]) == "normal"
    view.show_overview_tab("reports")
    view.root.update()
    assert view.app.status.winfo_height() > before_height
    if view.root.winfo_height() >= 1000:
        assert view.app.status.winfo_height() >= before_height * 3
    view.root.geometry("1000x720")
    view.root.update()
    assert view.app.status.get("1.0", "end") == before_text


def test_rich_rows_keep_record_identity_across_mouse_keyboard_filter_and_refresh(workbench):
    """New row renderer must open the selected record, never a neighbouring product."""
    view = workbench
    view.model.rows = [
        TaskRow(
            f"t{i}", f"商品 {i}", "jd" if i % 2 else "tmall", price=str(i), specification="256GB"
        )
        for i in range(1000)
    ]
    view.show_page("intelligence")
    view.root.deiconify()
    view.root.update()
    table = view.table
    table.canvas.event_generate("<Button-1>", x=60, y=90)
    view.root.update()
    assert table.selection() == ("t1",)
    assert view.detail_title.cget("text") == "商品 1"
    table._step(1)
    view.root.update()
    assert view.detail_title.cget("text") == "商品 2"
    view.refresh()
    view.root.update()
    assert table.selection() == ("t2",)
    assert len(table.canvas.find_all()) < 200  # draw visible rows, not 1,000 records
    view._filter("京东")
    view.root.update()
    assert table.selection() == ("t1",)
    assert view.detail_title.cget("text") == "商品 1"
    assert all(int(key[1:]) % 2 for key in table.get_children())
    view._filter("官网")
    view.root.update()
    assert table.selection() == ()
    assert view.detail_title.cget("text") == "暂无选中商品"


def test_rounded_actions_obey_disabled_state_and_keep_callbacks(workbench):
    view = workbench
    calls = []
    control = view.app.continue_button
    control.configure(command=lambda: calls.append("continued"))
    control.invoke()
    assert not calls
    control.configure(state="normal")
    control.invoke()
    assert calls == ["continued"]
    view.root.deiconify()
    view.root.geometry("1280x1020")
    view.root.update()
    controls = (control, view.app.cancel_button, view.app.open_button)
    assert len({c.winfo_width() for c in controls}) == 1, (
        view.root.winfo_height(),
        view._log_compact,
        [c.grid_info() for c in controls],
    )
    assert controls[0].winfo_y() < controls[1].winfo_y() < controls[2].winfo_y()
    view.root.geometry("1000x720")
    view.root.update()
    assert len({c.winfo_y() for c in controls}) == 1
    control.configure(state="disabled")
    control.invoke()
    assert calls == ["continued"]


def test_bundled_artwork_is_cached_and_missing_assets_do_not_trigger_downloads(workbench):
    view = workbench
    for name in ("华为", "OPPO", "vivo", "荣耀", "小米", "iPhone"):
        image = view.artwork.channel("official", name, size=24)
        assert image is not None
        assert image is view.artwork.channel("official", name, size=24)
    assert view.artwork.phone(42) is view.artwork.phone(42)
    assert view.artwork.channel("official", "未知品牌") is not None
    assert view.artwork.get("ui-brands/absent", 24) is None


@pytest.mark.parametrize(
    "values, expected",
    [
        (("2199.00", "2199", "2249", "2199.0"), (True, False, True)),
        (("无", "无", "无", "无"), (False, False, False)),
        (("0", "0.00", "1", "2"), (True, False, False)),
    ],
)
def test_price_highlight_matches_numeric_output_only(workbench, values, expected):
    from quote_app.domain.models import QuoteRow, WebQuery
    from quote_app.desktop_ui import BLUE, LINE

    view = workbench
    view.model.quote_rows = (
        QuoteRow(
            1,
            "sku",
            cells=dict(zip(("AH", "AK", "AJ", "AI"), values)),
            web_query=WebQuery(model_name="商品"),
        ),
    )
    view.show_page("decision")
    assert tuple(tile.cget("highlightbackground") == BLUE for tile in view.price_tiles) == expected
    view._filter("含异常")
    assert all(tile.cget("highlightbackground") == LINE for tile in view.price_tiles)


def test_history_and_settings_do_not_keep_space_from_hidden_data_controls(workbench):
    view = workbench
    view.root.deiconify()
    view.root.update()
    for page in ("history", "settings"):
        view.show_overview_tab("data")
        view.root.update()
        view.show_page(page)
        view.root.update()
        assert not view.context.winfo_ismapped()
        header_bottom = view.heading.master.winfo_y() + view.heading.master.winfo_height()
        assert view.body.winfo_y() - header_bottom < 35
    view.show_overview_tab("data")
    view.root.update()
    assert view.context.winfo_ismapped()
    assert view.run_controls.winfo_ismapped()


@pytest.mark.parametrize("size", ["1000x720", "1280x850"])
@pytest.mark.parametrize("page", ["intelligence", "decision", "audit"])
def test_real_prices_remain_single_line_and_fit_after_selection_and_resize(workbench, size, page):
    """Break caught: self-referential wrapping collapses prices into a vertical stack."""
    from tkinter import font
    from quote_app.domain.models import QuoteRow, WebQuery

    view = workbench
    view.root.deiconify()
    view.root.geometry(size)
    view.show_page(page)
    view.root.update()
    view.model.rows = [
        TaskRow("price-1", "华为 Mate 80", "official", "5499", "price_found", "succeeded")
    ]
    view.model.quote_rows = (
        QuoteRow(
            2,
            "material-1",
            cells={"AK": 5499, "AJ": 5999, "AI": 5799, "AH": 5499},
            web_query=WebQuery(model_name="华为 Mate 80"),
        ),
    )
    view.refresh()
    view.root.update()
    prices = view.detail_values if page == "decision" else [view.detail_values[1]]
    if page == "decision":
        prices = prices + [view.detail_rows[0]]
    expected = ["¥5,499", "¥5,999", "¥5,799", "¥5,499"] if page == "decision" else ["¥5,499"]
    for target_size in (size, "1280x850" if size == "1000x720" else "1000x720", size):
        view.root.geometry(target_size)
        view.refresh()
        view.root.update()
        for widget, amount in zip(prices, expected):
            assert widget.cget("text") == amount
            text_font = font.Font(root=view.root, font=widget.cget("font"))
            assert widget.winfo_height() <= text_font.metrics("linespace") + 8, (
                page, target_size, amount, widget.winfo_width(), widget.winfo_height()
            )
            assert widget.winfo_width() >= text_font.measure(amount)
            assert widget.winfo_x() + widget.winfo_width() <= widget.master.winfo_width()

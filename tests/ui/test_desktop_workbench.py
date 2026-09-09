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
    assert view.app.status.winfo_height() >= before_height * 3
    assert view.app.status.get("1.0", "end") == before_text
    assert str(view.app.continue_button["state"]) == "normal"
    view.show_overview_tab("reports")
    view.root.update()
    assert view.app.status.winfo_height() >= before_height * 3
    view.root.geometry("1000x720")
    view.root.update()
    assert view.app.status.get("1.0", "end") == before_text

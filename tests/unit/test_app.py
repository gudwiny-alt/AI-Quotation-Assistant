from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from quote_app.domain.models import InputPaths, Issue, QuoteMonth
from quote_app.excel.report_writer import RunSummary
from quote_app.services.readiness import ReadinessCheck
from quote_app.services.core_pipeline import CorePipelineError, CoreRunResult
from quote_app.tasks.models import WebsiteChannel, WebsiteTask
from quote_app.tasks.repository import AttemptToken


def _result() -> CoreRunResult:
    return CoreRunResult(
        quote_path=Path("/tmp/报价表.xlsx"),
        report_path=Path("/tmp/执行报告.xlsx"),
        summary=RunSummary(
            total_rows=5,
            completed_rows=1,
            partial_rows=2,
            failed_rows=1,
            unsupported_rows=1,
            manual_supplement_rows=4,
        ),
        rows=[],
    )


def _ready_check(_paths: object) -> ReadinessCheck:
    return ReadinessCheck(())


def test_default_quote_month_uses_the_injected_current_month() -> None:
    from quote_app.app import parse_cli_arguments

    request = parse_cli_arguments(
        [
            "--base",
            "base.xlsx",
            "--marketing",
            "marketing.xlsx",
            "--bop",
            "bop.xlsx",
            "--output-dir",
            "outputs",
        ],
        current_month=QuoteMonth(2026, 7),
    )

    assert request.quote_month == QuoteMonth(2026, 7)


def test_beta_notice_describes_execution_order_and_manual_resume() -> None:
    from quote_app.app import BETA_NOTICE

    assert BETA_NOTICE == (
        "执行顺序：品牌官网、天猫、京东；遇到登录或验证页面时，"
        "人工登录完成后点击“继续当前任务”按钮。"
    )


def test_app_build_label_uses_the_approved_short_title() -> None:
    from quote_app.app import APP_BUILD_LABEL

    assert APP_BUILD_LABEL == "终端福建分公司报价决策智能体 2026.09.04.170 · UI预览版"


def test_desktop_full_request_reuses_per_user_browser_and_task_state(
    tmp_path: Path,
) -> None:
    from quote_app.app import make_full_pipeline_request
    from quote_app.paths import build_app_paths

    full_request = make_full_pipeline_request(
        paths=InputPaths(
            tmp_path / "base.xlsx",
            tmp_path / "marketing.xlsx",
            tmp_path / "bop.xlsx",
            tmp_path / "outputs",
        ),
        quote_month=QuoteMonth(2026, 8),
        app_paths=build_app_paths("Darwin", home=tmp_path / "user"),
    )

    assert full_request.browser_profile_dir == (
        tmp_path / "user" / "Library" / "Application Support" / "福建移动铺货报价助手" / "browser-profile"
    )
    assert full_request.database_path.name == "tasks.sqlite3"
    assert full_request.evidence_dir.name == "evidence"


def test_desktop_request_passes_honor_only_mode(tmp_path: Path) -> None:
    from quote_app.app import make_full_pipeline_request
    from quote_app.paths import build_app_paths

    request = make_full_pipeline_request(
        paths=InputPaths(
            tmp_path / "base.xlsx",
            tmp_path / "marketing.xlsx",
            tmp_path / "bop.xlsx",
            tmp_path / "outputs",
        ),
        quote_month=QuoteMonth(2026, 8),
        app_paths=build_app_paths("Darwin", home=tmp_path / "user"),
        selected_brand="HONOR",
    )

    assert request.selected_brand == "HONOR"


def test_desktop_request_passes_honor_official_scope(tmp_path: Path) -> None:
    from quote_app.app import make_full_pipeline_request
    from quote_app.paths import build_app_paths

    request = make_full_pipeline_request(
        paths=InputPaths(
            tmp_path / "base.xlsx",
            tmp_path / "marketing.xlsx",
            tmp_path / "bop.xlsx",
            tmp_path / "outputs",
        ),
        quote_month=QuoteMonth(2026, 8),
        app_paths=build_app_paths("Darwin", home=tmp_path / "user"),
        selected_brand="HONOR",
        selected_channels=frozenset({WebsiteChannel.OFFICIAL}),
    )

    assert request.selected_brand == "HONOR"
    assert request.selected_channels == frozenset({WebsiteChannel.OFFICIAL})


def test_desktop_run_sends_selected_inputs_to_the_complete_pipeline(
    tmp_path: Path,
) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths
    from quote_app.services.full_pipeline import FullPipelineRequest

    received: list[FullPipelineRequest] = []
    statuses: list[str] = []
    app = object.__new__(QuoteApp)
    root = _GuiRoot()
    app.root = root
    app.pipeline = lambda request: received.append(request) or _result()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app.base_var = SimpleNamespace(get=lambda: str(tmp_path / "base.xlsx"))
    app.marketing_var = SimpleNamespace(get=lambda: str(tmp_path / "marketing.xlsx"))
    app.bop_var = SimpleNamespace(get=lambda: str(tmp_path / "bop.xlsx"))
    app.output_dir_var = SimpleNamespace(get=lambda: str(tmp_path / "outputs"))
    app.year_var = SimpleNamespace(get=lambda: "2026")
    app.month_var = SimpleNamespace(get=lambda: "8")
    app.brand_mode_var = SimpleNamespace(get=lambda: "仅 HONOR")
    app.open_button = _GuiButton()
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._pipeline_generation = 0
    app._active_pipeline_generation = None
    app._closing = False
    app._destroyed = False
    app._last_output_dir = None
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.readiness_checker = _ready_check

    app.run()
    assert app._pipeline_thread is not None
    app._pipeline_thread.join(timeout=5)
    root.run_next()

    assert len(received) == 1
    assert received[0].quote_month == QuoteMonth(2026, 8)
    assert received[0].selected_brand == "HONOR"
    assert received[0].browser_profile_dir == app.app_paths.browser_profile
    assert received[0].database_path == app.app_paths.task_database
    assert app.open_button.state == "normal"
    assert "报价表：/tmp/报价表.xlsx" in statuses[-1]


def test_gui_run_marks_honor_closed_loop_mode_in_status(tmp_path: Path) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths
    from quote_app.services.full_pipeline import FullPipelineRequest

    received: list[FullPipelineRequest] = []
    statuses: list[str] = []
    app = object.__new__(QuoteApp)
    root = _GuiRoot()
    app.root = root
    app.pipeline = lambda request: received.append(request) or _result()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app.base_var = SimpleNamespace(get=lambda: str(tmp_path / "base.xlsx"))
    app.marketing_var = SimpleNamespace(get=lambda: str(tmp_path / "marketing.xlsx"))
    app.bop_var = SimpleNamespace(get=lambda: str(tmp_path / "bop.xlsx"))
    app.output_dir_var = SimpleNamespace(get=lambda: str(tmp_path / "outputs"))
    app.year_var = SimpleNamespace(get=lambda: "2026")
    app.month_var = SimpleNamespace(get=lambda: "8")
    app.brand_mode_var = SimpleNamespace(get=lambda: "仅 HONOR")
    app.open_button = _GuiButton()
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._pipeline_generation = 0
    app._active_pipeline_generation = None
    app._closing = False
    app._destroyed = False
    app._last_output_dir = None
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.readiness_checker = _ready_check

    app.run()

    assert "荣耀闭环穿测模式（仅输出全部荣耀行）" in statuses[-1]
    assert app._pipeline_thread is not None
    app._pipeline_thread.join(timeout=5)
    root.run_next()
    assert received[0].selected_brand == "HONOR"


@pytest.mark.parametrize(
    ("mode", "expected_brand", "expected_status"),
    (
        ("全品牌", None, "全品牌全站模式（官网、京东、天猫）"),
        ("荣耀", "HONOR", "荣耀全站模式（官网、京东、天猫）"),
        ("小米", "小米", "小米全站模式（官网、京东、天猫）"),
        ("OPPO", "欧珀", "OPPO 全站模式（官网、京东、天猫）"),
        ("vivo", "维沃", "vivo 全站模式（官网、京东、天猫）"),
        ("华为", "华为", "华为全站模式（官网、京东、天猫）"),
        ("苹果", "苹果", "苹果全站模式（官网、京东、天猫）"),
    ),
)
def test_gui_run_modes_select_brand_and_all_three_channels(
    mode: str,
    expected_brand: str | None,
    expected_status: str,
) -> None:
    """Break caught: a selected scope omits JD/Tmall or runs the wrong brand."""
    from quote_app.app import QuoteApp

    app = object.__new__(QuoteApp)
    app.brand_mode_var = SimpleNamespace(get=lambda: mode)

    assert app._selected_brand_from_mode() == expected_brand
    assert app._selected_channels_from_mode() is None
    assert app._run_mode_status_prefix() == expected_status


def test_gui_approved_run_modes_are_ordered_with_all_brands_first() -> None:
    """Break caught: the selector defaults to a single brand or exposes test modes."""
    from quote_app.app import _RUN_MODE_OPTIONS

    assert _RUN_MODE_OPTIONS == (
        "全品牌",
        "荣耀",
        "小米",
        "OPPO",
        "vivo",
        "华为",
        "苹果",
    )


def test_gui_build_shows_author_credit_and_uses_readonly_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refreshed shell preserves valid selectors, actions and authorship."""
    from quote_app import app as app_module
    from quote_app import desktop_ui
    from types import SimpleNamespace

    widgets: list[dict[str, object]] = []
    geometries: list[str] = []

    def tk_call(*args):
        if args == ("package", "provide", "Tk"):
            return "9.0.4"
        if args == ("package", "vcompare", "9.0.4", "9.0"):
            return 1
        if args == ("tk", "windowingsystem"):
            return "aqua"
        raise desktop_ui.tk.TclError("Headless fixture does not create native images")

    class Widget:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            widgets.append(kwargs)
            self.tk = SimpleNamespace(call=tk_call)
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None
        def winfo_children(self):
            return []
        def geometry(self, value: str) -> None:
            geometries.append(value)
        def winfo_screenheight(self):
            return 1440

    for module, names in (
        (desktop_ui.tk, ("Frame", "Label", "Button", "Canvas", "Text")),
        (desktop_ui.ttk, ("Frame", "Entry", "Button", "Separator", "Combobox", "Style", "Scrollbar", "Treeview")),
        (desktop_ui, ("SoftScrolledText", "MonthPicker", "SoftSelect", "SoftEntry", "SlimScrollbar")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, Widget)
    monkeypatch.setattr(desktop_ui, "RoundedCard", Widget)
    monkeypatch.setattr(desktop_ui, "SoftButton", Widget)
    monkeypatch.setattr(desktop_ui, "Artwork", lambda _root: SimpleNamespace(
        channel=lambda *args, **kwargs: None, get=lambda *args, **kwargs: None
    ))
    app = object.__new__(app_module.QuoteApp)
    app.root = Widget()
    for name in ("base", "marketing", "bop", "output_dir", "year", "month", "brand_mode"):
        setattr(app, name + "_var", SimpleNamespace(get=lambda: "", trace_add=lambda *_args: None))
    app._build()
    assert geometries == ["1280x1020"]
    assert any(item.get("text") == "Design by Gudwin" for item in widgets)
    selectors = [item for item in widgets if "values" in item]
    dates = [item for item in widgets if "yearvariable" in item]
    assert dates[0]["yearvariable"] is app.year_var
    assert dates[0]["monthvariable"] is app.month_var
    assert [item["state"] for item in selectors] == ["readonly"]
    assert selectors[0]["textvariable"] is app.brand_mode_var
    assert selectors[0]["values"] == ("全品牌", "荣耀", "小米", "OPPO", "vivo", "华为", "苹果")
    assert any(item.get("command") == app.run for item in widgets)
    assert any(item.get("command") == app.continue_current_task for item in widgets)
    assert any(item.get("command") == app.cancel_manual_action for item in widgets)


@pytest.mark.parametrize("mode", ("全部品牌", "测试品牌"))
def test_gui_rejects_unknown_run_mode(mode: str) -> None:
    from quote_app.app import InputValidationError, QuoteApp

    app = object.__new__(QuoteApp)
    app.brand_mode_var = SimpleNamespace(get=lambda: mode)

    with pytest.raises(InputValidationError, match="当前运行范围不受支持"):
        app._selected_brand_from_mode()


def test_gui_readiness_check_blocks_pipeline_before_worker_creation(
    tmp_path: Path,
) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths
    from quote_app.services.readiness import ReadinessCheck, ReadinessItem

    statuses: list[str] = []
    received: list[object] = []
    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    app.pipeline = received.append
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app.base_var = SimpleNamespace(get=lambda: str(tmp_path / "base.xlsx"))
    app.marketing_var = SimpleNamespace(get=lambda: str(tmp_path / "marketing.xlsx"))
    app.bop_var = SimpleNamespace(get=lambda: str(tmp_path / "bop.xlsx"))
    app.output_dir_var = SimpleNamespace(get=lambda: str(tmp_path / "outputs"))
    app.year_var = SimpleNamespace(get=lambda: "2026")
    app.month_var = SimpleNamespace(get=lambda: "8")
    app.brand_mode_var = SimpleNamespace(get=lambda: "仅 HONOR")
    app.open_button = _GuiButton()
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._pipeline_generation = 0
    app._active_pipeline_generation = None
    app._closing = False
    app._destroyed = False
    app._last_output_dir = None
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.readiness_checker = lambda _paths: ReadinessCheck(
        (ReadinessItem("screen_capture", "未开启屏幕与系统音频录制权限", False),)
    )

    app.run()

    assert received == []
    assert app._pipeline_thread is None
    assert statuses == ["运行前检查未通过：未开启屏幕与系统音频录制权限"]


def test_gui_check_readiness_reports_current_items(tmp_path: Path) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths
    from quote_app.services.readiness import ReadinessCheck, ReadinessItem

    statuses: list[str] = []
    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app._closing = False
    app._destroyed = False
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.readiness_checker = lambda _paths: ReadinessCheck(
        (
            ReadinessItem("screen_capture", "屏幕与系统音频录制权限", True),
            ReadinessItem("accessibility", "未开启辅助功能权限", False),
        )
    )

    app.check_readiness()

    assert statuses == [
        "运行前检查（第1次）未通过：\n✓ 屏幕与系统音频录制权限\n✗ 未开启辅助功能权限"
    ]


def test_gui_schedules_readiness_check_when_desktop_opens() -> None:
    from quote_app.app import QuoteApp

    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    checked: list[bool] = []
    app.check_readiness = lambda: checked.append(True)

    app._schedule_initial_readiness_check()

    assert checked == []
    app.root.run_next()
    assert checked == [True]


def test_gui_readiness_button_rechecks_and_shows_the_latest_state(
    tmp_path: Path,
) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths
    from quote_app.services.readiness import ReadinessCheck, ReadinessItem

    statuses: list[str] = []
    checks = iter(
        (
            ReadinessCheck(
                (
                    ReadinessItem(
                        "screen_capture",
                        "屏幕与系统音频录制权限",
                        False,
                        "未开启屏幕与系统音频录制权限",
                    ),
                )
            ),
            ReadinessCheck(
                (ReadinessItem("screen_capture", "屏幕与系统音频录制权限", True),)
            ),
        )
    )
    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app._closing = False
    app._destroyed = False
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.readiness_checker = lambda _paths: next(checks)

    app.check_readiness()
    app.check_readiness()

    assert "第1次" in statuses[0]
    assert "✗ 未开启屏幕与系统音频录制权限" in statuses[0]
    assert "第2次" in statuses[1]
    assert "✓ 屏幕与系统音频录制权限" in statuses[1]


def test_gui_continue_current_task_releases_waiting_browser_controller() -> None:
    from quote_app.app import QuoteApp
    from quote_app.services.web_run import WebsiteRunController
    from quote_app.tasks.scheduler import ManualActionEvent

    statuses: list[str] = []
    controller = WebsiteRunController()
    controller.publish_manual_action(
        ManualActionEvent(
            task=WebsiteTask(
                task_id="waiting",
                run_id="run-1",
                source_row_number=2,
                output_row_number=2,
                material_code="CODE-2",
                brand="HONOR",
                model_name="荣耀Magic8",
                ram="16GB",
                storage="512GB",
                color="天青釉",
                channel=WebsiteChannel.TMALL,
            ),
            token=AttemptToken("waiting", 0, 1),
            site="天猫",
            reason="需要人工完成安全验证",
        )
    )
    app = object.__new__(QuoteApp)
    app._website_controller = controller
    app.continue_button = _GuiButton()
    app.cancel_button = _GuiButton()
    app._widgets_available = lambda: True
    app._set_status = statuses.append

    app.continue_current_task()

    assert controller.wait_for_resolution() is True
    assert app.continue_button.state == "disabled"
    assert app.cancel_button.state == "disabled"
    assert statuses == ["正在继续当前任务：天猫 / 荣耀Magic8"]


def _manual_action():
    from quote_app.tasks.scheduler import ManualActionEvent

    task = WebsiteTask(
        task_id="waiting",
        run_id="run-1",
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-2",
        brand="HONOR",
        model_name="荣耀Magic8",
        ram="16GB",
        storage="512GB",
        color="天青釉",
        channel=WebsiteChannel.TMALL,
    )
    return ManualActionEvent(
        task=task,
        token=AttemptToken("waiting", 0, 1),
        site="天猫",
        reason="需要人工完成安全验证",
    )


def test_controller_shutdown_after_manual_action_resolves_false() -> None:
    from quote_app.services.web_run import WebsiteRunController

    controller = WebsiteRunController()
    controller.publish_manual_action(_manual_action())

    controller.request_shutdown()

    assert controller.wait_for_resolution() is False


def test_controller_shutdown_before_manual_action_resolves_future_publish_false() -> None:
    from quote_app.services.web_run import WebsiteRunController

    controller = WebsiteRunController()
    controller.request_shutdown()

    controller.publish_manual_action(_manual_action())

    assert controller.wait_for_resolution() is False


def test_controller_shutdown_is_idempotent_before_and_after_publish() -> None:
    from quote_app.services.web_run import WebsiteRunController

    controller = WebsiteRunController()
    controller.request_shutdown()
    controller.request_shutdown()
    controller.publish_manual_action(_manual_action())
    controller.request_shutdown()

    assert controller.wait_for_resolution() is False


@pytest.mark.parametrize(
    ("event_name", "data", "expected"),
    [
        (
            "progress",
            {"attempt_number": 1, "channel": "official"},
            "正在处理：官网 / 荣耀Magic8",
        ),
        (
            "observation",
            {"outcome": "price_found", "price": "4999"},
            "已保存阶段结果：官网 / 荣耀Magic8 / 价格4999 / 截图待补",
        ),
        (
            "result",
            {"outcome": "price_found", "price": "4999"},
            "已保存阶段结果：官网 / 荣耀Magic8 / 价格4999 / "
            "截图已补 / 官网渠道完成",
        ),
        (
            "waiting_for_login",
            {"site": "jd", "condition": "login_required"},
            "网站任务已暂停：京东 / 荣耀Magic8 / 等待登录或安全验证",
        ),
        (
            "technical_failure",
            {
                "error_code": "PAGE_TIMEOUT",
                "retryable": True,
                "retry_remaining": 1,
            },
            "网站任务暂时失败：官网 / 荣耀Magic8 / PAGE_TIMEOUT；"
            "将自动重试（剩余1次）",
        ),
        (
            "technical_failure",
            {
                "error_code": "CAPTURE_PERMISSION",
                "retryable": False,
                "retry_remaining": 0,
            },
            "网站任务技术失败：官网 / 荣耀Magic8 / CAPTURE_PERMISSION",
        ),
    ],
)
def test_full_pipeline_ui_boundary_decorates_legacy_events_for_every_status(
    event_name: str,
    data: dict[str, object],
    expected: str,
) -> None:
    from quote_app.app import format_worker_event_status
    from quote_app.browser.worker import WorkerEvent
    from quote_app.services.full_pipeline import _ui_event_sink_for_tasks

    task = WebsiteTask(
        task_id="official-task",
        run_id="run-stage",
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-2",
        brand="HONOR",
        model_name="荣耀Magic8",
        ram="16GB",
        storage="512GB",
        color="天青釉",
        channel=WebsiteChannel.OFFICIAL,
    )
    decorated = []
    sink = _ui_event_sink_for_tasks((task,), decorated.append)
    assert sink is not None
    source = WorkerEvent(event_name, "run-stage", task.task_id, data)

    sink(source)

    assert dict(source.data) == data
    assert format_worker_event_status(decorated[0]) == expected
    sink(WorkerEvent(event_name, "run-stage", "unknown-task", data))
    assert len(decorated) == 1


def test_desktop_run_executes_complete_pipeline_off_the_tk_thread(
    tmp_path: Path,
) -> None:
    from quote_app.app import APP_BUILD_LABEL, QuoteApp
    from quote_app.paths import build_app_paths

    statuses: list[str] = []
    worker_threads: list[int] = []
    root = _GuiRoot()
    app = object.__new__(QuoteApp)
    app.root = root
    app.pipeline = lambda _request: worker_threads.append(threading.get_ident()) or _result()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app.base_var = SimpleNamespace(get=lambda: str(tmp_path / "base.xlsx"))
    app.marketing_var = SimpleNamespace(get=lambda: str(tmp_path / "marketing.xlsx"))
    app.bop_var = SimpleNamespace(get=lambda: str(tmp_path / "bop.xlsx"))
    app.output_dir_var = SimpleNamespace(get=lambda: str(tmp_path / "outputs"))
    app.year_var = SimpleNamespace(get=lambda: "2026")
    app.month_var = SimpleNamespace(get=lambda: "8")
    app.brand_mode_var = SimpleNamespace(get=lambda: "仅 HONOR")
    app.open_button = _GuiButton()
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._pipeline_generation = 0
    app._active_pipeline_generation = None
    app._closing = False
    app._destroyed = False
    app._last_output_dir = None
    app._set_status = statuses.append
    app.readiness_checker = _ready_check

    app.run()
    assert app._pipeline_thread is not None
    app._pipeline_thread.join(timeout=5)

    assert worker_threads == [app._pipeline_thread.ident]
    assert worker_threads[0] != threading.get_ident()
    assert statuses == [
        "荣耀闭环穿测模式（仅输出全部荣耀行）\n"
        f"{APP_BUILD_LABEL}\n自动报价运行中"
    ]
    root.run_next()
    assert "报价表：/tmp/报价表.xlsx" in statuses[-1]
    assert app.open_button.state == "normal"


def test_gui_stage_result_is_queued_and_rendered_on_the_tk_thread(
    tmp_path: Path,
) -> None:
    """Break caught: worker events either skip stage copy or touch Tk directly."""
    from quote_app.app import QuoteApp
    from quote_app.browser.worker import WorkerEvent
    from quote_app.paths import build_app_paths

    statuses: list[str] = []
    status_threads: list[int] = []
    event_published = threading.Event()
    release_worker = threading.Event()

    def pipeline(request: object):
        event_sink = getattr(request, "event_sink")
        assert event_sink is not None
        event_sink(
            WorkerEvent(
                "observation",
                "run-stage",
                "official-task",
                {
                    "channel": "official",
                    "model_name": "荣耀Magic8",
                    "outcome": "price_found",
                    "price": "4999",
                },
            )
        )
        event_published.set()
        assert release_worker.wait(timeout=5)
        return _result()

    app = object.__new__(QuoteApp)
    root = _GuiRoot()
    app.root = root
    app.pipeline = pipeline
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app.base_var = SimpleNamespace(get=lambda: str(tmp_path / "base.xlsx"))
    app.marketing_var = SimpleNamespace(get=lambda: str(tmp_path / "marketing.xlsx"))
    app.bop_var = SimpleNamespace(get=lambda: str(tmp_path / "bop.xlsx"))
    app.output_dir_var = SimpleNamespace(get=lambda: str(tmp_path / "outputs"))
    app.year_var = SimpleNamespace(get=lambda: "2026")
    app.month_var = SimpleNamespace(get=lambda: "8")
    app.brand_mode_var = SimpleNamespace(get=lambda: "仅 HONOR")
    app.open_button = _GuiButton()
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._pipeline_generation = 0
    app._active_pipeline_generation = None
    app._closing = False
    app._destroyed = False
    app._last_output_dir = None
    app._widgets_available = lambda: True

    def set_status(message: str) -> None:
        status_threads.append(threading.get_ident())
        statuses.append(message)

    app._set_status = set_status
    app.readiness_checker = _ready_check

    app.run()
    assert event_published.wait(timeout=5)
    assert statuses[-1].endswith("自动报价运行中")

    root.run_next()

    assert statuses[-1] == (
        "已保存阶段结果：官网 / 荣耀Magic8 / 价格4999 / 截图待补"
    )
    assert set(status_threads) == {threading.get_ident()}

    release_worker.set()
    assert app._pipeline_thread is not None
    app._pipeline_thread.join(timeout=5)
    root.run_next()


def test_gui_stage_result_marks_screenshot_and_channel_complete() -> None:
    """Break caught: a completed capture remains displayed as screenshot pending."""
    from quote_app.app import format_worker_event_status
    from quote_app.browser.worker import WorkerEvent

    status = format_worker_event_status(
        WorkerEvent(
            "result",
            "run-stage",
            "official-task",
            {
                "channel": "official",
                "model_name": "荣耀Magic8",
                "outcome": "price_found",
                "price": "4999",
            },
        )
    )

    assert status == (
        "已保存阶段结果：官网 / 荣耀Magic8 / 价格4999 / "
        "截图已补 / 官网渠道完成"
    )


def test_desktop_gui_displays_the_specific_pipeline_error_after_web_work() -> None:
    """Break caught: a publication error is hidden behind an unhelpful generic status."""
    from quote_app.app import QuoteApp

    statuses: list[str] = []
    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    app.open_button = _GuiButton()
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_results.put((1, ValueError("报价模板文件不存在")))
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._active_pipeline_generation = 1
    app._closing = False
    app._destroyed = False
    app._widgets_available = lambda: True
    app._set_status = statuses.append

    app._poll_pipeline_result()

    assert statuses == ["自动报价未完成：报价模板文件不存在"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            [
                "--base",
                " ",
                "--marketing",
                "marketing.xlsx",
                "--bop",
                "bop.xlsx",
                "--output-dir",
                "outputs",
            ],
            "基础表不能为空",
        ),
        (
            [
                "--base",
                "base.xlsx",
                "--marketing",
                "marketing.xlsx",
                "--bop",
                "bop.xlsx",
                "--output-dir",
                "outputs",
                "--month",
                "13",
            ],
            "报价月份不合法",
        ),
        (
            [
                "--base",
                "base.xlsx",
                "--marketing",
                "marketing.xlsx",
                "--bop",
                "bop.xlsx",
                "--output-dir",
                "outputs",
                "--year",
                "0",
            ],
            "报价年份不合法",
        ),
        (
            [
                "--base",
                "base.xlsx",
                "--marketing",
                "marketing.xlsx",
                "--bop",
                "bop.xlsx",
                "--output-dir",
                "outputs",
                "--year",
                "-1",
            ],
            "报价年份不合法",
        ),
    ],
)
def test_cli_input_validation_rejects_blank_paths_and_invalid_month(
    arguments: list[str], message: str
) -> None:
    from quote_app.app import InputValidationError, parse_cli_arguments

    with pytest.raises(InputValidationError, match=message):
        parse_cli_arguments(arguments, current_month=QuoteMonth(2026, 7))


def test_cli_calls_injected_pipeline_and_prints_output_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from quote_app.app import APP_BUILD_LABEL, BETA_NOTICE, run_cli

    received: dict[str, object] = {}

    def fake_pipeline(paths: object, quote_month: object) -> CoreRunResult:
        received["paths"] = paths
        received["quote_month"] = quote_month
        return _result()

    exit_code = run_cli(
        [
            "--base",
            "base.xlsx",
            "--marketing",
            "marketing.xlsx",
            "--bop",
            "bop.xlsx",
            "--output-dir",
            "outputs",
            "--year",
            "2026",
            "--month",
            "8",
        ],
        pipeline=fake_pipeline,
        current_month=QuoteMonth(2026, 7),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert received["quote_month"] == QuoteMonth(2026, 8)
    assert "报价表：/tmp/报价表.xlsx" in output
    assert "执行报告：/tmp/执行报告.xlsx" in output
    assert "总行数：5" in output
    assert "处理完成：1" in output
    assert "处理失败：1" in output
    assert BETA_NOTICE in output
    assert APP_BUILD_LABEL in output


def test_cli_core_pipeline_error_is_chinese_and_has_no_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from quote_app.app import run_cli

    def failing_pipeline(*_args: object) -> CoreRunResult:
        raise CorePipelineError((Issue("INVALID", "基础表格式不正确", True),))

    exit_code = run_cli(
        [
            "--base",
            "base.xlsx",
            "--marketing",
            "marketing.xlsx",
            "--bop",
            "bop.xlsx",
            "--output-dir",
            "outputs",
        ],
        pipeline=failing_pipeline,
        current_month=QuoteMonth(2026, 7),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "运行未完成：输入预检查未通过：基础表格式不正确" in captured.err
    assert "Traceback" not in captured.err


def test_format_run_summary_exposes_all_available_summary_fields() -> None:
    from quote_app.app import format_run_summary

    assert format_run_summary(_result().summary) == (
        "总行数：5\n"
        "处理完成：1\n"
        "部分完成：2\n"
        "处理失败：1\n"
        "不支持品牌：1\n"
        "待人工补充：4"
    )


def test_full_pipeline_success_status_includes_sites_waiting_for_login() -> None:
    """Break caught: the published workbook is blank but the app hides login work."""
    from quote_app.app import format_success
    from quote_app.services.full_pipeline import FullPipelineResult
    from quote_app.services.web_run import WebsiteRunSummary

    result = FullPipelineResult(
        quote_path=Path("/tmp/报价表.xlsx"),
        report_path=Path("/tmp/执行报告.xlsx"),
        summary=_result().summary,
        rows=(),
        website_summary=WebsiteRunSummary(
            succeeded=0,
            waiting_for_login=2,
            technical_failure=1,
            evidence_paths=(),
            waiting_sites=("jd", "tmall"),
            technical_failure_codes=("LAYOUT_CHANGED",),
        ),
    )

    status = format_success(result)

    assert "等待登录：2" in status
    assert "等待京东登录" in status
    assert "等待天猫登录" in status


def test_gui_opens_the_dedicated_login_browser_before_a_quotation_run(
    tmp_path: Path,
) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths

    statuses: list[str] = []
    opened_profiles: list[Path] = []
    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app._pipeline_thread = None
    app._website_thread = None
    app._closing = False
    app._destroyed = False
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.login_browser_launcher = opened_profiles.append

    app.open_login_browser()

    assert opened_profiles == [app.app_paths.browser_profile]
    assert statuses == [
        "已分别打开天猫和京东独立登录浏览器。请自行完成登录，完成后关闭浏览器，再点击“开始自动报价”。"
    ]


def test_gui_refuses_to_open_login_browser_while_quotation_is_running(
    tmp_path: Path,
) -> None:
    from quote_app.app import QuoteApp
    from quote_app.paths import build_app_paths

    statuses: list[str] = []
    app = object.__new__(QuoteApp)
    app.root = _GuiRoot()
    app.app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    app._pipeline_thread = _AliveThread()
    app._website_thread = None
    app._closing = False
    app._destroyed = False
    app._widgets_available = lambda: True
    app._set_status = statuses.append
    app.login_browser_launcher = lambda _profile: (_ for _ in ()).throw(
        AssertionError("must not open while running")
    )

    app.open_login_browser()

    assert statuses == ["自动报价正在运行，请等待当前任务完成后再登录"]


def _website_request(tmp_path: Path):
    from quote_app.services.web_run import WebsiteRunRequest

    task = WebsiteTask(
        task_id="website-task",
        run_id="run-1",
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-1",
        brand="小米",
        model_name="小米 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.JD,
    )
    return WebsiteRunRequest(
        run_id="run-1",
        tasks=(task,),
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
    )


@pytest.mark.parametrize(
    ("waiting_sites", "failure_codes", "expected"),
    [
        (("jd",), (), "等待京东登录"),
        (("official:小米",), (), "等待小米品牌官网登录"),
        ((), ("CAPTURE_PERMISSION",), "需要开启屏幕与系统录音权限"),
        ((), ("CAPTURE_ACCESSIBILITY",), "需要开启辅助功能权限"),
    ],
)
def test_website_summary_uses_precise_chinese_intervention_statuses(
    waiting_sites: tuple[str, ...],
    failure_codes: tuple[str, ...],
    expected: str,
) -> None:
    from quote_app.app import format_website_run_summary
    from quote_app.services.web_run import WebsiteRunSummary

    summary = WebsiteRunSummary(
        succeeded=2,
        waiting_for_login=len(waiting_sites),
        technical_failure=len(failure_codes),
        evidence_paths=(Path("/tmp/a.png"), Path("/tmp/b.png")),
        waiting_sites=waiting_sites,
        technical_failure_codes=failure_codes,
    )

    status = format_website_run_summary(summary)

    assert expected in status
    assert "网站查价成功：2" in status
    assert "截图成功：2" in status


class _GuiRoot:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.cancelled: list[str] = []
        self.destroy_calls = 0
        self.protocols: dict[str, object] = {}
        self.exists = True
        self._next_after = 0

    def after(self, delay: int, callback: object) -> str:
        assert delay == 25
        self._next_after += 1
        after_id = f"after-{self._next_after}"
        self.callbacks[after_id] = callback
        return after_id

    def after_cancel(self, after_id: str) -> None:
        self.cancelled.append(after_id)
        self.callbacks.pop(after_id, None)

    def protocol(self, name: str, callback: object) -> None:
        self.protocols[name] = callback

    def winfo_exists(self) -> bool:
        return self.exists

    def destroy(self) -> None:
        self.destroy_calls += 1
        self.exists = False

    def run_next(self) -> None:
        _after_id, callback = self.callbacks.popitem()
        assert callable(callback)
        callback()


class _GuiButton:
    def __init__(self) -> None:
        self.state = "disabled"
        self.configure_calls = 0

    def configure(self, *, state: str) -> None:
        self.configure_calls += 1
        self.state = state


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def _gui_app(root: _GuiRoot, button: _GuiButton, service: object):
    from quote_app.app import QuoteApp

    statuses: list[str] = []
    app = object.__new__(QuoteApp)
    app.root = root
    app.website_service = service
    app._website_results = queue.SimpleQueue()
    app._website_thread = None
    app._website_after_id = None
    app._website_generation = 0
    app._active_website_generation = None
    app._closing = False
    app._destroyed = False
    app.open_button = button
    app._last_output_dir = None
    app._set_status = statuses.append
    return app, statuses


def test_gui_runs_website_service_off_tk_thread_and_keeps_partial_output_openable(
    tmp_path: Path,
) -> None:
    from quote_app.services.web_run import WebsiteRunSummary

    request = _website_request(tmp_path)
    worker_threads: list[int] = []
    summary = WebsiteRunSummary(
        succeeded=1,
        waiting_for_login=1,
        technical_failure=1,
        evidence_paths=(tmp_path / "evidence" / "one.png",),
        waiting_sites=("jd",),
        technical_failure_codes=("CAPTURE_PERMISSION",),
    )

    def service(received):
        assert received == request
        worker_threads.append(threading.get_ident())
        return summary

    root = _GuiRoot()
    button = _GuiButton()
    app, statuses = _gui_app(root, button, service)

    app.run_website(request)
    app._website_thread.join(timeout=5)

    assert worker_threads and worker_threads[0] != threading.get_ident()
    assert app._website_thread.daemon is False
    assert len(root.callbacks) == 1
    root.run_next()
    assert app._last_output_dir == request.evidence_dir
    assert button.state == "normal"
    assert "等待京东登录" in statuses[-1]
    assert "需要开启屏幕与系统录音权限" in statuses[-1]


def test_gui_close_waits_for_worker_cleanup_and_never_touches_widgets_after_close(
    tmp_path: Path,
) -> None:
    from quote_app.services.web_run import WebsiteRunSummary

    request = _website_request(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    summary = WebsiteRunSummary(0, 1, 0, (), ("jd",), ())

    def service(_request):
        entered.set()
        try:
            assert release.wait(timeout=5)
            return summary
        finally:
            cleaned.set()

    root = _GuiRoot()
    button = _GuiButton()
    app, statuses = _gui_app(root, button, service)
    app._install_close_handler()
    assert root.protocols["WM_DELETE_WINDOW"] == app.close

    app.run_website(request)
    assert entered.wait(timeout=5)
    scheduled_before_close = app._website_after_id
    status_count = len(statuses)
    button_calls = button.configure_calls

    app.close()
    app.close()

    assert root.destroy_calls == 0
    assert scheduled_before_close in root.cancelled
    assert len(root.callbacks) == 1
    assert len(statuses) == status_count
    assert button.configure_calls == button_calls

    release.set()
    app._website_thread.join(timeout=5)
    assert cleaned.is_set()
    root.run_next()

    assert root.destroy_calls == 1
    assert root.callbacks == {}
    assert len(statuses) == status_count
    assert button.configure_calls == button_calls
    app.close()
    assert root.destroy_calls == 1


def test_gui_close_releases_full_pipeline_manual_wait_and_preserves_sqlite_waiting(
    tmp_path: Path,
) -> None:
    """Break caught: closing while the full pipeline awaits login hangs forever."""
    from datetime import datetime, timezone
    from hashlib import sha256

    from quote_app.domain.models import QuoteMonth
    from quote_app.paths import build_app_paths
    from quote_app.services.web_run import WebsiteRunController
    from quote_app.tasks.models import (
        SCHEMA_VERSION,
        InputFingerprint,
        RunRecord,
        RunState,
        TaskState,
    )
    from quote_app.tasks.repository import SQLiteTaskRepository
    from quote_app.tasks.retry import LoginRequired, RetryPolicy
    from quote_app.tasks.scheduler import BrowserTaskScheduler

    app_paths = build_app_paths("Darwin", home=tmp_path / "user")
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    run_record = RunRecord(
        run_id="run-close-waiting",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=tuple(
            InputFingerprint(
                source_role=role,
                path=tmp_path / f"{role}.xlsx",
                sha256=str(index) * 64,
                byte_size=1,
                modified_ns=1,
            )
            for index, role in enumerate(("base", "marketing", "bop"), 1)
        ),
        output_dir=tmp_path / "outputs",
        browser_profile_dir=app_paths.browser_profile,
        associated_rows_snapshot="[]",
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256=sha256(b"[]").hexdigest(),
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=now,
        updated_at=now,
    )
    task = WebsiteTask(
        task_id="close-waiting",
        run_id=run_record.run_id,
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-2",
        brand="HONOR",
        model_name="荣耀Magic8",
        ram="16GB",
        storage="512GB",
        color="天青釉",
        channel=WebsiteChannel.TMALL,
    )
    with SQLiteTaskRepository(app_paths.task_database) as repository:
        repository.create_run(run_record)
        repository.upsert_task(task)

    waiting_published = threading.Event()

    def pipeline(request: object):
        controller = getattr(request, "controller")
        assert isinstance(controller, WebsiteRunController)
        with SQLiteTaskRepository(app_paths.task_database) as repository:
            scheduler = BrowserTaskScheduler(
                repository,
                run_record.run_id,
                lambda *_args: (_ for _ in ()).throw(
                    LoginRequired("tmall", "需要登录")
                ),
                retry_policy=RetryPolicy(retry_delay_seconds=0),
            )
            scheduler.run_until_idle()
            action = scheduler.waiting_action
            assert action is not None
            controller.publish_manual_action(action)
            waiting_published.set()
            if controller.wait_for_resolution():
                scheduler.continue_current_task()
            else:
                assert scheduler.cancel_manual_action() is True
        return _result()

    root = _GuiRoot()
    button = _GuiButton()
    app, statuses = _gui_app(root, button, lambda _request: None)
    app.pipeline = pipeline
    app.app_paths = app_paths
    app.base_var = SimpleNamespace(get=lambda: str(tmp_path / "base.xlsx"))
    app.marketing_var = SimpleNamespace(get=lambda: str(tmp_path / "marketing.xlsx"))
    app.bop_var = SimpleNamespace(get=lambda: str(tmp_path / "bop.xlsx"))
    app.output_dir_var = SimpleNamespace(get=lambda: str(tmp_path / "outputs"))
    app.year_var = SimpleNamespace(get=lambda: "2026")
    app.month_var = SimpleNamespace(get=lambda: "8")
    app.brand_mode_var = SimpleNamespace(get=lambda: "仅 HONOR")
    app.readiness_checker = _ready_check
    app._pipeline_results = queue.SimpleQueue()
    app._pipeline_thread = None
    app._pipeline_after_id = None
    app._pipeline_generation = 0
    app._active_pipeline_generation = None
    app._website_controller = None
    app._shown_manual_action_task_id = None
    app._install_close_handler()

    app.run()
    assert waiting_published.wait(timeout=5)
    status_count = len(statuses)
    button_calls = button.configure_calls

    app.close()
    app.close()

    assert app._pipeline_thread is not None
    app._pipeline_thread.join(timeout=5)
    assert not app._pipeline_thread.is_alive()
    root.run_next()
    with SQLiteTaskRepository(app_paths.task_database) as repository:
        assert repository.task_state(task.task_id) is TaskState.WAITING_FOR_LOGIN
    assert root.destroy_calls == 1
    assert root.callbacks == {}
    assert len(statuses) == status_count
    assert button.configure_calls == button_calls


def test_gui_ignores_stale_completed_outcome_when_a_new_run_starts(
    tmp_path: Path,
) -> None:
    from quote_app.services.web_run import WebsiteRunSummary

    request = _website_request(tmp_path)
    summaries = iter(
        (
            WebsiteRunSummary(1, 0, 0, (Path("/tmp/stale.png"),)),
            WebsiteRunSummary(2, 0, 0, (Path("/tmp/new-1.png"), Path("/tmp/new-2.png"))),
        )
    )

    def service(_request):
        return next(summaries)

    root = _GuiRoot()
    button = _GuiButton()
    app, statuses = _gui_app(root, button, service)

    app.run_website(request)
    first_thread = app._website_thread
    first_thread.join(timeout=5)
    app.run_website(request)
    second_thread = app._website_thread
    second_thread.join(timeout=5)

    assert first_thread is not second_thread
    assert len(root.callbacks) == 1
    root.run_next()
    assert "网站查价成功：2" in statuses[-1]
    assert "截图成功：2" in statuses[-1]
    assert "网站查价成功：1" not in statuses[-1]

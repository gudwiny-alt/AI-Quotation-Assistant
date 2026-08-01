"""本地核心内测版的命令行和 Tk 启动界面。"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

from quote_app.domain.models import InputPaths, QuoteMonth
from quote_app.browser.channel_detection import BrowserNotFoundError
from quote_app.browser.login import open_login_browser
from quote_app.browser.profile_lock import ProfileLockError
from quote_app.browser.session import BrowserProfileInUseError, ProfileValidationError
from quote_app.browser.worker import WorkerEvent
from quote_app.evidence.models import MacCapturePolicy
from quote_app.excel.report_writer import RunSummary
from quote_app.paths import AppPaths, build_app_paths
from quote_app.services.core_pipeline import CorePipelineError, CoreRunResult, run_core_pipeline
from quote_app.services.full_pipeline import (
    FullPipelineRequest,
    FullPipelineResult,
    run_full_pipeline,
)
from quote_app.services.readiness import ReadinessCheck, check_runtime_readiness
from quote_app.services.web_run import (
    WebsiteRunController,
    WebsiteRunRequest,
    WebsiteRunSummary,
    mac_visual_review_runtime_factory,
    run_website_tasks,
)
from quote_app.tasks.models import WebsiteChannel


APP_BUILD_LABEL = "荣耀搜索词兼容修复版（仅官网、全部荣耀行）2026.08.01.7"
BETA_NOTICE = (
    f"{APP_BUILD_LABEL}：荣耀官网首次使用无需预先登录；遇到登录或验证页面时，"
    "完成后点击“继续当前任务”。程序会兼容搜索词中的品牌前缀和空格。"
)

CorePipeline = Callable[[InputPaths, QuoteMonth], CoreRunResult]
DesktopPipeline = Callable[[FullPipelineRequest], FullPipelineResult]
WebsiteService = Callable[[WebsiteRunRequest], WebsiteRunSummary]
LoginBrowserLauncher = Callable[[Path], None]
ReadinessChecker = Callable[[AppPaths], ReadinessCheck]


class InputValidationError(ValueError):
    """Raised when the beta runner cannot form a valid core request."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    paths: InputPaths
    quote_month: QuoteMonth


def make_full_pipeline_request(
    *,
    paths: InputPaths,
    quote_month: QuoteMonth,
    app_paths: AppPaths | None = None,
    controller: WebsiteRunController | None = None,
    selected_brand: str | None = None,
    selected_channels: frozenset[WebsiteChannel] | None = None,
    event_sink: Callable[[WorkerEvent], None] | None = None,
) -> FullPipelineRequest:
    """Bind a user-selected quotation run to durable per-user browser state."""
    state_paths = app_paths or build_app_paths()
    return FullPipelineRequest(
        paths=paths,
        quote_month=quote_month,
        browser_profile_dir=state_paths.browser_profile,
        database_path=state_paths.task_database,
        evidence_dir=state_paths.evidence_dir,
        runtime_readiness=lambda: check_runtime_readiness(state_paths),
        controller=controller,
        selected_brand=selected_brand,
        selected_channels=selected_channels,
        event_sink=event_sink,
    )


def run_desktop_pipeline(request: FullPipelineRequest) -> FullPipelineResult:
    """Use the approved visual-review capture path for the Mac pilot only."""
    if sys.platform != "darwin":
        return run_full_pipeline(request)
    mac_request = replace(
        request,
        capture_acceptance_policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )
    return run_full_pipeline(
        mac_request,
        website_runner=partial(
            run_website_tasks,
            runtime_factory=mac_visual_review_runtime_factory,
        ),
    )


def _path_argument(value: str | None, label: str) -> Path:
    if value is None or not value.strip():
        raise InputValidationError(f"{label}不能为空")
    return Path(value.strip()).expanduser()


def make_run_request(
    *,
    base: str | None,
    marketing: str | None,
    bop: str | None,
    output_dir: str | None,
    year: int | None,
    month: int | None,
    current_month: QuoteMonth | None = None,
) -> RunRequest:
    """Validate user-entered fields without touching source workbooks."""
    default_month = current_month or QuoteMonth.current()
    selected_year = default_month.year if year is None else year
    selected_month = default_month.month if month is None else month
    if not 1 <= selected_year <= 9999:
        raise InputValidationError("报价年份不合法，请输入 1 到 9999")
    try:
        quote_month = QuoteMonth(selected_year, selected_month)
    except ValueError:
        raise InputValidationError("报价月份不合法，请输入 1 到 12 月") from None
    return RunRequest(
        paths=InputPaths(
            base=_path_argument(base, "基础表"),
            marketing=_path_argument(marketing, "营销商品信息查询表"),
            bop=_path_argument(bop, "BOP资源信息表"),
            output_dir=_path_argument(output_dir, "输出目录"),
        ),
        quote_month=quote_month,
    )


def parse_cli_arguments(
    arguments: Sequence[str], *, current_month: QuoteMonth | None = None
) -> RunRequest:
    parser = argparse.ArgumentParser(description="资金物流平台铺货报价本地核心内测版")
    parser.add_argument("--base", help="基础表工作簿路径")
    parser.add_argument("--marketing", help="营销商品信息查询表工作簿路径")
    parser.add_argument("--bop", help="BOP资源信息表工作簿路径")
    parser.add_argument("--output-dir", help="输出目录")
    parser.add_argument("--year", type=int, help="报价年份；缺省为当前自然月")
    parser.add_argument("--month", type=int, help="报价月份；缺省为当前自然月")
    parsed = parser.parse_args(arguments)
    return make_run_request(
        base=parsed.base,
        marketing=parsed.marketing,
        bop=parsed.bop,
        output_dir=parsed.output_dir,
        year=parsed.year,
        month=parsed.month,
        current_month=current_month,
    )


def format_run_summary(summary: RunSummary) -> str:
    return "\n".join(
        (
            f"总行数：{summary.total_rows}",
            f"处理完成：{summary.completed_rows}",
            f"部分完成：{summary.partial_rows}",
            f"处理失败：{summary.failed_rows}",
            f"不支持品牌：{summary.unsupported_rows}",
            f"待人工补充：{summary.manual_supplement_rows}",
        )
    )


def format_success(result: CoreRunResult | FullPipelineResult) -> str:
    lines = [
        f"版本：{APP_BUILD_LABEL}",
        f"报价表：{result.quote_path}",
        f"执行报告：{result.report_path}",
        format_run_summary(result.summary),
    ]
    if isinstance(result, FullPipelineResult):
        lines.append(format_website_run_summary(result.website_summary))
    lines.append(BETA_NOTICE)
    return "\n".join(lines)


_WEBSITE_SITE_LABELS = {
    "jd": "京东",
    "tmall": "天猫",
    "official": "品牌官网",
}


def _website_site_label(site: str) -> str:
    if site.startswith("official:"):
        brand = site.removeprefix("official:")
        if brand:
            return f"{brand}品牌官网"
    return _WEBSITE_SITE_LABELS.get(site, site)


def format_website_run_summary(summary: WebsiteRunSummary) -> str:
    lines = [
        f"网站查价成功：{summary.succeeded}",
        f"截图成功：{len(summary.evidence_paths)}",
        f"等待登录：{summary.waiting_for_login}",
        f"技术失败：{summary.technical_failure}",
    ]
    lines.extend(
        f"等待{_website_site_label(site)}登录"
        for site in summary.waiting_sites
    )
    codes = set(summary.technical_failure_codes)
    if "CAPTURE_PERMISSION" in codes:
        lines.append("需要开启屏幕与系统录音权限")
    if "CAPTURE_ACCESSIBILITY" in codes:
        lines.append("需要开启辅助功能权限")
    return "\n".join(lines)


_WORKER_CHANNEL_LABELS = {
    "official": "官网",
    "jd": "京东",
    "tmall": "天猫",
}
_WORKER_OUTCOME_LABELS = {
    "no_model": "无该机型",
    "capacity_unavailable": "容量不可用",
    "color_unavailable": "颜色不可用",
    "sold_out": "已售罄",
}


def format_worker_event_status(event: WorkerEvent) -> str:
    """Return user-facing copy for one credential-free website stage event."""
    channel_value = event.data.get("channel")
    channel = _WORKER_CHANNEL_LABELS.get(
        channel_value,
        channel_value,
    ) if isinstance(channel_value, str) else "网站"
    model_value = event.data.get("model_name")
    model = (
        model_value.strip()
        if isinstance(model_value, str) and model_value.strip()
        else event.task_id
    )
    if event.event == "progress":
        return f"正在处理：{channel} / {model}"
    if event.event in {"observation", "result"}:
        outcome = event.data.get("outcome")
        price = event.data.get("price")
        if outcome == "price_found" and isinstance(price, str) and price:
            business = f"价格{price}"
        elif isinstance(outcome, str):
            business = _WORKER_OUTCOME_LABELS.get(outcome, "结果已确认")
        else:
            business = "结果已确认"
        if event.event == "observation":
            return (
                f"已保存阶段结果：{channel} / {model} / "
                f"{business} / 截图待补"
            )
        return (
            f"已保存阶段结果：{channel} / {model} / {business} / "
            f"截图已补 / {channel}渠道完成"
        )
    if event.event == "waiting_for_login":
        site = event.data.get("site")
        site_label = (
            _website_site_label(site)
            if isinstance(site, str) and site
            else channel
        )
        return f"网站任务已暂停：{site_label} / {model} / 等待登录或安全验证"
    if event.event == "technical_failure":
        code = event.data.get("error_code")
        code_label = code if isinstance(code, str) and code else "UNKNOWN"
        retryable = event.data.get("retryable")
        retry_remaining = event.data.get("retry_remaining")
        if retryable is True and isinstance(retry_remaining, int) and retry_remaining > 0:
            return (
                f"网站任务暂时失败：{channel} / {model} / {code_label}；"
                f"将自动重试（剩余{retry_remaining}次）"
            )
        return f"网站任务技术失败：{channel} / {model} / {code_label}"
    return f"网站任务状态已更新：{channel} / {model}"


def run_cli(
    arguments: Sequence[str],
    *,
    pipeline: CorePipeline = run_core_pipeline,
    current_month: QuoteMonth | None = None,
) -> int:
    """Run the local core pipeline without creating a Tk window."""
    try:
        request = parse_cli_arguments(arguments, current_month=current_month)
        result = pipeline(request.paths, request.quote_month)
    except InputValidationError as error:
        print(f"输入无效：{error}", file=sys.stderr)
        return 2
    except CorePipelineError as error:
        print(f"运行未完成：{error}", file=sys.stderr)
        return 1
    print(format_success(result))
    return 0


class QuoteApp:
    """Minimal Tk shell around the already-verified local core pipeline."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        pipeline: DesktopPipeline = run_desktop_pipeline,
        website_service: WebsiteService = run_website_tasks,
        login_browser_launcher: LoginBrowserLauncher = open_login_browser,
        readiness_checker: ReadinessChecker = check_runtime_readiness,
        app_paths: AppPaths | None = None,
    ) -> None:
        self.root = root
        self.pipeline = pipeline
        self.app_paths = app_paths or build_app_paths()
        self._pipeline_results: queue.SimpleQueue[
            tuple[int, FullPipelineResult | BaseException]
        ] = queue.SimpleQueue()
        self._pipeline_events: queue.SimpleQueue[
            tuple[int, WorkerEvent]
        ] = queue.SimpleQueue()
        self._pipeline_thread: threading.Thread | None = None
        self._pipeline_after_id: str | None = None
        self._pipeline_generation = 0
        self._active_pipeline_generation: int | None = None
        self.website_service = website_service
        self.login_browser_launcher = login_browser_launcher
        if not callable(readiness_checker):
            raise ValueError("readiness_checker must be callable")
        self.readiness_checker = readiness_checker
        self._website_results: queue.SimpleQueue[
            tuple[int, WebsiteRunRequest, WebsiteRunSummary | BaseException]
        ] = queue.SimpleQueue()
        self._website_thread: threading.Thread | None = None
        self._website_after_id: str | None = None
        self._website_generation = 0
        self._active_website_generation: int | None = None
        self._closing = False
        self._destroyed = False
        current = QuoteMonth.current()
        self.base_var = tk.StringVar()
        self.marketing_var = tk.StringVar()
        self.bop_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.year_var = tk.StringVar(value=str(current.year))
        self.month_var = tk.StringVar(value=str(current.month))
        self.brand_mode_var = tk.StringVar(value="荣耀官网验收（仅官网）")
        self._last_output_dir: Path | None = None
        self._website_controller: WebsiteRunController | None = None
        self._shown_manual_action_task_id: str | None = None
        self._readiness_check_count = 0
        self._build()
        self._install_close_handler()
        self._schedule_initial_readiness_check()

    def _build(self) -> None:
        self.root.title(f"资金物流平台铺货报价 - {APP_BUILD_LABEL}")
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        notice = ttk.Label(frame, text=BETA_NOTICE, foreground="#B45309")
        notice.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._add_file_row(frame, 1, "基础表", self.base_var, False)
        self._add_file_row(frame, 2, "营销商品信息查询表", self.marketing_var, False)
        self._add_file_row(frame, 3, "BOP资源信息表", self.bop_var, False)
        self._add_file_row(frame, 4, "输出目录", self.output_dir_var, True)

        ttk.Label(frame, text="报价月份").grid(row=5, column=0, sticky="w", pady=4)
        month_frame = ttk.Frame(frame)
        month_frame.grid(row=5, column=1, columnspan=2, sticky="w", pady=4)
        ttk.Entry(month_frame, width=8, textvariable=self.year_var).grid(row=0, column=0)
        ttk.Label(month_frame, text="年").grid(row=0, column=1, padx=(4, 12))
        ttk.Entry(month_frame, width=5, textvariable=self.month_var).grid(row=0, column=2)
        ttk.Label(month_frame, text="月").grid(row=0, column=3, padx=4)
        ttk.Label(month_frame, text="运行范围").grid(
            row=0, column=4, padx=(16, 4)
        )
        ttk.Combobox(
            month_frame,
            textvariable=self.brand_mode_var,
            values=("荣耀官网验收（仅官网）", "荣耀全站闭环（官网、京东、天猫）"),
            state="readonly",
            width=10,
        ).grid(row=0, column=5)

        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 8))
        ttk.Button(actions, text="开始自动报价", command=self.run).grid(row=0, column=0)
        ttk.Button(
            actions,
            text="首次登录（京东/天猫）",
            command=self.open_login_browser,
        ).grid(row=0, column=1, padx=8)
        ttk.Button(
            actions,
            text="检查截图权限",
            command=self.check_readiness,
        ).grid(row=0, column=2, padx=(0, 8))
        self.open_button = ttk.Button(
            actions, text="打开输出目录", command=self.open_output_directory, state="disabled"
        )
        self.open_button.grid(row=0, column=3)
        self.continue_button = ttk.Button(
            actions,
            text="继续当前任务",
            command=self.continue_current_task,
            state="disabled",
        )
        self.continue_button.grid(row=1, column=0, pady=(8, 0))
        self.cancel_button = ttk.Button(
            actions,
            text="取消本次网页任务",
            command=self.cancel_manual_action,
            state="disabled",
        )
        self.cancel_button.grid(row=1, column=1, pady=(8, 0))

        ttk.Label(frame, text="运行结果").grid(row=7, column=0, sticky="nw", pady=(4, 0))
        self.status = scrolledtext.ScrolledText(frame, width=72, height=13, state="disabled")
        self.status.grid(row=7, column=1, columnspan=2, sticky="nsew", pady=(4, 0))

    def _add_file_row(
        self, frame: ttk.Frame, row: int, label: str, variable: tk.StringVar, directory: bool
    ) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        chooser = self._choose_directory if directory else self._choose_file
        ttk.Button(frame, text="选择…", command=lambda: chooser(variable)).grid(
            row=row, column=2, padx=(8, 0), pady=4
        )

    @staticmethod
    def _choose_file(variable: tk.StringVar) -> None:
        selected = filedialog.askopenfilename(filetypes=[("Excel 工作簿", "*.xlsx *.xlsm *.xls")])
        if selected:
            variable.set(selected)

    @staticmethod
    def _choose_directory(variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory()
        if selected:
            variable.set(selected)

    def run(self) -> None:
        if not self._widgets_available():
            return
        if self._pipeline_thread is not None and self._pipeline_thread.is_alive():
            self._set_status("自动报价正在运行，请等待当前任务完成")
            return
        readiness = self._current_readiness()
        if not readiness.ready:
            self._set_status(
                "运行前检查未通过：" + "；".join(readiness.messages)
            )
            return
        self.open_button.configure(state="disabled")
        try:
            request = make_run_request(
                base=self.base_var.get(),
                marketing=self.marketing_var.get(),
                bop=self.bop_var.get(),
                output_dir=self.output_dir_var.get(),
                year=_as_optional_int(self.year_var.get()),
                month=_as_optional_int(self.month_var.get()),
            )
            selected_brand = self._selected_brand_from_mode()
            selected_channels = self._selected_channels_from_mode()
            controller = WebsiteRunController()
            self._website_controller = controller
            self._shown_manual_action_task_id = None
            self._pipeline_generation += 1
            generation = self._pipeline_generation
            self._active_pipeline_generation = generation
            pipeline_events = getattr(self, "_pipeline_events", None)
            if pipeline_events is None:
                pipeline_events = queue.SimpleQueue()
                self._pipeline_events = pipeline_events

            def queue_worker_event(event: WorkerEvent) -> None:
                pipeline_events.put((generation, event))

            full_request = make_full_pipeline_request(
                paths=request.paths,
                quote_month=request.quote_month,
                app_paths=self.app_paths,
                controller=controller,
                selected_brand=selected_brand,
                selected_channels=selected_channels,
                event_sink=queue_worker_event,
            )
        except InputValidationError as error:
            self._set_status(f"输入无效：{error}")
            return
        status = f"{APP_BUILD_LABEL}\n自动报价运行中"
        if selected_channels == frozenset({WebsiteChannel.OFFICIAL}):
            status = f"荣耀官网验收模式（仅官网）\n{status}"
        elif selected_brand == "HONOR":
            status = f"荣耀闭环穿测模式（仅输出全部荣耀行）\n{status}"
        self._set_status(status)

        def work() -> None:
            try:
                outcome: FullPipelineResult | BaseException = self.pipeline(full_request)
            except BaseException as error:
                outcome = error
            self._pipeline_results.put((generation, outcome))

        self._pipeline_thread = threading.Thread(
            target=work,
            name=f"quotation-run-{generation}",
            daemon=False,
        )
        self._pipeline_thread.start()
        self._schedule_pipeline_poll()

    def _selected_brand_from_mode(self) -> str:
        mode = self.brand_mode_var.get()
        if mode in {
            "仅 HONOR",
            "荣耀官网验收（仅官网）",
            "荣耀全站闭环（官网、京东、天猫）",
        }:
            return "HONOR"
        raise InputValidationError("当前版本仅支持 HONOR")

    def _selected_channels_from_mode(
        self,
    ) -> frozenset[WebsiteChannel] | None:
        mode = self.brand_mode_var.get()
        if mode == "荣耀官网验收（仅官网）":
            return frozenset({WebsiteChannel.OFFICIAL})
        if mode in {"仅 HONOR", "荣耀全站闭环（官网、京东、天猫）"}:
            return None
        raise InputValidationError("当前版本仅支持荣耀官网验收或荣耀全站闭环")

    def check_readiness(self) -> None:
        """Render the current local capture and browser preflight state."""
        if not self._widgets_available():
            return
        readiness = self._current_readiness()
        self._readiness_check_count = (
            getattr(self, "_readiness_check_count", 0) + 1
        )
        prefix = (
            f"运行前检查（第{self._readiness_check_count}次）已通过："
            if readiness.ready
            else f"运行前检查（第{self._readiness_check_count}次）未通过："
        )
        items = "\n".join(
            f"{'✓' if item.ready else '✗'} "
            f"{item.label if item.ready else item.failure_message or item.label}"
            for item in readiness.items
        )
        self._set_status(f"{prefix}\n{items}")

    def _schedule_initial_readiness_check(self) -> None:
        """Show the local capture/browser state as soon as the GUI is ready."""
        self.root.after(25, self.check_readiness)

    def _current_readiness(self) -> ReadinessCheck:
        checker = getattr(self, "readiness_checker", check_runtime_readiness)
        readiness = checker(self.app_paths)
        if not isinstance(readiness, ReadinessCheck):
            raise TypeError("readiness_checker must return ReadinessCheck")
        return readiness

    def open_login_browser(self) -> None:
        """Let the user establish JD/Tmall sessions before browser automation starts."""
        if not self._widgets_available():
            return
        if self._worker_is_running(getattr(self, "_pipeline_thread", None)):
            self._set_status("自动报价正在运行，请等待当前任务完成后再登录")
            return
        if self._worker_is_running(getattr(self, "_website_thread", None)):
            self._set_status("网站查价正在运行，请等待当前任务完成后再登录")
            return
        try:
            self.login_browser_launcher(self.app_paths.browser_profile)
        except (
            BrowserNotFoundError,
            BrowserProfileInUseError,
            ProfileLockError,
            ProfileValidationError,
            OSError,
        ) as error:
            self._set_status(f"无法打开登录浏览器：{error}")
            return
        self._set_status(
            "已打开京东/天猫登录浏览器。请自行完成登录，完成后关闭该浏览器，再点击“开始自动报价”。"
        )

    @staticmethod
    def _worker_is_running(worker: threading.Thread | None) -> bool:
        return worker is not None and worker.is_alive()

    def _poll_pipeline_result(self) -> None:
        self._pipeline_after_id = None
        if self._destroyed or not self._root_exists():
            return
        self._render_pipeline_events()
        selected: FullPipelineResult | BaseException | None = None
        while True:
            try:
                generation, outcome = self._pipeline_results.get_nowait()
            except queue.Empty:
                break
            if generation == self._active_pipeline_generation:
                selected = outcome

        if not self._closing and selected is not None:
            if isinstance(selected, BaseException):
                if isinstance(selected, CorePipelineError):
                    self._set_status(f"运行未完成：{selected}")
                else:
                    self._set_status(f"自动报价未完成：{selected}")
            else:
                self._last_output_dir = selected.quote_path.parent
                self._set_status(format_success(selected))
                self.open_button.configure(state="normal")
            self._active_pipeline_generation = None

        worker = self._pipeline_thread
        if worker is not None and worker.is_alive():
            self._render_manual_action_if_waiting()
            self._schedule_pipeline_poll()
            return
        self._pipeline_thread = None
        if self._closing:
            self._destroy_root()

    def _render_pipeline_events(self) -> None:
        event_queue = getattr(self, "_pipeline_events", None)
        if event_queue is None:
            return
        while True:
            try:
                generation, event = event_queue.get_nowait()
            except queue.Empty:
                return
            if (
                not self._closing
                and generation == self._active_pipeline_generation
            ):
                self._set_status(format_worker_event_status(event))

    def continue_current_task(self) -> None:
        if not self._widgets_available():
            return
        controller = getattr(self, "_website_controller", None)
        action = controller.waiting_action if controller is not None else None
        if controller is None or action is None:
            self._set_status("当前没有等待人工处理的网站任务")
            return
        controller.continue_current_task()
        self._set_manual_action_buttons(enabled=False)
        self._shown_manual_action_task_id = None
        self._set_status(
            f"正在继续当前任务：{action.site} / {action.task.model_name}"
        )

    def cancel_manual_action(self) -> None:
        if not self._widgets_available():
            return
        controller = getattr(self, "_website_controller", None)
        if controller is None or not controller.cancel_manual_action():
            self._set_status("当前没有等待人工处理的网站任务")
            return
        self._set_manual_action_buttons(enabled=False)
        self._shown_manual_action_task_id = None
        self._set_status("已取消本次网页任务；当前等待任务将保留，之后可重新开始报价")

    def _render_manual_action_if_waiting(self) -> None:
        controller = getattr(self, "_website_controller", None)
        action = controller.waiting_action if controller is not None else None
        if action is None or action.task.task_id == self._shown_manual_action_task_id:
            return
        self._shown_manual_action_task_id = action.task.task_id
        self._set_manual_action_buttons(enabled=True)
        self._set_status(
            "自动报价已暂停，请在当前浏览器页面完成处理：\n"
            f"网站：{action.site}\n品牌：{action.task.brand}\n"
            f"机型：{action.task.model_name}\n原因：{action.reason}\n"
            "完成后点击“继续当前任务”。"
        )

    def _set_manual_action_buttons(self, *, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for name in ("continue_button", "cancel_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)

    def _schedule_pipeline_poll(self) -> None:
        if (
            self._pipeline_after_id is not None
            or self._destroyed
            or not self._root_exists()
        ):
            return
        self._pipeline_after_id = self.root.after(25, self._poll_pipeline_result)

    def _cancel_pipeline_poll(self) -> None:
        after_id = getattr(self, "_pipeline_after_id", None)
        self._pipeline_after_id = None
        if after_id is None or self._destroyed or not self._root_exists():
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _set_status(self, message: str) -> None:
        if not self._widgets_available():
            return
        self.status.configure(state="normal")
        self.status.delete("1.0", tk.END)
        self.status.insert(tk.END, message)
        self.status.configure(state="disabled")

    def run_website(self, request: WebsiteRunRequest) -> None:
        """Start browser work without ever using Playwright on the Tk thread."""
        if not self._widgets_available():
            return
        if self._website_thread is not None and self._website_thread.is_alive():
            self._set_status("网站查价正在运行，请等待当前任务完成")
            return
        self._website_generation += 1
        generation = self._website_generation
        self._active_website_generation = generation
        self._last_output_dir = request.evidence_dir
        self.open_button.configure(state="normal")
        self._set_status("网站查价运行中")

        def work() -> None:
            try:
                outcome: WebsiteRunSummary | BaseException = self.website_service(
                    request
                )
            except BaseException as error:
                outcome = error
            self._website_results.put((generation, request, outcome))

        self._website_thread = threading.Thread(
            target=work,
            name=f"website-run-{request.run_id}",
            daemon=False,
        )
        self._website_thread.start()
        self._schedule_website_poll()

    def _poll_website_result(self) -> None:
        self._website_after_id = None
        if self._destroyed or not self._root_exists():
            return
        selected: tuple[WebsiteRunRequest, WebsiteRunSummary | BaseException] | None = None
        while True:
            try:
                generation, request, outcome = self._website_results.get_nowait()
            except queue.Empty:
                break
            if generation == self._active_website_generation:
                selected = (request, outcome)

        if not self._closing and selected is not None:
            request, outcome = selected
            self._last_output_dir = request.evidence_dir
            self.open_button.configure(state="normal")
            if isinstance(outcome, BaseException):
                self._set_status("网站查价未完成，请查看任务检查点后重试")
            else:
                self._set_status(format_website_run_summary(outcome))
            self._active_website_generation = None

        worker = self._website_thread
        if worker is not None and worker.is_alive():
            self._schedule_website_poll()
            return
        self._website_thread = None
        if self._closing:
            self._destroy_root()

    def _schedule_website_poll(self) -> None:
        if (
            self._website_after_id is not None
            or self._destroyed
            or not self._root_exists()
        ):
            return
        self._website_after_id = self.root.after(
            25,
            self._poll_website_result,
        )

    def _cancel_website_poll(self) -> None:
        after_id = self._website_after_id
        self._website_after_id = None
        if after_id is None or self._destroyed or not self._root_exists():
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _install_close_handler(self) -> None:
        if not self._root_exists():
            return
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self) -> None:
        """Wait asynchronously for active worker resources to be cleaned up."""
        if self._closing or self._destroyed:
            return
        self._closing = True
        controller = getattr(self, "_website_controller", None)
        request_shutdown = getattr(controller, "request_shutdown", None)
        if callable(request_shutdown):
            request_shutdown()
        self._active_pipeline_generation = None
        self._cancel_pipeline_poll()
        self._active_website_generation = None
        self._cancel_website_poll()
        worker = getattr(self, "_pipeline_thread", None)
        if worker is not None and worker.is_alive():
            self._schedule_pipeline_poll()
            return
        worker = self._website_thread
        if worker is not None and worker.is_alive():
            self._schedule_website_poll()
            return
        self._destroy_root()

    def _destroy_root(self) -> None:
        if self._destroyed:
            return
        self._cancel_pipeline_poll()
        self._cancel_website_poll()
        if self._root_exists():
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        self._destroyed = True

    def _root_exists(self) -> bool:
        if self._destroyed:
            return False
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _widgets_available(self) -> bool:
        return not self._closing and self._root_exists()

    def open_output_directory(self) -> None:
        if not self._widgets_available() or self._last_output_dir is None:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(self._last_output_dir)])
        elif os.name == "nt":
            os.startfile(self._last_output_dir)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(self._last_output_dir)])


def _as_optional_int(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        raise InputValidationError("报价年份和月份必须是整数") from None


def launch_gui(
    *,
    pipeline: DesktopPipeline = run_desktop_pipeline,
    website_service: WebsiteService = run_website_tasks,
    app_paths: AppPaths | None = None,
) -> None:
    root = tk.Tk()
    QuoteApp(
        root,
        pipeline=pipeline,
        website_service=website_service,
        app_paths=app_paths,
    )
    root.mainloop()


def main(arguments: Sequence[str] | None = None) -> int:
    selected_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not selected_arguments or selected_arguments == ["--gui"]:
        launch_gui()
        return 0
    if "--gui" in selected_arguments:
        print("--gui 不能与命令行输入参数同时使用", file=sys.stderr)
        return 2
    return run_cli(selected_arguments)

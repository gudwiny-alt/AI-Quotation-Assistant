"""Native blue-white workbench; business actions stay on QuoteApp."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import TYPE_CHECKING

from PIL import Image, ImageTk

from quote_app.desktop_state import (
    CHANNEL_LABELS,
    STATE_LABELS,
    DesktopState,
    TaskRow,
    compact_path,
    read_history,
    read_task_rows,
    quote_channel_prices,
)

if TYPE_CHECKING:
    from quote_app.app import QuoteApp
    from quote_app.browser.worker import WorkerEvent
    from quote_app.services.full_pipeline import FullPipelineResult

BG = "#F3F6FC"
WHITE = "#FFFFFF"
INK = "#132443"
MUTED = "#72829D"
BLUE = "#2468F5"
LINE = "#DFE7F3"
PALE = "#EAF1FF"
GREEN = "#0B9975"
ORANGE = "#D88119"
PAGES = (
    ("overview", "▦", "协同总览", "准备数据，启动本月报价工作"),
    ("intelligence", "▥", "价格情报智能体", "全渠道取价与证据留存"),
    ("decision", "▤", "报价决策智能体", "多源价格比较与报价规则处理"),
    ("audit", "◇", "稽核审查智能体", "查看已保存结果、截图状态与技术异常"),
    ("history", "◷", "任务记录", "本机已保存任务 · 只读查看"),
    ("settings", "⚙", "系统设置", "浏览器登录、截图权限与本机存储"),
)


def label(
    parent: tk.Misc,
    text: str = "",
    *,
    size: int = 12,
    color: str = INK,
    bold: bool = False,
    bg: str = WHITE,
    **kwargs,
):
    return tk.Label(
        parent,
        text=text,
        font=("Helvetica", size, "bold" if bold else "normal"),
        fg=color,
        bg=bg,
        anchor="w",
        **kwargs,
    )


def button(parent: tk.Misc, text: str, command, *, primary: bool = False, **kwargs):
    return ttk.Button(
        parent,
        text=text,
        command=command,
        width=0,
        style="Primary.TButton" if primary else "Workbench.TButton",
        **kwargs,
    )


def card(parent: tk.Misc, **kwargs):
    return tk.Frame(parent, bg=WHITE, highlightbackground=LINE, highlightthickness=1, **kwargs)


class DesktopWorkbench:
    def __init__(self, app: QuoteApp, *, title: str, credit: str, modes: tuple[str, ...]):
        self.app, self.root = app, app.root
        self.model = DesktopState()
        self.page = "overview"
        self.filter = "全部"
        self._trace_ids: list[tuple[tk.StringVar, str]] = []
        self._configure_styles()
        self.root.title(title)
        self.root.geometry("1280x850")
        self.root.minsize(1000, 720)
        self.root.configure(bg=BG)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        self._sidebar(credit)
        self.main = tk.Frame(self.root, bg=BG, padx=26, pady=22)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(3, weight=1)
        self._header()
        self._task_bar(modes)
        self._metrics()
        self.body = tk.Frame(self.main, bg=BG)
        self.body.grid(row=3, column=0, sticky="nsew", pady=(16, 12))
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self._log()
        self.show_page("overview")

    def _configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Workbench.TButton",
            font=("Helvetica", 11),
            padding=(12, 8),
            background=WHITE,
            foreground=INK,
            bordercolor=LINE,
            lightcolor=WHITE,
            darkcolor=WHITE,
            focuscolor=LINE,
        )
        style.map(
            "Workbench.TButton", background=[("active", PALE)], foreground=[("disabled", "#A5B0C2")]
        )
        style.configure(
            "Primary.TButton",
            font=("Helvetica", 12, "bold"),
            padding=(16, 10),
            background=BLUE,
            foreground=WHITE,
            bordercolor=BLUE,
            lightcolor=BLUE,
            darkcolor=BLUE,
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", "#ACC4F9"), ("active", "#1556DA")],
            foreground=[("disabled", WHITE)],
        )
        style.configure("TCombobox", padding=5, fieldbackground=WHITE)
        style.configure("TEntry", padding=5)
        style.configure(
            "Workbench.Treeview",
            font=("Helvetica", 12),
            rowheight=43,
            background=WHITE,
            fieldbackground=WHITE,
            foreground=INK,
            borderwidth=0,
            bordercolor=WHITE,
        )
        style.configure(
            "Workbench.Treeview.Heading",
            font=("Helvetica", 11, "bold"),
            background="#F0F4FA",
            foreground="#536788",
            padding=(8, 10),
            relief="flat",
        )
        style.map(
            "Workbench.Treeview",
            background=[("selected", "#E0ECFF")],
            foreground=[("selected", "#174BA8")],
        )

    def _sidebar(self, credit):
        sidebar = tk.Frame(
            self.root,
            width=218,
            bg="#F7FAFF",
            padx=15,
            pady=26,
            highlightbackground=LINE,
            highlightthickness=1,
        )
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(9, weight=1)
        label(sidebar, "铺货报价智能体", size=17, bold=True, bg="#F7FAFF").grid(
            row=0, column=0, sticky="w", pady=(4, 6)
        )
        label(sidebar, "福建分公司 · 终端业务", size=10, color=MUTED, bg="#F7FAFF").grid(
            row=1, column=0, sticky="w", padx=8, pady=(0, 32)
        )
        self.nav = {}
        for index, (key, icon, title, _) in enumerate(PAGES):
            nav = tk.Button(
                sidebar,
                text=title,
                command=lambda key=key: self.show_page(key),
                bg="#F7FAFF",
                fg=INK,
                activebackground=PALE,
                activeforeground=BLUE,
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                font=("Helvetica", 12),
                anchor="w",
                padx=12,
                pady=15,
                cursor="hand2",
            )
            nav.grid(row=index + 2, column=0, sticky="ew", pady=(12 if index == 4 else 3, 3))
            self.nav[key] = nav
        label(
            sidebar,
            "●  Mac 本地运行" if sys.platform == "darwin" else "●  本地运行",
            color=MUTED,
            size=10,
            bg="#F7FAFF",
        ).grid(row=10, column=0, sticky="w", padx=8)
        label(sidebar, ".170 · UI预览版", color=MUTED, size=9, bg="#F7FAFF").grid(
            row=11, column=0, sticky="w", padx=8, pady=(6, 4)
        )
        label(sidebar, credit, color=MUTED, size=9, bg="#F7FAFF").grid(
            row=12, column=0, sticky="w", padx=8
        )

    def _header(self):
        frame = tk.Frame(self.main, bg=BG)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        frame.columnconfigure(0, weight=1)
        self.heading = label(frame, size=25, bold=True, bg=BG)
        self.heading.grid(row=0, column=0, sticky="w")
        self.subheading = label(frame, color=MUTED, size=12, bg=BG)
        self.subheading.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.start_button = button(frame, "开始自动报价", self.app.run, primary=True)
        self.start_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

    def _task_bar(self, modes):
        bar = card(self.main, padx=14, pady=10)
        bar.grid(row=1, column=0, sticky="ew")
        label(bar, "报价月份", size=11, color=MUTED).grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(bar, textvariable=self.app.year_var, width=6).grid(row=0, column=1)
        label(bar, "年", size=11).grid(row=0, column=2, padx=4)
        ttk.Combobox(
            bar,
            textvariable=self.app.month_var,
            width=3,
            state="readonly",
            values=tuple(str(month) for month in range(1, 13)),
        ).grid(row=0, column=3)
        label(bar, "月", size=11).grid(row=0, column=4, padx=(4, 16))
        label(bar, "运行范围", size=11, color=MUTED).grid(row=0, column=5, padx=(0, 8))
        ttk.Combobox(
            bar, textvariable=self.app.brand_mode_var, values=modes, state="readonly", width=7
        ).grid(row=0, column=6)
        bar.columnconfigure(7, weight=1)
        self.app.continue_button = button(
            bar, "继续当前任务", self.app.continue_current_task, state="disabled", padding=(7, 7)
        )
        self.app.continue_button.grid(row=0, column=8, padx=(10, 6))
        self.app.cancel_button = button(
            bar, "取消本次网页任务", self.app.cancel_manual_action, state="disabled", padding=(7, 7)
        )
        self.app.cancel_button.grid(row=0, column=9)

    def _metrics(self):
        row = self.metrics_frame = tk.Frame(self.main, bg=BG)
        row.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        self.metrics = []
        for index, color in enumerate((INK, BLUE, ORANGE)):
            row.columnconfigure(index, weight=1, uniform="metric")
            panel = card(row, padx=18, pady=12)
            panel.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=(0 if index == 0 else 6, 0 if index == 2 else 6),
            )
            title = label(panel, color=MUTED, size=11)
            title.pack(anchor="w")
            number = label(panel, "0", size=26, bold=True, color=color)
            number.pack(anchor="w", pady=(4, 0))
            self.metrics.append((title, number))

    def _log(self):
        panel = card(self.main, padx=14, pady=10)
        panel.grid(row=4, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)
        label(panel, "执行日志", size=13, bold=True).grid(row=0, column=0, sticky="w")
        self.app.open_button = button(
            panel, "打开输出目录", self.app.open_output_directory, state="disabled"
        )
        self.app.open_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        self.app.status = scrolledtext.ScrolledText(
            panel,
            height=4,
            width=60,
            state="disabled",
            wrap="word",
            bg=WHITE,
            fg="#526685",
            font=("Helvetica", 11),
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=4,
        )
        self.app.status.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.task_status = label(self.main, "尚未开始任务", size=10, color=MUTED, bg=BG)
        self.task_status.grid(row=5, column=0, sticky="ew", pady=(7, 0))

    def append_log(self, message: str):
        text = self.app.status
        text.configure(state="normal")
        text.insert(tk.END, f"{datetime.now():%H:%M:%S}  {message}\n\n")
        text.see(tk.END)
        text.configure(state="disabled")

    def show_page(self, page: str):
        self.page = page
        self.filter = "全部"
        if page in {"history", "settings"}:
            self.metrics_frame.grid_remove()
        else:
            self.metrics_frame.grid()
        for key, nav in self.nav.items():
            nav.configure(
                bg=BLUE if key == page else "#F7FAFF",
                fg=WHITE if key == page else INK,
                font=("Helvetica", 12, "bold" if key == page else "normal"),
            )
        entry = next(item for item in PAGES if item[0] == page)
        self.heading.configure(text=entry[2])
        self.subheading.configure(text=entry[3])
        for variable, trace_id in self._trace_ids:
            variable.trace_remove("write", trace_id)
        self._trace_ids.clear()
        for child in self.body.winfo_children():
            child.destroy()
        self.table = None
        self.detail = None
        self.empty = None
        if page == "overview":
            self._overview()
        elif page == "settings":
            self._settings()
        elif page == "history":
            self._history()
        else:
            self._workbench()
        self.refresh()

    def _scrollable(self, parent):
        viewport = tk.Frame(parent, bg=BG)
        viewport.grid(row=0, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        content = tk.Frame(canvas, bg=BG)
        content.columnconfigure(0, weight=1)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        content.bind(
            "<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        return content

    def _overview(self):
        content = self._scrollable(self.body)
        content.columnconfigure(0, weight=1)
        intro = tk.Frame(content, bg=BG)
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        label(intro, "数据准备", size=15, bold=True, bg=BG).pack(side="left")
        label(intro, "内部系统数据通过本地 Excel 导入", color=MUTED, size=11, bg=BG).pack(
            side="right"
        )
        cards = tk.Frame(content, bg=BG)
        cards.grid(row=1, column=0, sticky="ew")
        sources = (
            ("01", "基础表", "本月待报价商品与历史报价", self.app.base_var),
            ("02", "营销商品信息查询表", "营销商品与价格资料", self.app.marketing_var),
            ("03", "BOP资源信息表", "BOP 资源关联资料", self.app.bop_var),
        )
        for index, (num, title, subtitle, variable) in enumerate(sources):
            cards.columnconfigure(index, weight=1, uniform="source")
            panel = card(cards, padx=14, pady=14)
            panel.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 5, 0 if index == 2 else 5),
            )
            label(panel, f"{num}  本地导入", color=BLUE, size=10, bold=True).pack(anchor="w")
            label(panel, title, size=13, bold=True).pack(anchor="w", pady=(10, 5))
            label(panel, subtitle, size=10, color=MUTED).pack(anchor="w")
            filename = label(panel, compact_path(variable.get(), 25), size=11, color=MUTED)
            filename.pack(anchor="w", pady=(16, 10))

            def update(*_args, variable=variable, filename=filename):
                filename.configure(
                    text=compact_path(variable.get(), 25), fg=INK if variable.get() else MUTED
                )
                self.refresh()

            trace_id = variable.trace_add("write", update)
            self._trace_ids.append((variable, trace_id))
            controls = tk.Frame(panel, bg=WHITE)
            controls.pack(fill="x")
            button(
                controls, "选择文件…", lambda variable=variable: self.app._choose_file(variable)
            ).pack(side="left")
            button(
                controls, "路径", lambda variable=variable: self._show_path(variable.get())
            ).pack(side="right")
        output = card(content, padx=14, pady=12)
        output.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        output.columnconfigure(1, weight=1)
        label(output, "输出目录", size=11, bold=True).grid(row=0, column=0, padx=(0, 12))
        ttk.Entry(output, textvariable=self.app.output_dir_var).grid(row=0, column=1, sticky="ew")
        button(output, "选择…", lambda: self.app._choose_directory(self.app.output_dir_var)).grid(
            row=0, column=2, padx=(8, 0)
        )
        hint = card(content, padx=16, pady=14)
        hint.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        label(hint, "开始前，请完成浏览器登录与截图权限检查", bold=True, size=12).grid(
            row=0, column=0, sticky="w"
        )
        label(
            hint, "遇到登录或验证时，完成当前浏览器操作后点击“继续当前任务”。", color=MUTED, size=10
        ).grid(row=1, column=0, sticky="w", pady=(7, 0))
        hint.columnconfigure(0, weight=1)
        button(hint, "登录与权限 →", lambda: self.show_page("settings")).grid(
            row=0, column=1, rowspan=2, padx=(10, 0)
        )

    def _settings(self):
        content = self._scrollable(self.body)
        panel = card(content, padx=22, pady=20)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        label(panel, "浏览器与截图", size=16, bold=True).grid(row=0, column=0, sticky="w")
        label(panel, "登录状态保存在本机浏览器资料目录中。", color=MUTED).grid(
            row=1, column=0, sticky="w", pady=(7, 14)
        )
        actions = tk.Frame(panel, bg=WHITE)
        actions.grid(row=2, column=0, sticky="w")
        button(actions, "首次登录（京东/天猫）", self.app.open_login_browser).pack(
            side="left", padx=(0, 10)
        )
        button(actions, "检查截图权限", self.app.check_readiness).pack(side="left")
        label(panel, "本机存储", size=15, bold=True).grid(
            row=3, column=0, sticky="w", pady=(28, 12)
        )
        paths = self.app.app_paths
        for index, (title, path) in enumerate(
            (
                ("应用数据", paths.data_dir),
                ("任务数据库", paths.task_database),
                ("截图证据", paths.evidence_dir),
                ("浏览器资料", paths.browser_profile),
            )
        ):
            line = tk.Frame(panel, bg=WHITE)
            line.grid(row=index + 4, column=0, sticky="ew", pady=5)
            line.columnconfigure(1, weight=1)
            label(line, title, size=11, color=MUTED, width=10).grid(row=0, column=0, sticky="w")
            label(line, compact_path(str(path), 55), size=11).grid(row=0, column=1, sticky="w")
            button(line, "查看路径", lambda path=path: self._show_path(str(path))).grid(
                row=0, column=2
            )

    def _workbench(self):
        split = tk.Frame(self.body, bg=BG)
        split.grid(row=0, column=0, sticky="nsew")
        split.columnconfigure(0, weight=3)
        split.columnconfigure(1, weight=2)
        split.rowconfigure(0, weight=1)
        panel = card(split, padx=14, pady=14)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        title = {
            "intelligence": "渠道采集进度",
            "decision": "报价处理清单",
            "audit": "证据核验清单",
        }[self.page]
        label(panel, title, size=15, bold=True).grid(row=0, column=0, sticky="w")
        filters = tk.Frame(panel, bg=WHITE)
        filters.grid(row=1, column=0, sticky="w", pady=(10, 10))
        self.filter_buttons = {}
        options = (
            ("全部", "官网", "天猫", "京东")
            if self.page == "intelligence"
            else (
                ("全部", "待补证据", "截图已保存") if self.page == "audit" else ("全部", "含异常")
            )
        )
        for item in options:
            control = button(filters, item, lambda item=item: self._filter(item))
            control.pack(side="left", padx=(0, 5))
            self.filter_buttons[item] = control
        columns = (("商品", 210), ("渠道", 65), ("价格", 80), ("状态", 110))
        if self.page == "decision":
            columns = (("商品 / 物料", 200), ("官网", 75), ("天猫", 75), ("京东", 75))
        elif self.page == "audit":
            columns = (("商品", 210), ("渠道", 65), ("价格", 80), ("截图状态", 110))
        self.table = self._table(panel, columns, row=2)
        self.table.bind("<<TreeviewSelect>>", self._selection)
        self.empty = label(panel, "尚无数据", color=MUTED, size=11, wraplength=440, justify="left")
        self.empty.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.empty.bind(
            "<Configure>",
            lambda event: event.widget.configure(wraplength=max(160, event.width - 4)),
        )
        side = card(split, padx=18, pady=16)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)
        label(
            side, "本条报价依据" if self.page == "decision" else "任务详情", size=15, bold=True
        ).grid(row=0, column=0, sticky="w", pady=(0, 14))
        self.detail = scrolledtext.ScrolledText(
            side,
            width=25,
            height=8,
            wrap="word",
            font=("Helvetica", 12),
            bg=WHITE,
            fg=INK,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            state="disabled",
            spacing1=4,
            spacing3=7,
        )
        self.detail.grid(row=1, column=0, sticky="nsew")
        self.preview = None
        self.preview_image = None
        if self.page != "decision":
            self.preview = label(side, "暂无截图预览", size=11, color=MUTED, bg=BG)
            self.preview.grid(row=2, column=0, sticky="ew", pady=(10, 0), ipady=18)
        self.evidence_button = (
            button(side, "查看渠道证据", lambda: self.show_page("audit"))
            if self.page == "decision"
            else button(side, "打开截图证据", self._open_evidence, state="disabled")
        )
        self.evidence_button.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        if self.page == "decision":
            self.result_button = button(side, "打开报价工作簿", self._open_quote, state="disabled")
            self.result_button.grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def _table(self, parent, columns, *, row):
        frame = tk.Frame(parent, bg=WHITE)
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame,
            columns=tuple(str(i) for i in range(len(columns))),
            show="headings",
            selectmode="browse",
            style="Workbench.Treeview",
            height=5,
        )
        for index, (title, width) in enumerate(columns):
            tree.heading(str(index), text=title, anchor="w")
            tree.column(str(index), width=width, minwidth=55, stretch=index == 0, anchor="w")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.tag_configure("alternate", background="#F7F9FD")
        tree.tag_configure("warning", foreground=ORANGE)
        return tree

    def _filter(self, value):
        self.filter = value
        self.refresh()

    def refresh(self):
        rows = self.model.rows
        if self.page == "overview":
            values = (
                (
                    "已选择数据文件",
                    sum(
                        bool(v.get())
                        for v in (self.app.base_var, self.app.marketing_var, self.app.bop_var)
                    ),
                ),
                ("已获取渠道记录", len(rows)),
                ("已保存截图", self.model.evidence_count),
            )
        elif self.page == "decision":
            values = (
                ("输出商品记录", len(self.model.quote_rows)),
                ("含异常商品", sum(bool(row.issues) for row in self.model.quote_rows)),
                ("有效价格记录", sum(bool(row.price) for row in rows)),
            )
        else:
            values = (
                ("已接收渠道记录", len(rows)),
                ("截图已保存", self.model.evidence_count),
                (
                    "待补截图记录",
                    sum(bool(row.outcome) and row.evidence_state != "complete" for row in rows),
                ),
            )
        for (title, number), (text, count) in zip(self.metrics, values):
            title.configure(text=text)
            number.configure(text=str(count))
        self.task_status.configure(text=self.model.summary)
        self.start_button.configure(state="disabled" if self.model.running else "normal")
        if self.page not in {"intelligence", "decision", "audit"} or self.table is None:
            return
        selected = self.table.selection()
        for item in self.table.get_children():
            self.table.delete(item)
        if self.page == "decision":
            for index, row in enumerate(self.model.quote_rows):
                if self.filter == "含异常" and not row.issues:
                    continue
                values = (row.web_query.model_name or row.material_code, *quote_channel_prices(row))
                self.table.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=values,
                    tags=("warning",) if row.issues else ("alternate",) if index % 2 else (),
                )
            self.empty.configure(
                text="报价已生成；选择商品查看真实输出字段与异常。"
                if self.model.quote_rows
                else "尚无本次报价结果。任务完成后展示实际输出；运行中的阶段价格请到价格情报页查看。"
            )
            self.result_button.configure(
                state="normal"
                if self.model.quote_path and self.model.quote_path.is_file()
                else "disabled"
            )
        else:
            for index, row in enumerate(rows):
                if (
                    self.page == "intelligence"
                    and self.filter != "全部"
                    and CHANNEL_LABELS.get(row.channel) != self.filter
                ):
                    continue
                if (
                    self.page == "audit"
                    and self.filter == "待补证据"
                    and row.evidence_state == "complete"
                ):
                    continue
                if (
                    self.page == "audit"
                    and self.filter == "截图已保存"
                    and row.evidence_state != "complete"
                ):
                    continue
                last = row.evidence_label if self.page == "audit" else row.state_label
                self.table.insert(
                    "",
                    "end",
                    iid=row.task_id,
                    values=(
                        row.model_name or row.task_id,
                        CHANNEL_LABELS.get(row.channel, row.channel),
                        self._price(row.price),
                        last,
                    ),
                    tags=("warning",) if row.error else ("alternate",) if index % 2 else (),
                )
            self.empty.configure(
                text=(
                    f"本次已接收 {len(rows)} 条渠道记录；总任务数以执行报告为准。"
                    if rows
                    else "尚无本次渠道记录。选择数据并开始任务后，这里将展示真实取价进度。"
                )
            )
        items = self.table.get_children()
        for name, control in self.filter_buttons.items():
            control.configure(
                style="Primary.TButton" if name == self.filter else "Workbench.TButton"
            )
        if selected and selected[0] in items:
            self.table.selection_set(selected[0])
        elif items:
            self.table.selection_set(items[0])
        self._selection()

    @staticmethod
    def _cell(value):
        return "—" if value is None or value == "" else str(value)

    @staticmethod
    def _price(value):
        return f"¥ {value}" if value != "" else "—"

    def _selection(self, _event=None):
        if self.detail is None:
            return
        selection = self.table.selection()
        self.selected_evidence = None
        if not selection:
            text = "暂无可展示的记录\n\n运行后选择左侧记录，查看价格、处理结果及证据。"
        elif self.page == "decision":
            row = self.model.quote_rows[int(selection[0])]
            query = row.web_query
            official, tmall, jd = quote_channel_prices(row)
            text = f"{query.model_name or row.material_code}\n{query.storage or ''} / {query.color or ''}\n\n物料编码  {row.material_code}\n来源行号  {row.source_row_number}\n\n渠道输出价格\n官网  {official}\n天猫  {tmall}\n京东  {jd}\n\n最低有效价  {self._cell(row.cells.get('AH'))}\n来源链接  {self._cell(row.cells.get('S'))}\n\n"
            text += "处理异常\n" + (
                "\n".join(f"{issue.code}：{issue.message}" for issue in row.issues)
                if row.issues
                else "本条结果未记录异常"
            )
            text += "\n\n公式计算结果以实际报价工作簿为准。"
        else:
            row = next((r for r in self.model.rows if r.task_id == selection[0]), None)
            if row is None:
                return
            if row.evidence_state == "complete" and row.evidence_path is None and self.model.run_id:
                saved_rows, _error = read_task_rows(
                    self.app.app_paths.task_database, self.model.run_id
                )
                saved = next((item for item in saved_rows if item.task_id == row.task_id), None)
                if saved is not None:
                    row = saved
            text = self._task_detail(row)
            if row.evidence_path and row.evidence_path.is_file():
                self.selected_evidence = row.evidence_path
        self.detail.configure(state="normal")
        self.detail.delete("1.0", tk.END)
        self.detail.insert(tk.END, text)
        self.detail.configure(state="disabled")
        if self.page != "decision":
            self.evidence_button.configure(state="normal" if self.selected_evidence else "disabled")
            self._preview_evidence(self.selected_evidence)

    def _preview_evidence(self, path: Path | None):
        if self.preview is None:
            return
        self.preview_image = None
        self.preview.configure(image="", text="暂无截图预览")
        if path is None:
            return
        try:
            with Image.open(path) as source:
                source.thumbnail((280, 120))
                self.preview_image = ImageTk.PhotoImage(source, master=self.root)
            self.preview.configure(image=self.preview_image, text="")
        except (OSError, ValueError):
            self.preview.configure(text="预览不可用 · 可打开原始证据")

    def _task_detail(self, row: TaskRow):
        return (
            f"{row.model_name or row.task_id}\n{row.specification}\n\n"
            f"渠道  {CHANNEL_LABELS.get(row.channel, row.channel)}\n"
            f"阶段价格  {self._price(row.price)}\n业务结果  {row.outcome_label}\n"
            f"任务状态  {row.state_label}\n截图状态  {row.evidence_label}\n\n"
            + (f"技术异常\n{row.error}\n\n" if row.error else "")
            + (f"来源\n{row.url}\n\n" if row.url else "来源链接尚未载入\n\n")
            + (
                "截图已保存；可打开文件核对。"
                if row.evidence_path and row.evidence_path.is_file()
                else "暂无可打开的截图文件。价格已保存与截图完成分别记录。"
            )
        )

    def _history(self):
        panel = card(self.body, padx=16, pady=16)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        header = tk.Frame(panel, bg=WHITE)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        label(header, "最近任务", size=15, bold=True).pack(side="left")
        button(header, "刷新记录", lambda: self.show_page("history")).pack(side="right")
        tree = self._table(
            panel, (("创建时间", 185), ("状态", 100), ("任务标识", 330), ("更新时间", 185)), row=1
        )
        history = read_history(self.app.app_paths.task_database)
        for run in history.runs:
            tree.insert(
                "",
                "end",
                iid=run.run_id,
                values=(
                    run.created_at[:19].replace("T", " "),
                    STATE_LABELS.get(run.state, run.state),
                    run.run_id,
                    run.updated_at[:19].replace("T", " "),
                ),
            )
        label(
            panel,
            history.error
            or (
                "选择记录后查看已保存渠道明细。仅展示最近 200 次任务。"
                if history.runs
                else "本机尚无任务记录。完成首次运行后，任务检查点会保存在这里。"
            ),
            color=MUTED,
            size=11,
        ).grid(row=2, column=0, sticky="ew", pady=(12, 8))
        button(panel, "查看任务明细", lambda: self._history_detail(tree)).grid(
            row=3, column=0, sticky="e"
        )

    def _history_detail(self, tree):
        selected = tree.selection()
        if not selected:
            return
        rows, error = read_task_rows(self.app.app_paths.task_database, selected[0])
        window = tk.Toplevel(self.root)
        window.title("任务明细 · 只读")
        window.geometry("980x600")
        window.configure(bg=WHITE)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        label(window, f"任务 {selected[0]}", size=13, bold=True).grid(
            row=0, column=0, sticky="w", padx=18, pady=16
        )
        table = self._table(
            window,
            (("商品", 240), ("渠道", 80), ("价格", 100), ("状态", 120), ("截图", 140)),
            row=1,
        )
        for row in rows:
            table.insert(
                "",
                "end",
                iid=row.task_id,
                values=(
                    row.model_name,
                    CHANNEL_LABELS.get(row.channel),
                    self._price(row.price),
                    row.state_label,
                    row.evidence_label,
                ),
            )
        label(
            window,
            error or "双击记录查看详情；此窗口不会恢复、取消或修改任务。",
            color=MUTED,
            size=11,
        ).grid(row=2, column=0, sticky="w", padx=18, pady=14)

        def details(_event):
            selected_rows = table.selection()
            if selected_rows:
                row = next(item for item in rows if item.task_id == selected_rows[0])
                messagebox.showinfo("已保存任务详情", self._task_detail(row), parent=window)

        def open_selected_evidence():
            selection = table.selection()
            if selection:
                row = next(item for item in rows if item.task_id == selection[0])
                if row.evidence_path:
                    self._open_path(row.evidence_path)

        evidence = button(window, "打开选中截图", open_selected_evidence, state="disabled")
        evidence.grid(row=3, column=0, sticky="e", padx=18, pady=(0, 12))

        def update_evidence_button(_event):
            selection = table.selection()
            row = next((item for item in rows if selection and item.task_id == selection[0]), None)
            available = (
                row is not None and row.evidence_path is not None and row.evidence_path.is_file()
            )
            evidence.configure(state="normal" if available else "disabled")

        table.bind("<<TreeviewSelect>>", update_evidence_button)
        table.bind("<Double-1>", details)

    def _show_path(self, value):
        messagebox.showinfo("完整路径", value or "尚未选择", parent=self.root)

    def _open_evidence(self):
        if self.selected_evidence:
            self._open_path(self.selected_evidence)

    def _open_quote(self):
        if self.model.quote_path:
            self._open_path(self.model.quote_path)

    def _open_path(self, path: Path):
        if not path.is_file():
            self.append_log(f"文件不存在：{path}")
            return
        try:
            if sys.platform == "win32":
                import os

                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as error:
            self.append_log(f"无法打开文件：{error}")

    def begin_run(self):
        self.model.begin_run()
        self.show_page("intelligence")

    def apply_event(self, event: WorkerEvent):
        self.model.apply_event(event)
        self.refresh()

    def finish(self, result: FullPipelineResult | BaseException):
        self.model.finish(result)
        if self.model.run_id:
            rows, error = read_task_rows(self.app.app_paths.task_database, self.model.run_id)
            if not error:
                self.model.rows = rows
            elif error:
                self.append_log(error)
        self.refresh()

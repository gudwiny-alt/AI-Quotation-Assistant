"""Native blue-white workbench; business actions stay on QuoteApp."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from PIL import Image, ImageTk

from quote_app.resources import bundled_resource_path
from quote_app.desktop_controls import MonthPicker, SoftEntry, SoftSelect, SoftScrolledText
from quote_app.desktop_scrolling import bind_scrolling, scroll_canvas
from quote_app.desktop_widgets import (
    Artwork,
    IconMedallion,
    RichTable,
    SoftButton,
    SlimScrollbar,
    StatusPill,
    product_subtitle,
    numeric_price,
)

from quote_app.desktop_state import (
    CHANNEL_LABELS,
    STATE_LABELS,
    DesktopState,
    TaskRow,
    compact_path,
    read_history,
    read_task_rows,
)

if TYPE_CHECKING:
    from quote_app.app import QuoteApp
    from quote_app.browser.worker import WorkerEvent
    from quote_app.services.full_pipeline import FullPipelineResult

BG = "#F9FBFF"
SIDEBAR = "#F2F7FE"
FONT = ".AppleSystemUIFont" if sys.platform == "darwin" else "Microsoft YaHei UI"
WHITE = "#FFFFFF"
INK = "#132443"
MUTED = "#72829D"
BLUE = "#2468F5"
LINE = "#DFE7F3"
PALE = "#EAF1FF"
GREEN = "#0B9975"
ORANGE = "#D88119"
PAGES = (
    ("overview", "layout-dashboard", "协同总览", "本月报价执行与结果概览"),
    ("intelligence", "chart-no-axes-column-increasing", "价格情报智能体", "全渠道取价与证据留存"),
    ("decision", "file-text", "报价决策智能体", "多源价格比较与报价规则处理"),
    ("audit", "shield-check", "稽核审查智能体", "证据核验与异常追溯"),
    ("history", "history", "任务记录", "本机已保存任务 · 只读查看"),
    ("settings", "settings", "系统设置", "浏览器登录、截图权限与本机存储"),
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
        font=(FONT, size, "bold" if bold else "normal"),
        fg=color,
        bg=bg,
        anchor=kwargs.pop("anchor", "w"),
        **kwargs,
    )


def button(parent: tk.Misc, text: str, command, *, primary: bool = False, **kwargs):
    return SoftButton(parent, text=text, command=command, primary=primary, **kwargs)


class RoundedCard(tk.Frame):
    """A native frame with a decorative rounded canvas behind its normal widgets."""

    def __init__(self, parent, **kwargs):
        background = parent.cget("bg")
        super().__init__(parent, bg=background, **kwargs)
        self.border = tk.Canvas(self, bg=background, highlightthickness=0, borderwidth=0)
        self.border._decorative_card = True
        self.border.place(x=0, y=0, relwidth=1, relheight=1, bordermode="ignore")
        self.border.tk.call("lower", self.border._w)
        self.border.bind("<Configure>", self._draw_border)

    def _draw_border(self, event):
        width, height, radius = event.width - 1, event.height - 1, 8
        self.border.delete("all")
        self.border.create_polygon(
            radius,
            1,
            width - radius,
            1,
            width,
            1,
            width,
            radius,
            width,
            height - radius,
            width,
            height,
            width - radius,
            height,
            radius,
            height,
            1,
            height,
            1,
            height - radius,
            1,
            radius,
            1,
            1,
            fill=WHITE,
            outline=LINE,
            smooth=True,
            splinesteps=20,
        )


def card(parent: tk.Misc, **kwargs):
    return RoundedCard(parent, **kwargs)


class DesktopWorkbench:
    def __init__(self, app: QuoteApp, *, title: str, credit: str, modes: tuple[str, ...]):
        self.app, self.root = app, app.root
        self.model = DesktopState()
        self.artwork = Artwork(self.root)
        self.page = "overview"
        self.filter = "全部"
        self.overview_tab = "tasks"
        self._run_context: tuple[str, str, str, str] | None = None
        self._icons: dict[tuple[str, str, int], ImageTk.PhotoImage | None] = {}
        self._trace_ids: list[tuple[tk.StringVar, str]] = []
        self._configure_styles()
        self.root.title(title)
        startup_height = min(1020, max(720, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"1280x{startup_height}")
        self.root.minsize(1000, 720)
        self.root.configure(bg=BG)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        self._sidebar(credit)
        self.main = tk.Frame(self.root, bg=BG, padx=26, pady=18)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(3, weight=1)
        self._header()
        self._task_bar(modes)
        self._metrics()
        self.body = tk.Frame(self.main, bg=BG)
        self.body.grid(row=3, column=0, sticky="nsew", pady=(12, 10))
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self._log()
        self.root.bind("<Configure>", self._resize_log, add="+")
        bind_scrolling(self.root, self._scroll_wheel)
        modifier = "Command" if sys.platform == "darwin" else "Control"
        for index, (page, *_rest) in enumerate(PAGES, 1):
            self.root.bind(f"<{modifier}-Key-{index}>", partial(self._navigate_key, page=page))
        for index, tab in enumerate(("tasks", "data", "reports"), 1):
            self.root.bind(
                f"<{modifier}-Shift-Key-{index}>",
                partial(self._navigate_key, page="overview", tab=tab),
            )
        for symbol, tab in zip(("exclam", "at", "numbersign"), ("tasks", "data", "reports")):
            self.root.bind(
                f"<{modifier}-Shift-Key-{symbol}>",
                partial(self._navigate_key, page="overview", tab=tab),
            )
        self.show_page("overview")

    def _configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Workbench.TButton",
            font=(FONT, 11),
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
            font=(FONT, 12, "bold"),
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
        for name, background, foreground in (
            ("Nav.TButton", SIDEBAR, INK),
            ("NavActive.TButton", BLUE, WHITE),
        ):
            style.configure(
                name,
                font=(FONT, 13, "bold" if name == "NavActive.TButton" else "normal"),
                padding=(17, 15),
                background=background,
                foreground=foreground,
                anchor="w",
                borderwidth=0,
                bordercolor=background,
                lightcolor=background,
                darkcolor=background,
                relief="flat",
                focuscolor=background,
            )
            style.map(
                name,
                background=[("active", "#1556DA" if name == "NavActive.TButton" else PALE)],
                foreground=[("active", WHITE if name == "NavActive.TButton" else BLUE)],
            )
        style.configure("TCombobox", padding=5, fieldbackground=WHITE)
        style.configure("TEntry", padding=5)
        style.configure(
            "Workbench.Treeview",
            font=(FONT, 12),
            rowheight=54,
            background=WHITE,
            fieldbackground=WHITE,
            foreground=INK,
            borderwidth=0,
            bordercolor=WHITE,
        )
        style.configure(
            "Workbench.Treeview.Heading",
            font=(FONT, 11, "bold"),
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

    def _icon(self, name, color="blue", size=24):
        key = (name, color, size)
        if key not in self._icons:
            path = bundled_resource_path(f"assets/ui-icons/{name}-{color}.png")
            try:
                with Image.open(path) as source:
                    source = source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
                    self._icons[key] = ImageTk.PhotoImage(source, master=self.root)
            except (OSError, tk.TclError, TypeError, AttributeError):
                self._icons[key] = None
        return self._icons[key]

    def _icon_label(self, parent, name, *, color="blue", size=24, bg=WHITE):
        picture = self._icon(name, color, size)
        if size >= 30 and picture and name != "box":
            return IconMedallion(parent, picture, size, bg=bg, tone=color)
        return label(parent, image=picture or "", bg=bg, width=size if picture else 2)

    def _sidebar(self, credit):
        sidebar = tk.Frame(
            self.root, width=236, bg=SIDEBAR, highlightbackground=LINE, highlightthickness=1
        )
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(9, weight=1)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.grid(row=0, column=0, sticky="ew", padx=21, pady=(29, 33))
        self._icon_label(brand, "box", size=30, bg=SIDEBAR).grid(
            row=0, column=0, rowspan=2, padx=(0, 10)
        )
        label(brand, "铺货报价智能体", size=15, bold=True, bg=SIDEBAR).grid(
            row=0, column=1, sticky="w"
        )
        label(brand, "福建分公司 · 终端业务", size=10, color=MUTED, bg=SIDEBAR).grid(
            row=1, column=1, sticky="w", pady=(3, 0)
        )
        self.nav = {}
        for index, (key, icon, title, _) in enumerate(PAGES):
            if index == 4:
                tk.Frame(sidebar, bg=LINE, height=1).grid(
                    row=5, column=0, sticky="ew", padx=20, pady=(20, 14)
                )
            nav = ttk.Button(
                sidebar,
                text="   " + title,
                command=lambda key=key: self.show_page(key),
                style="Nav.TButton",
                image=self._icon(icon, "muted", 22) or "",
                compound="left",
                width=0,
                cursor="hand2",
            )
            nav.grid(row=index + 1 + (index >= 4), column=0, sticky="ew", padx=14, pady=4)
            self.nav[key] = nav
        local = tk.Frame(sidebar, bg=SIDEBAR)
        local.grid(row=10, column=0, sticky="ew", padx=24, pady=(10, 6))
        self._icon_label(local, "monitor", color="muted", size=18, bg=SIDEBAR).pack(
            side="left", padx=(0, 8)
        )
        label(
            local,
            "Mac 本地运行" if sys.platform == "darwin" else "本地运行",
            color=MUTED,
            size=10,
            bg=SIDEBAR,
        ).pack(side="left")
        label(
            sidebar,
            ".170 · UI 预览版  |  ⌘1–6 切页"
            if sys.platform == "darwin"
            else ".170 · UI 预览版  |  Ctrl 1–6",
            color=MUTED,
            size=9,
            bg=SIDEBAR,
        ).grid(row=11, column=0, sticky="w", padx=24, pady=(4, 3))
        label(sidebar, credit, color=MUTED, size=9, bg=SIDEBAR).grid(
            row=12, column=0, sticky="w", padx=24, pady=(0, 20)
        )

    def _header(self):
        frame = tk.Frame(self.main, bg=BG)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        frame.columnconfigure(0, weight=1)
        self.heading = label(frame, size=24, bold=True, bg=BG)
        self.heading.grid(row=0, column=0, sticky="w")
        self.subheading = label(frame, color=MUTED, size=12, bg=BG)
        self.subheading.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.period = label(frame, size=11, color=MUTED, bg=BG, justify="right", anchor="e")
        self.period.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 16))
        self.start_button = button(frame, "开始自动报价", self.app.run, primary=True)
        self.start_button.grid(row=0, column=2, rowspan=2, sticky="e")

    def _task_bar(self, modes):
        self.context = tk.Frame(self.main, bg=BG)
        self.context.grid(row=1, column=0, sticky="ew")
        self.context.columnconfigure(0, weight=1)
        self.tabs = tk.Frame(self.context, bg=BG)
        self.tabs.grid(row=0, column=0, sticky="ew")
        self.tab_buttons = {}
        for key, title in (("tasks", "任务总览"), ("data", "数据准备"), ("reports", "报表结果")):
            control = button(
                self.tabs, title, lambda key=key: self.show_overview_tab(key), padding=(20, 6)
            )
            control.pack(side="left", padx=(0, 8))
            self.tab_buttons[key] = control
        self.stages = tk.Frame(self.context, bg=BG)
        self.stage_buttons = {}
        for index, (key, title) in enumerate(
            (("intelligence", "价格情报"), ("decision", "报价决策"), ("audit", "稽核审查"))
        ):
            self.stages.columnconfigure(index, weight=1, uniform="stage")
            control = button(
                self.stages,
                f"{index + 1}   {title}",
                lambda key=key: self.show_page(key),
                padding=(10, 10),
            )
            control.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            self.stage_buttons[key] = control
        self.run_controls = bar = card(self.context, padx=18, pady=13)
        bar.columnconfigure(4, weight=1)
        label(bar, "报价月份", size=11, color=MUTED).grid(row=0, column=0, padx=(0, 10))
        MonthPicker(bar, yearvariable=self.app.year_var, monthvariable=self.app.month_var).grid(
            row=0, column=1
        )
        label(bar, "运行范围", size=11, color=MUTED).grid(row=0, column=2, padx=(24, 10))
        SoftSelect(
            bar, textvariable=self.app.brand_mode_var, values=modes, state="readonly", width=170
        ).grid(row=0, column=3)

    def _metrics(self):
        row = self.metrics_frame = tk.Frame(self.main, bg=BG)
        row.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.metrics = []
        self.metric_icons = []
        for index, (color, icon, icon_color) in enumerate(
            ((INK, "files", "blue"), (BLUE, "circle-check", "green"), (ORANGE, "clock", "orange"))
        ):
            row.columnconfigure(index, weight=1, uniform="metric")
            panel = card(row, padx=18, pady=10)
            panel.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=(0 if index == 0 else 6, 0 if index == 2 else 6),
            )
            icon_widget = self._icon_label(panel, icon, color=icon_color, size=36)
            icon_widget.grid(row=0, column=0, rowspan=2, padx=(0, 16))
            self.metric_icons.append(icon_widget)
            title = label(panel, color=INK, size=12)
            title.grid(row=0, column=1, sticky="w")
            number = label(panel, "0", size=25, bold=True, color=color)
            number.grid(row=1, column=1, sticky="w", pady=(1, 0))
            self.metrics.append((title, number))

    def _log(self):
        panel = card(self.main, padx=18, pady=12)
        panel.grid(row=4, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)
        self.log_heading = label(panel, "最近执行动态", size=14, bold=True)
        self.log_heading.grid(row=0, column=0, sticky="w")
        self.app.status = SoftScrolledText(
            panel,
            height=2,
            width=45,
            state="disabled",
            wrap="word",
            bg=WHITE,
            fg="#61769A",
            font=(FONT, 11),
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=4,
            spacing3=4,
        )
        self.app.status.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        self.log_actions = tk.Frame(panel, bg=WHITE)
        self.app.continue_button = button(
            self.log_actions,
            "继续当前任务",
            self.app.continue_current_task,
            primary=True,
            state="disabled",
            padding=(12, 6),
        )
        self.app.cancel_button = button(
            self.log_actions,
            "取消网页任务",
            self.app.cancel_manual_action,
            state="disabled",
            padding=(12, 6),
        )
        self.log_separator = tk.Frame(self.log_actions, bg=LINE, height=1)
        self.app.open_button = button(
            self.log_actions,
            "打开输出目录",
            self.app.open_output_directory,
            state="disabled",
            padding=(12, 6),
            image=self._icon("folder-open", "blue", 18),
        )
        self._log_compact = None
        self._arrange_log_actions(False)
        self.task_status = label(self.main, "尚未开始任务", size=10, color=MUTED, bg=BG)
        self.task_status.grid(row=5, column=0, sticky="ew", pady=(7, 0))

    def _arrange_log_actions(self, compact):
        if compact == self._log_compact:
            return
        self._log_compact = compact
        controls = (self.app.continue_button, self.app.cancel_button, self.app.open_button)
        for widget in (*controls, self.log_separator):
            widget.grid_forget()
        for column in range(3):
            self.log_actions.columnconfigure(column, weight=0, minsize=0)
        if compact:
            # Short windows use a single tidy toolbar to preserve the page viewport.
            self.log_actions.grid(row=0, column=1, sticky="e", padx=(16, 0), rowspan=1)
            self.app.status.grid(columnspan=2)
            for index, control in enumerate(controls):
                control.grid(row=0, column=index, padx=(6, 0), sticky="ew")
        else:
            self.log_actions.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(24, 0))
            self.log_actions.columnconfigure(0, weight=1, minsize=158)
            self.app.status.grid(columnspan=1)
            controls[0].grid(row=0, column=0, sticky="ew")
            controls[1].grid(row=1, column=0, sticky="ew", pady=(7, 0))
            self.log_separator.grid(row=2, column=0, sticky="ew", pady=9)
            controls[2].grid(row=3, column=0, sticky="ew")

    def _resize_log(self, event):
        if event.widget is not self.root:
            return
        # macOS may deliver an older Configure event after the new geometry is applied.
        height = self.root.winfo_height()
        lines = min(12, 2 + max(0, height - 850) // 30)
        if int(self.app.status.cget("height")) != lines:
            self.app.status.configure(height=lines)
        self._arrange_log_actions(height < 900)

    def append_log(self, message: str):
        text = self.app.status
        text.configure(state="normal")
        text.insert(tk.END, f"•  {datetime.now():%H:%M:%S}   {message}\n")
        text.see(tk.END)
        text.configure(state="disabled")

    def show_page(self, page: str):
        self.page = page
        self.filter = "全部"
        if page in {"history", "settings"}:
            self.context.grid_remove()
        else:
            self.context.grid()
        self.tabs.grid_remove()
        self.stages.grid_remove()
        self.run_controls.grid_remove()
        self.metrics_frame.grid_remove()
        if page == "overview":
            self.tabs.grid()
            if self.overview_tab == "data":
                self.run_controls.grid(row=1, column=0, sticky="ew", pady=(14, 0))
            elif self.overview_tab == "tasks":
                self.metrics_frame.grid()
        elif page not in {"history", "settings"}:
            self.stages.grid(row=0, column=0, sticky="ew")
            self.metrics_frame.grid()
        for key, nav in self.nav.items():
            icon = next(item[1] for item in PAGES if item[0] == key)
            nav.configure(
                style="NavActive.TButton" if key == page else "Nav.TButton",
                image=self._icon(icon, "white" if key == page else "muted", 22) or "",
            )
        for key, control in self.stage_buttons.items():
            control.configure(style="Primary.TButton" if key == page else "Workbench.TButton")
        for key, control in self.tab_buttons.items():
            control.configure(
                style="Primary.TButton" if key == self.overview_tab else "Workbench.TButton"
            )
        entry = next(item for item in PAGES if item[0] == page)
        self.heading.configure(text=entry[2])
        self.subheading.configure(text=entry[3])
        self.log_heading.configure(
            text={"overview": "最近执行动态", "decision": "规则处理记录", "audit": "核验记录"}.get(
                page, "执行日志"
            )
        )
        for variable, trace_id in self._trace_ids:
            variable.trace_remove("write", trace_id)
        self._trace_ids.clear()
        for child in self.body.winfo_children():
            child.destroy()
        self.table = self.detail = self.empty = None
        if page == "overview":
            self._overview()
        elif page == "settings":
            self._settings()
        elif page == "history":
            self._history()
        else:
            self._workbench()
        self.refresh()

    def _navigate_key(self, _event=None, *, page, tab=None):
        if tab is not None:
            self.show_overview_tab(tab)
        else:
            self.show_page(page)
        return "break"

    def show_overview_tab(self, tab: str):
        if tab not in {"tasks", "data", "reports"}:
            raise ValueError(f"Unknown overview tab: {tab}")
        self.overview_tab = tab
        self.show_page("overview")

    def _scrollable(self, parent):
        viewport = tk.Frame(parent, bg=BG)
        viewport.grid(row=0, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        canvas = tk.Canvas(
            viewport, bg=BG, highlightthickness=0, borderwidth=0, yscrollincrement=1
        )
        scrollbar = SlimScrollbar(viewport, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        content = tk.Frame(canvas, bg=BG)
        content.columnconfigure(0, weight=1)
        content._workbench_scroll_canvas = canvas
        canvas._workbench_scroll_canvas = canvas
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        content.bind(
            "<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        return content

    def _scroll_wheel(self, event, *, precise=False):
        widget = event.widget
        while widget is not None:
            if widget.winfo_class() in {"Text", "Listbox", "Treeview"}:
                # These already scrolled through their native class bindings.
                return None
            canvas = getattr(widget, "_workbench_scroll_canvas", None)
            if canvas is not None:
                return scroll_canvas(canvas, event, precise=precise)
            widget = getattr(widget, "master", None)
        return None

    def _overview(self):
        content = self._scrollable(self.body)
        if self.overview_tab == "tasks":
            self._dashboard(content)
        elif self.overview_tab == "data":
            self._data_preparation(content)
        else:
            self._reports(content)

    def _dashboard(self, content):
        top = tk.Frame(content, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=3, uniform="dashboard")
        top.columnconfigure(1, weight=2, uniform="dashboard")
        progress = card(top, padx=18, pady=14)
        progress.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        progress.columnconfigure(1, weight=1)
        label(progress, "本批执行概览", size=17, bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        label(progress, "已接收渠道记录 · 以截图保存为完成口径", size=10, color=MUTED).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 13)
        )
        self.channel_counts, self.channel_bars = {}, {}
        for index, (key, title) in enumerate(CHANNEL_LABELS.items()):
            line = tk.Frame(progress, bg="#F2F6FD", padx=12, pady=8)
            line.grid(row=index + 2, column=0, columnspan=3, sticky="ew", pady=3)
            line.columnconfigure(2, weight=1)
            label(line, image=self.artwork.channel(key, size=25) or "", bg="#F2F6FD").grid(
                row=0, column=0, padx=(0, 10)
            )
            label(line, title, size=12, bg="#F2F6FD").grid(row=0, column=1, padx=(0, 16))
            bar = tk.Canvas(line, height=9, width=90, bg="#F2F6FD", highlightthickness=0)
            bar.grid(row=0, column=2, sticky="ew")
            bar.bind("<Configure>", lambda _event, key=key: self._draw_channel_bar(key))
            self.channel_bars[key] = bar
            count = label(line, "0 / 0", size=11, bg="#F2F6FD", width=7, anchor="e")
            count.grid(row=0, column=3, padx=(12, 0))
            self.channel_counts[key] = count
        reminders = card(top, padx=18, pady=14)
        reminders.grid(row=0, column=1, sticky="nsew")
        reminders.columnconfigure(0, weight=1)
        label(reminders, "待办提醒", size=17, bold=True).grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.reminder_counts = {}
        for index, (key, title, icon) in enumerate(
            (
                ("login", "等待登录 / 验证", "clock"),
                ("evidence", "已有结果 · 截图待补", "image"),
                ("error", "技术异常记录", "triangle-alert"),
            )
        ):
            line = tk.Frame(reminders, bg=WHITE)
            line.grid(row=index + 1, column=0, sticky="ew", pady=9)
            line.columnconfigure(1, weight=1)
            self._icon_label(line, icon, color="orange", size=22).grid(
                row=0, column=0, padx=(0, 10)
            )
            label(line, title, size=11).grid(row=0, column=1, sticky="w")
            count = label(line, "0 条", color=MUTED, size=11)
            count.grid(row=0, column=2, padx=(8, 0))
            self.reminder_counts[key] = count
        button(reminders, "查看核验清单  →", lambda: self.show_page("audit")).grid(
            row=4, column=0, sticky="ew", pady=(12, 0)
        )
        agents = tk.Frame(content, bg=BG)
        agents.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        for index, (key, icon, title, subtitle) in enumerate(PAGES[1:4]):
            agents.columnconfigure(index, weight=1, uniform="agent")
            panel = card(agents, padx=17, pady=12)
            panel.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 6, 0 if index == 2 else 6),
            )
            self._icon_label(panel, icon, size=24).pack(anchor="w")
            label(panel, title, size=14, bold=True).pack(anchor="w", pady=(9, 5))
            label(panel, subtitle, size=10, color=MUTED, wraplength=230, justify="left").pack(
                anchor="w"
            )
            button(
                panel,
                ("查看采集任务", "查看报价明细", "查看核验记录")[index] + "  →",
                lambda key=key: self.show_page(key),
            ).pack(fill="x", pady=(10, 0))

    def _draw_channel_bar(self, key):
        canvas = self.channel_bars[key]
        width = canvas.winfo_width()
        if not isinstance(width, int):
            return
        rows = [row for row in self.model.rows if row.channel == key]
        complete = sum(row.evidence_state == "complete" for row in rows)
        canvas.delete("all")
        canvas.create_line(5, 5, max(5, width - 5), 5, fill=LINE, width=8, capstyle="round")
        if complete and rows:
            canvas.create_line(
                5,
                5,
                max(5, (width - 10) * complete / len(rows) + 5),
                5,
                fill=BLUE,
                width=8,
                capstyle="round",
            )

    def _data_preparation(self, content):
        intro = tk.Frame(content, bg=BG)
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        label(intro, "准备本月报价资料", size=18, bold=True, bg=BG).pack(anchor="w")
        label(
            intro,
            "线上渠道启动后自动取价；内部系统资料通过本地 Excel 导入。",
            color=MUTED,
            size=11,
            bg=BG,
        ).pack(anchor="w", pady=(5, 0))
        cards = tk.Frame(content, bg=BG)
        cards.grid(row=1, column=0, sticky="ew")
        for index in range(3):
            cards.columnconfigure(index, weight=1, uniform="source")
        online = card(cards, padx=16, pady=17)
        online.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        heading = tk.Frame(online, bg=WHITE)
        heading.pack(fill="x")
        self._icon_label(heading, "globe", size=32).pack(side="left", padx=(0, 10))
        label(heading, "线上价格渠道", size=13, bold=True).pack(side="left")
        label(online, "官网 · 天猫 · 京东", size=10, color=MUTED).pack(anchor="w", pady=(13, 0))
        self.online_source_state = label(online, "待启动采集", size=11, color=ORANGE)
        self.online_source_state.pack(anchor="w", pady=(13, 6))
        label(online, "自动取价与截图留存", size=10, color=MUTED).pack(anchor="w")
        channels = tk.Frame(online, bg="#F2F6FD", padx=8, pady=9)
        channels.pack(fill="x", pady=(17, 0))
        for index, (channel, title) in enumerate(CHANNEL_LABELS.items()):
            channels.columnconfigure(index, weight=1, uniform="channels")
            item = tk.Frame(channels, bg="#F2F6FD")
            item.grid(row=0, column=index, sticky="ew")
            label(item, image=self.artwork.channel(channel, size=28) or "", bg="#F2F6FD").pack()
            label(item, title, size=10, bg="#F2F6FD", anchor="center").pack(pady=(4, 0))
        sources = (
            ("一级终端营销系统", "营销商品信息查询表 · 本地导入", self.app.marketing_var),
            ("福建移动 BOSS 系统", "BOP 资源信息表 · 本地导入", self.app.bop_var),
        )
        for index, (title, subtitle, variable) in enumerate(sources, 1):
            panel = card(cards, padx=16, pady=17)
            panel.grid(row=0, column=index, sticky="nsew", padx=(6, 0 if index == 2 else 6))
            heading = tk.Frame(panel, bg=WHITE)
            heading.pack(fill="x")
            self._icon_label(heading, "database" if index == 1 else "file-text", size=30).pack(
                side="left", padx=(0, 9)
            )
            title_label = label(heading, title, size=13, bold=True, wraplength=165, justify="left")
            title_label.pack(side="left", fill="x", expand=True)
            title_label.bind(
                "<Configure>",
                lambda event: event.widget.configure(wraplength=max(60, event.width - 4)),
            )
            subtitle_label = label(
                panel, subtitle, size=10, color=MUTED, wraplength=220, justify="left"
            )
            subtitle_label.pack(fill="x", pady=(12, 0))
            subtitle_label.bind(
                "<Configure>",
                lambda event: event.widget.configure(wraplength=max(100, event.width - 4)),
            )
            state = label(
                panel,
                "已选择文件" if variable.get() else "待选择文件",
                size=11,
                color=GREEN if variable.get() else ORANGE,
            )
            state.pack(anchor="w", pady=(12, 4))
            filename = label(
                panel,
                compact_path(variable.get(), 22),
                size=10,
                color=MUTED,
                wraplength=210,
                justify="left",
            )
            filename.pack(fill="x", pady=(0, 13))
            filename.bind(
                "<Configure>",
                lambda event: event.widget.configure(wraplength=max(100, event.width - 4)),
            )

            def update(*_args, variable=variable, filename=filename, state=state):
                filename.configure(text=compact_path(variable.get(), 22))
                state.configure(
                    text="已选择文件" if variable.get() else "待选择文件",
                    fg=GREEN if variable.get() else ORANGE,
                )
                self.refresh()

            self._trace_ids.append((variable, variable.trace_add("write", update)))
            controls = tk.Frame(panel, bg=WHITE)
            controls.pack(fill="x", side="bottom")
            controls.columnconfigure(0, weight=1)
            controls.columnconfigure(1, weight=1)
            button(
                controls,
                "选择文件",
                lambda variable=variable: self.app._choose_file(variable),
                padding=(8, 7),
            ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
            button(
                controls,
                "查看路径",
                lambda variable=variable: self._show_path(variable.get()),
                padding=(8, 7),
            ).grid(row=0, column=1, sticky="ew")
        base = card(content, padx=18, pady=12)
        base.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        base.columnconfigure(2, weight=1)
        self._icon_label(base, "file-text", size=26).grid(row=0, column=0, rowspan=2, padx=(0, 12))
        label(base, "基础报价表", size=12, bold=True).grid(
            row=0, column=1, sticky="w", padx=(0, 20)
        )
        base_state = label(
            base,
            "已选择文件" if self.app.base_var.get() else "待选择文件",
            size=10,
            color=GREEN if self.app.base_var.get() else ORANGE,
        )
        base_state.grid(row=1, column=1, sticky="w", pady=(3, 0))
        base_filename = label(
            base, compact_path(self.app.base_var.get(), 28), size=10, color=MUTED, wraplength=300
        )
        base_filename.grid(row=0, column=2, rowspan=2, sticky="ew", padx=(0, 12))
        base_filename.bind(
            "<Configure>",
            lambda event: event.widget.configure(wraplength=max(100, event.width - 4)),
        )
        button(
            base, "选择文件…", lambda: self.app._choose_file(self.app.base_var), padding=(8, 7)
        ).grid(row=0, column=3, rowspan=2, padx=(0, 7))
        button(
            base, "查看路径", lambda: self._show_path(self.app.base_var.get()), padding=(8, 7)
        ).grid(row=0, column=4, rowspan=2)

        def update_base(*_args):
            selected = self.app.base_var.get()
            base_filename.configure(text=compact_path(selected, 28))
            base_state.configure(
                text="已选择文件" if selected else "待选择文件", fg=GREEN if selected else ORANGE
            )
            self.refresh()

        self._trace_ids.append(
            (self.app.base_var, self.app.base_var.trace_add("write", update_base))
        )
        output = card(content, padx=18, pady=14)
        output.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        output.columnconfigure(1, weight=1)
        label(output, "输出目录", size=12, bold=True).grid(row=0, column=0, padx=(0, 15))
        SoftEntry(output, textvariable=self.app.output_dir_var).grid(row=0, column=1, sticky="ew")
        button(
            output, "选择目录…", lambda: self.app._choose_directory(self.app.output_dir_var)
        ).grid(row=0, column=2, padx=(10, 0))
        hint = card(content, padx=18, pady=14)
        hint.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        hint.columnconfigure(0, weight=1)
        label(hint, "运行前准备", bold=True, size=13).grid(row=0, column=0, sticky="w")
        label(
            hint, "完成浏览器登录及截图权限检查后，即可开始自动报价。", color=MUTED, size=11
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        button(hint, "登录与权限  →", lambda: self.show_page("settings")).grid(
            row=0, column=1, rowspan=2, padx=(10, 0)
        )

    def _reports(self, content):
        label(content, "本次任务输出", size=18, bold=True, bg=BG).grid(
            row=0, column=0, sticky="w", pady=(0, 7)
        )
        label(
            content,
            "生成完成后可打开报价工作簿与执行报告，结果以实际文件为准。",
            size=11,
            color=MUTED,
            bg=BG,
        ).grid(row=1, column=0, sticky="w", pady=(0, 18))
        cards = tk.Frame(content, bg=BG)
        cards.grid(row=2, column=0, sticky="ew")
        self.report_buttons, self.report_labels = {}, {}
        for index, (key, title, icon, contents) in enumerate(
            (
                ("quote", "报价工作簿", "file-text", ("商品与规格", "渠道价格与来源", "报价公式")),
                (
                    "report",
                    "执行报告",
                    "files",
                    ("运行汇总与逐行处理状态", "失败步骤与原因", "建议操作"),
                ),
            )
        ):
            cards.columnconfigure(index, weight=1, uniform="reports")
            panel = card(cards, padx=20, pady=20)
            panel.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 7, 0 if index == 1 else 7),
            )
            panel.columnconfigure(0, weight=1)
            heading = tk.Frame(panel, bg=WHITE)
            heading.grid(row=0, column=0, sticky="ew")
            self._icon_label(heading, icon, size=38).pack(side="left", padx=(0, 15))
            label(heading, title, size=17, bold=True).pack(side="left")
            status = label(panel, "尚未生成", size=11, color=MUTED, wraplength=310, justify="left")
            status.grid(row=1, column=0, sticky="ew", pady=(14, 16))
            self.report_labels[key] = status
            tk.Frame(panel, bg=LINE, height=1).grid(row=2, column=0, sticky="ew", pady=(0, 15))
            label(panel, "包含内容", size=12, bold=True).grid(
                row=3, column=0, sticky="w", pady=(0, 8)
            )
            for row, text in enumerate(contents):
                label(panel, "•  " + text, size=11, color=MUTED).grid(
                    row=row + 4, column=0, sticky="w", pady=4
                )
            action = button(
                panel,
                "打开" + title,
                lambda key=key: self._open_report(key),
                primary=True,
                state="disabled",
            )
            action.grid(row=7, column=0, sticky="ew", pady=(18, 0))
            self.report_buttons[key] = action
        hint = card(content, padx=20, pady=18)
        hint.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        hint.columnconfigure(0, weight=1)
        label(hint, "截图证据与历史任务", size=14, bold=True).grid(row=0, column=0, sticky="w")
        label(
            hint, "渠道截图在核验清单中查看，已保存任务在任务记录中查阅。", size=11, color=MUTED
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        button(hint, "查看任务记录  →", lambda: self.show_page("history")).grid(
            row=0, column=1, rowspan=2, padx=(12, 0)
        )
        label(
            content,
            "工作簿可能包含阶段结果；价格已保存与截图完成分别记录。",
            size=10,
            color=MUTED,
            bg=BG,
        ).grid(row=4, column=0, sticky="w", pady=(13, 0))

    def _open_report(self, key):
        path = self.model.quote_path if key == "quote" else self.model.report_path
        if path:
            self._open_path(path)

    def _settings(self):
        content = self._scrollable(self.body)
        columns = tk.Frame(content, bg=BG)
        columns.grid(row=0, column=0, sticky="ew")
        columns.columnconfigure(0, weight=3, uniform="settings")
        columns.columnconfigure(1, weight=2, uniform="settings")
        environment = card(columns, padx=20, pady=20)
        environment.grid(row=0, column=0, sticky="nsew", padx=(0, 13))
        environment.columnconfigure(0, weight=1)
        label(environment, "运行环境", size=18, bold=True).grid(row=0, column=0, sticky="w")
        label(environment, "浏览器与系统权限检查结果显示在下方日志。", size=11, color=MUTED).grid(
            row=1, column=0, sticky="w", pady=(7, 17)
        )
        for index, (icon, title, subtitle) in enumerate(
            (
                ("globe", "Google Chrome", "访问官网、京东与天猫获取价格"),
                ("monitor", "屏幕录制权限", "保存真实网页截图作为取价证据"),
                ("shield-check", "辅助功能权限", "用于网页窗口与截图流程"),
            )
        ):
            line = tk.Frame(environment, bg="#F5F8FD", padx=14, pady=13)
            line.grid(row=index + 2, column=0, sticky="ew", pady=3)
            if title == "Google Chrome":
                label(
                    line, image=self.artwork.get("ui-brands/chrome", 32) or "", bg="#F5F8FD"
                ).grid(row=0, column=0, rowspan=2, padx=(0, 14))
            else:
                self._icon_label(line, icon, size=27, bg="#F5F8FD").grid(
                    row=0, column=0, rowspan=2, padx=(0, 14)
                )
            label(line, title, size=13, bold=True, bg="#F5F8FD").grid(row=0, column=1, sticky="w")
            label(line, subtitle, size=10, color=MUTED, bg="#F5F8FD").grid(
                row=1, column=1, sticky="w", pady=(4, 0)
            )
        button(environment, "检查运行环境", self.app.check_readiness).grid(
            row=5, column=0, sticky="ew", pady=(15, 0)
        )
        storage = card(columns, padx=18, pady=20)
        storage.grid(row=0, column=1, rowspan=2, sticky="nsew")
        storage.columnconfigure(0, weight=1)
        label(storage, "本地存储", size=18, bold=True).grid(row=0, column=0, sticky="w")
        label(storage, "文件与任务记录保存在本机。", size=11, color=MUTED).grid(
            row=1, column=0, sticky="w", pady=(7, 14)
        )
        paths = self.app.app_paths
        for index, (title, path, icon) in enumerate(
            (
                ("应用数据", paths.data_dir, "folder-open"),
                ("任务数据库", paths.task_database, "history"),
                ("截图证据", paths.evidence_dir, "image"),
                ("浏览器资料", paths.browser_profile, "database"),
            )
        ):
            line = tk.Frame(storage, bg=WHITE)
            line.grid(row=index + 2, column=0, sticky="ew", pady=(9, 12))
            line.columnconfigure(1, weight=1)
            self._icon_label(line, icon, size=25).grid(row=0, column=0, rowspan=2, padx=(0, 12))
            label(line, title, size=12, bold=True).grid(row=0, column=1, sticky="w")
            button(
                line, "查看路径", lambda path=path: self._show_path(str(path)), padding=(9, 5)
            ).grid(row=0, column=2, rowspan=2)
            label(line, compact_path(str(path), 14), size=9, color=MUTED, wraplength=95).grid(
                row=1, column=1, sticky="w", pady=(4, 0)
            )
        login = card(columns, padx=20, pady=18)
        login.grid(row=1, column=0, sticky="ew", padx=(0, 13), pady=(14, 0))
        login.columnconfigure(0, weight=1)
        label(login, "渠道登录", size=17, bold=True).grid(row=0, column=0, sticky="w")
        channel_marks = tk.Frame(login, bg=WHITE)
        channel_marks.grid(row=1, column=0, sticky="w", pady=(10, 10))
        for channel in ("jd", "tmall"):
            label(channel_marks, image=self.artwork.channel(channel, size=26) or "").pack(
                side="left", padx=(0, 8)
            )
            label(channel_marks, CHANNEL_LABELS[channel], size=12).pack(side="left", padx=(0, 20))
        label(
            login,
            "遇到登录或安全验证时，在浏览器完成操作后继续当前任务。",
            size=10,
            color=MUTED,
            wraplength=430,
            justify="left",
        ).grid(row=2, column=0, sticky="ew")
        button(login, "首次登录（京东 / 天猫）", self.app.open_login_browser).grid(
            row=3, column=0, sticky="ew", pady=(14, 0)
        )
        about = card(content, padx=20, pady=17)
        about.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        label(about, "关于软件", size=15, bold=True).pack(anchor="w")
        brands = tk.Frame(about, bg=WHITE)
        brands.pack(fill="x", pady=(14, 10))
        for index, name in enumerate(("华为", "OPPO", "vivo", "荣耀", "小米", "苹果")):
            brands.columnconfigure(index, weight=1, uniform="brand_marks")
            item = tk.Frame(brands, bg=WHITE)
            item.grid(row=0, column=index, sticky="ew")
            label(
                item, image=self.artwork.channel("official", name, size=34) or "", anchor="center"
            ).pack()
            label(item, name, size=10, color=MUTED, anchor="center").pack(pady=(3, 0))
        label(
            about,
            "铺货报价智能体     ·     Mac .170 逻辑基线     ·     本地运行",
            size=11,
            color=MUTED,
        ).pack(anchor="w", pady=(8, 0))
        label(
            about,
            "网站取价需要联网，内部业务数据通过本地文件导入。  总览标签：⌘⇧1–3"
            if sys.platform == "darwin"
            else "网站取价需要联网，内部业务数据通过本地文件导入。  总览标签：Ctrl Shift 1–3",
            size=10,
            color=MUTED,
        ).pack(anchor="w", pady=(6, 0))

    def _workbench(self):
        split = tk.Frame(self.body, bg=BG)
        split.grid(row=0, column=0, sticky="nsew")
        split.columnconfigure(0, weight=6, uniform="workbench")
        split.columnconfigure(1, weight=4, uniform="workbench")
        split.rowconfigure(0, weight=1)
        panel = card(split, padx=16, pady=17)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        title = {
            "intelligence": "渠道采集进度",
            "decision": "报价处理清单",
            "audit": "证据核验清单",
        }[self.page]
        label(panel, title, size=17, bold=True).grid(row=0, column=0, sticky="w")
        filters = tk.Frame(panel, bg=WHITE)
        filters.grid(row=1, column=0, sticky="w", pady=(13, 13))
        self.filter_buttons = {}
        options = (
            ("全部", "官网", "天猫", "京东")
            if self.page == "intelligence"
            else (
                ("全部", "待补证据", "截图已保存") if self.page == "audit" else ("全部", "含异常")
            )
        )
        for item in options:
            control = button(filters, item, lambda item=item: self._filter(item), padding=(13, 6))
            control.pack(side="left", padx=(0, 5))
            self.filter_buttons[item] = control
        columns = (("商品", 195), ("渠道", 90), ("价格", 75), ("状态", 102))
        if self.page == "decision":
            columns = (("商品 / 物料", 190), ("最低有效价", 94), ("状态", 100))
        elif self.page == "audit":
            columns = (("商品", 195), ("渠道", 90), ("价格", 82), ("截图状态", 104))
        self.table = RichTable(panel, columns)
        self.table.grid(row=2, column=0, sticky="nsew")
        self.table.bind("<<TreeviewSelect>>", self._selection)
        self.empty = label(panel, "尚无数据", color=MUTED, size=10, wraplength=420, justify="left")
        self.empty.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.empty.bind(
            "<Configure>",
            lambda event: event.widget.configure(wraplength=max(160, event.width - 4)),
        )
        side = card(split, padx=16, pady=17)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)
        label(
            side,
            {"decision": "本条报价依据", "audit": "核验详情"}.get(self.page, "当前采集"),
            size=17,
            bold=True,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        holder = tk.Frame(side, bg=WHITE)
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        info = self._scrollable(holder)
        info.configure(bg=WHITE)
        product = tk.Frame(info, bg=WHITE)
        product.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        product.columnconfigure(1, weight=1)
        picture = tk.Frame(product, bg=WHITE)
        picture.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self.product_picture = label(picture, image=self.artwork.phone(58) or "")
        self.product_picture.pack()
        label(picture, "产品示意", size=8, color=MUTED).pack()
        self.detail_title = label(
            product, "暂无选中商品", size=14, bold=True, wraplength=200, justify="left"
        )
        self.detail_title.grid(row=0, column=1, sticky="ew", pady=(0, 5))
        self.detail_spec = label(
            product,
            "选择左侧记录查看真实结果",
            size=10,
            color=MUTED,
            wraplength=200,
            justify="left",
        )
        self.detail_spec.grid(row=1, column=1, sticky="ew")
        for text_label in (self.detail_title, self.detail_spec):
            text_label.bind(
                "<Configure>",
                lambda event: event.widget.configure(wraplength=max(80, event.width - 4)),
            )
        self.detail_source = label(info, "", size=10, color=MUTED, compound="left")
        self.detail_source.grid(row=1, column=0, sticky="w", pady=(0, 6))
        tiles = tk.Frame(info, bg=WHITE)
        tiles.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.detail_values = []
        self.price_tiles = []
        self.price_marks = []
        labels = (
            ("官网", "天猫", "京东")
            if self.page == "decision"
            else ("渠道", "阶段价格", "截图状态")
        )
        for index, text in enumerate(labels):
            tiles.columnconfigure(index, weight=1, uniform="detail")
            tile = tk.Frame(
                tiles, bg="#F5F8FD", padx=6, pady=10, highlightthickness=1, highlightbackground=LINE
            )
            tile.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 4, 0))
            self.price_tiles.append(tile)
            if self.page == "decision":
                channel = ("official", "tmall", "jd")[index]
                mark = label(tile, image=self.artwork.channel(channel, size=23) or "", bg="#F5F8FD")
                mark.pack(pady=(0, 5))
                self.price_marks.append(mark)
            tile_title = label(tile, text, size=10, color=MUTED, bg="#F5F8FD")
            tile_title.pack(anchor="center")
            value = label(
                tile,
                "—",
                size=17 if self.page == "decision" else 11,
                bold=True,
                color=BLUE,
                bg="#F5F8FD",
                wraplength=68,
            )
            value.pack(fill="x", pady=(7, 0))
            value.bind(
                "<Configure>",
                lambda event: event.widget.configure(wraplength=max(30, event.width - 4)),
            )
            self.detail_values.append(value)
            if self.page != "decision":
                if index != 1:
                    tile.grid_remove()
                else:
                    tile.grid(row=0, column=0, columnspan=3, padx=0)
                    tile_title.pack_configure(side="left", padx=(5, 0))
                    value.pack_configure(side="right", fill="none", pady=0, padx=(12, 4))
                    value.configure(font=(FONT, 18, "bold"), wraplength=160)
        self.detail_rows = []
        for index, name in enumerate(
            ("最低有效价", "物料编码", "处理异常")
            if self.page == "decision"
            else ("业务结果", "任务状态", "截图文件")
        ):
            line = tk.Frame(info, bg=WHITE)
            line.grid(row=index + 3, column=0, sticky="ew", pady=4)
            line.columnconfigure(1, weight=1)
            label(line, name, color=MUTED, size=11).grid(row=0, column=0, sticky="w")
            primary_price = self.page == "decision" and index == 0
            value = (
                label(line, "—", size=24, color=BLUE, bold=True, anchor="e", wraplength=190)
                if primary_price
                else label(line, "—", size=10, color=INK, wraplength=160, justify="right")
                if self.page == "decision" and index == 1
                else StatusPill(line)
            )
            value.grid(row=0, column=1, sticky="e", padx=(9, 0))
            self.detail_rows.append(value)
            if self.page != "decision" and index == 2:
                line.grid_remove()
        tk.Frame(info, bg=LINE, height=1).grid(row=6, column=0, sticky="ew", pady=(4, 7))
        self.detail = SoftScrolledText(
            info,
            width=24,
            height=4,
            wrap="word",
            font=(FONT, 10),
            bg=WHITE,
            fg=MUTED,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            state="disabled",
            spacing3=5,
        )
        self.detail.grid(row=7 if self.page == "decision" else 9, column=0, sticky="ew")
        if self.page != "decision":
            self.detail.configure(height=2)
        self.manual_hint = label(
            info,
            "",
            color=ORANGE,
            size=10,
            bg="#FFF6E8",
            wraplength=280,
            justify="left",
            padx=10,
            pady=10,
        )
        self.manual_hint.bind(
            "<Configure>", lambda e: e.widget.configure(wraplength=max(80, e.width - 24))
        )
        self.preview = None
        self.preview_image = None
        if self.page != "decision":
            self.preview = label(
                info, "暂无可展示的截图证据", size=11, color=MUTED, bg="#F2F6FD", anchor="center"
            )
            self.preview.grid(row=7, column=0, sticky="ew", pady=(0, 7), ipady=25)
        self.evidence_button = (
            button(side, "查看渠道证据  →", lambda: self.show_page("audit"))
            if self.page == "decision"
            else button(side, "打开截图证据", self._open_evidence, state="disabled")
        )
        self.evidence_button.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        if self.page == "decision":
            self.result_button = button(side, "打开报价工作簿", self._open_quote, state="disabled")
            self.result_button.grid(row=3, column=0, sticky="ew", pady=(8, 0))

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
        vertical = SlimScrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = SlimScrollbar(frame, orient="horizontal", command=tree.xview)
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
        configuration = (
            self.app.year_var.get(),
            self.app.month_var.get(),
            self.app.brand_mode_var.get(),
            f"{datetime.now():%Y-%m-%d}",
        )
        preparing = self.page == "overview" and self.overview_tab == "data"
        year, month, brand, day = configuration if preparing else self._run_context or configuration
        self.period.configure(
            text=""
            if self.page in {"history", "settings"}
            else f"{year}年{month}月报价 · {brand}\n"
            + ("下次任务设置" if preparing and self._run_context else day)
        )
        if self.page == "overview" and self.overview_tab == "tasks":
            for key in CHANNEL_LABELS:
                channel_rows = [row for row in rows if row.channel == key]
                complete = sum(row.evidence_state == "complete" for row in channel_rows)
                self.channel_counts[key].configure(text=f"{complete} / {len(channel_rows)}")
                self._draw_channel_bar(key)
            counts = {
                "login": sum(row.state == "waiting_for_login" for row in rows),
                "evidence": sum(
                    bool(row.outcome) and row.evidence_state != "complete" for row in rows
                ),
                "error": sum(bool(row.error) for row in rows),
            }
            for key, count in counts.items():
                self.reminder_counts[key].configure(
                    text=f"{count} 条", fg=ORANGE if count else MUTED
                )
        if self.page == "overview" and self.overview_tab == "data":
            self.online_source_state.configure(
                text="本次任务运行中"
                if self.model.running
                else "已有本次渠道记录"
                if rows
                else "待启动采集",
                fg=BLUE if self.model.running else GREEN if rows else ORANGE,
            )
        if self.page == "overview" and self.overview_tab == "reports":
            for key, path in (("quote", self.model.quote_path), ("report", self.model.report_path)):
                available = path is not None and path.is_file()
                self.report_buttons[key].configure(state="normal" if available else "disabled")
                self.report_labels[key].configure(
                    text=compact_path(str(path), 45)
                    if available
                    else "文件不存在"
                    if path
                    else "任务运行中 · 等待生成"
                    if self.model.running
                    else "尚未生成",
                    fg=GREEN if available else MUTED,
                )
        if self.page == "overview":
            values = (
                (
                    "已接收渠道记录",
                    len(rows),
                ),
                ("截图已保存", self.model.evidence_count),
                (
                    "待补截图记录",
                    sum(bool(row.outcome) and row.evidence_state != "complete" for row in rows),
                ),
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
        appearance = (
            (
                ("files", "blue", INK),
                ("triangle-alert", "orange", ORANGE),
                ("circle-check", "green", GREEN),
            )
            if self.page == "decision"
            else (
                ("files", "blue", INK),
                ("circle-check", "green", BLUE),
                ("clock", "orange", ORANGE),
            )
        )
        for (title, number), icon_widget, (text, count), (icon, icon_color, number_color) in zip(
            self.metrics, self.metric_icons, values, appearance
        ):
            title.configure(text=text)
            number.configure(text=str(count), fg=number_color)
            icon_widget.configure(
                image=self._icon(icon, icon_color, 36) or "",
                **({"tone": icon_color} if isinstance(icon_widget, IconMedallion) else {}),
            )
        self.task_status.configure(text=self.model.summary)
        if self.page == "settings":
            self.start_button.configure(
                text="检查运行环境", command=self.app.check_readiness, state="normal"
            )
        elif self.page == "history":
            self.start_button.configure(
                text="刷新任务记录", command=lambda: self.show_page("history"), state="normal"
            )
        else:
            self.start_button.configure(
                text="开始自动报价",
                command=self.app.run,
                state="disabled" if self.model.running else "normal",
            )
        if self.page not in {"intelligence", "decision", "audit"} or self.table is None:
            return
        selected = self.table.selection()
        for item in self.table.get_children():
            self.table.delete(item)
        if self.page == "decision":
            for index, row in enumerate(self.model.quote_rows):
                if self.filter == "含异常" and not row.issues:
                    continue
                values = (
                    row.web_query.model_name or row.material_code,
                    self._price(row.cells.get("AH", "")),
                    "含异常" if row.issues else "已输出",
                )
                self.table.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=values,
                    image=self.artwork.phone(42),
                    subtitle=product_subtitle(
                        row.web_query.model_name,
                        " / ".join(
                            str(v) for v in (row.web_query.storage, row.web_query.color) if v
                        ),
                    ),
                    badges={2: "orange" if row.issues else "green"},
                    emphasis=(1,),
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
                    image=self.artwork.phone(42),
                    subtitle=product_subtitle(row.model_name, row.specification),
                    icons={1: self.artwork.channel(row.channel, row.model_name, size=17)},
                    badges={
                        3: "orange"
                        if row.error or row.state in {"waiting_for_login", "paused"}
                        else "green"
                        if (
                            row.evidence_state == "complete"
                            if self.page == "audit"
                            else row.state == "succeeded"
                        )
                        else "blue"
                        if row.state == "running"
                        else "muted"
                    },
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
        if value is None or value == "":
            return "—"
        amount = numeric_price(value)
        if amount is None:
            return str(value)
        return f"¥{amount:,.0f}" if amount == amount.to_integral_value() else f"¥{amount:,f}"

    def _selection(self, _event=None):
        if self.detail is None:
            return
        selection = self.table.selection()
        self.selected_evidence = None
        title, spec = "暂无选中商品", "选择左侧记录查看真实结果"
        values, checks = ("—", "—", "—"), ("—", "—", "—")
        tones = ("muted", "muted", "muted")
        self.detail_source.configure(text="", image="")
        self.manual_hint.grid_remove()
        for tile in self.price_tiles:
            tile.configure(highlightbackground=LINE)
        if not selection:
            text = "运行后可查看价格、业务结果与截图证据。"
        elif self.page == "decision":
            row = self.model.quote_rows[int(selection[0])]
            query = row.web_query
            title = query.model_name or row.material_code
            spec = (
                " / ".join(str(part) for part in (query.storage, query.color) if part)
                or f"来源行号 {row.source_row_number}"
            )
            self.detail_source.configure(
                text="  三渠道价格比较 · 本次工作簿输出",
                image=self.artwork.channel("official", title, size=25) or "",
            )
            tones = ("blue", "muted", "orange" if row.issues else "green")
            values = tuple(self._price(row.cells.get(key)) for key in ("AK", "AJ", "AI"))
            # AH is the existing pipeline output, not a newly calculated UI quote.
            minimum = numeric_price(row.cells.get("AH"))
            for tile, mark, channel, key in zip(
                self.price_tiles, self.price_marks, ("official", "tmall", "jd"), ("AK", "AJ", "AI")
            ):
                mark.configure(image=self.artwork.channel(channel, title, size=23) or "")
                chosen = minimum is not None and numeric_price(row.cells.get(key)) == minimum
                tile.configure(highlightbackground=BLUE if chosen else LINE)
            checks = (
                self._price(row.cells.get("AH")),
                row.material_code,
                f"{len(row.issues)} 项" if row.issues else "未记录异常",
            )
            text = "处理记录\n" + (
                "\n".join(f"{issue.code}：{issue.message}" for issue in row.issues)
                if row.issues
                else "本条输出未记录处理异常。"
            )
            text += f"\n\n来源链接  {self._cell(row.cells.get('S'))}\n公式计算结果以实际报价工作簿为准。"
        else:
            task = next((r for r in self.model.rows if r.task_id == selection[0]), None)
            if task is None:
                return
            if (
                task.evidence_state == "complete"
                and task.evidence_path is None
                and self.model.run_id
            ):
                saved_rows, _error = read_task_rows(
                    self.app.app_paths.task_database, self.model.run_id
                )
                saved = next((item for item in saved_rows if item.task_id == task.task_id), None)
                if saved is not None:
                    task = saved
            title, spec = (
                task.model_name or task.task_id,
                task.specification or "商品规格以采集记录为准",
            )
            self.detail_source.configure(
                text="  "
                + CHANNEL_LABELS.get(task.channel, task.channel)
                + " · "
                + task.state_label,
                image=self.artwork.channel(task.channel, title, size=25) or "",
            )
            tones = (
                "green" if task.outcome else "muted",
                "orange"
                if task.state in {"waiting_for_login", "technical_failure", "paused"}
                else "green"
                if task.state == "succeeded"
                else "blue"
                if task.state == "running"
                else "muted",
                "green" if task.evidence_path and task.evidence_path.is_file() else "muted",
            )
            if task.state == "waiting_for_login":
                self.manual_hint.configure(
                    text="等待人工处理\n请在浏览器完成登录或安全验证，再点击下方“继续当前任务”。"
                )
                self.manual_hint.grid(row=8, column=0, sticky="ew", pady=(10, 0))
            elif task.error:
                self.manual_hint.configure(
                    text="需要关注 · " + task.error + "\n请根据实际执行日志核实原因。"
                )
                self.manual_hint.grid(row=8, column=0, sticky="ew", pady=(10, 0))
            values = (
                CHANNEL_LABELS.get(task.channel, task.channel),
                self._price(task.price),
                task.evidence_label,
            )
            if task.evidence_path and task.evidence_path.is_file():
                self.selected_evidence = task.evidence_path
            checks = (
                task.outcome_label,
                task.state_label,
                "可打开" if self.selected_evidence else "暂无可打开文件",
            )
            text = (f"技术异常  {task.error}\n\n" if task.error else "") + (
                f"来源  {task.url}" if task.url else "来源链接尚未载入。"
            )
            text += "\n\n价格保存与截图完成分别记录。"
        self.detail_title.configure(text=title)
        self.product_picture.configure(image=self.artwork.phone(58) or "")
        self.detail_spec.configure(text=spec)
        for widget, value in zip(self.detail_values, values):
            widget.configure(text=value)
        for widget, value, tone in zip(self.detail_rows, checks, tones):
            widget.configure(
                text=value, **({"tone": tone} if isinstance(widget, StatusPill) else {})
            )
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
        self.preview.configure(image="", text="暂无可展示的截图证据")
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
        panel = card(self.body, padx=18, pady=18)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        header = tk.Frame(panel, bg=WHITE)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        label(header, "本机任务记录", size=18, bold=True).pack(side="left")
        label(header, "最近 200 次 · 只读查看", size=10, color=MUTED).pack(side="right")
        filters = tk.Frame(panel, bg=WHITE)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        filters.columnconfigure(1, weight=1)
        self.history_query = tk.StringVar(self.root)
        self.history_state = tk.StringVar(self.root, value="全部状态")
        entry = SoftEntry(
            filters,
            textvariable=self.history_query,
            placeholder="输入任务编号搜索",
            icon=self.artwork.get("ui-icons/search-muted", 20),
            width=420,
        )
        entry.grid(row=0, column=1, sticky="ew")

        self.history_snapshot = read_history(self.app.app_paths.task_database)
        states = tuple(
            dict.fromkeys(
                STATE_LABELS.get(run.state, run.state) for run in self.history_snapshot.runs
            )
        )
        selector = SoftSelect(
            filters,
            textvariable=self.history_state,
            values=("全部状态", *states),
            state="readonly",
            width=160,
        )
        selector.grid(row=0, column=2, padx=(12, 0))
        selector.bind("<<ComboboxSelected>>", lambda _event: self._filter_history())
        self.history_table = RichTable(
            panel, (("创建时间", 175), ("状态", 110), ("任务标识", 250), ("更新时间", 160))
        )
        self.history_table.grid(row=2, column=0, sticky="nsew")
        self.history_table.bind("<<TreeviewSelect>>", self._history_selection)
        self.history_table.canvas.bind(
            "<Double-1>", lambda _event: self._history_detail(self.history_table)
        )
        self.history_hint = label(panel, color=MUTED, size=11)
        self.history_hint.grid(row=3, column=0, sticky="ew", pady=(12, 15))
        detail = tk.Frame(panel, bg="#F2F6FD", padx=18, pady=16)
        detail.grid(row=4, column=0, sticky="ew")
        detail.columnconfigure(0, weight=1)
        self.history_title = label(detail, "选择批次查看记录", size=14, bold=True, bg="#F2F6FD")
        self.history_title.grid(row=0, column=0, sticky="w")
        self.history_meta = label(
            detail, "历史查看不会恢复、取消或修改任务。", size=10, color=MUTED, bg="#F2F6FD"
        )
        self.history_meta.grid(row=1, column=0, sticky="w", pady=(7, 0))
        self.history_detail_button = button(
            detail,
            "查看任务明细  →",
            lambda: self._history_detail(self.history_table),
            primary=True,
            state="disabled",
        )
        self.history_detail_button.grid(row=0, column=1, rowspan=2, padx=(10, 0))
        self._trace_ids.append(
            (
                self.history_query,
                self.history_query.trace_add("write", lambda *_: self._filter_history()),
            )
        )
        self._filter_history()

    def _filter_history(self):
        tree = self.history_table
        selected = tree.selection()
        for item in tree.get_children():
            tree.delete(item)
        query, state = self.history_query.get().strip().casefold(), self.history_state.get()
        for index, run in enumerate(self.history_snapshot.runs):
            status = STATE_LABELS.get(run.state, run.state)
            if (query and query not in run.run_id.casefold()) or (
                state != "全部状态" and status != state
            ):
                continue
            tree.insert(
                "",
                "end",
                iid=run.run_id,
                values=(
                    run.created_at[:19].replace("T", " "),
                    status,
                    run.run_id,
                    run.updated_at[:19].replace("T", " "),
                ),
                badges={
                    1: "green"
                    if run.state == "completed"
                    else "orange"
                    if run.state in {"waiting_for_login", "paused", "failed", "stopped"}
                    else "blue"
                    if run.state == "running"
                    else "muted"
                },
            )
        items = tree.get_children()
        self.history_hint.configure(
            text=self.history_snapshot.error
            or (
                f"显示 {len(items)} 次任务 · 双击记录可查看已保存渠道明细"
                if items
                else "暂无符合条件的任务记录"
                if self.history_snapshot.runs
                else "本机尚无任务记录，首次运行后可在此查阅。"
            )
        )
        if selected and selected[0] in items:
            tree.selection_set(selected[0])
        elif items:
            tree.selection_set(items[0])
        self._history_selection()

    def _history_selection(self, _event=None):
        selected = self.history_table.selection()
        run = next(
            (run for run in self.history_snapshot.runs if selected and run.run_id == selected[0]),
            None,
        )
        self.history_detail_button.configure(state="normal" if run else "disabled")
        self.history_title.configure(
            text=f"批次 · {run.created_at[:19].replace('T', ' ')}" if run else "选择批次查看记录"
        )
        self.history_meta.configure(
            text=f"{STATE_LABELS.get(run.state, run.state)}    ·    最近更新 {run.updated_at[:19].replace('T', ' ')}"
            if run
            else "历史查看不会恢复、取消或修改任务。"
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
        self._run_context = (
            self.app.year_var.get(),
            self.app.month_var.get(),
            self.app.brand_mode_var.get(),
            f"{datetime.now():%Y-%m-%d}",
        )
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

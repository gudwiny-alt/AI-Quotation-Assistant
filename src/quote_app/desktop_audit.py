"""Native audit workbench backed only by the local review session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter import font as tkfont
from typing import Iterable, Sequence

from quote_app.desktop_widgets import RichTable, rounded, TONES


CATEGORY_NAMES = (
    "商品分类与关联",
    "渠道与价格口径",
    "截图内容核验",
    "证据与数据完整性",
    "报价规则与资格",
    "报表与报送一致性",
)
STATUSES = ("通过", "未通过", "待复核", "待补充", "未检查", "不适用")
ACTION_STATUSES = {"未通过", "待复核", "待补充"}
QUEUE_STATUSES = ACTION_STATUSES | {"未检查"}
STATUS_TONES = {
    "通过": ("#E6F8F1", "#0B9975"),
    "未通过": ("#FFECEC", "#E1251B"),
    "待复核": ("#FFF3E3", "#C77912"),
    "待补充": ("#FFF3E3", "#C77912"),
    "未检查": ("#EEF2F8", "#64748B"),
    "不适用": ("#EEF2F8", "#72829D"),
}


def _counts(checks: Iterable[object]) -> dict[str, int]:
    result = {status: 0 for status in STATUSES}
    for item in checks:
        status = getattr(item, "status", "未检查")
        if status in result:
            result[status] += 1
    return result


def _overall(counts: dict[str, int]) -> str:
    if counts["未通过"] and sum(counts[value] for value in ("待复核", "待补充", "未检查")):
        return "需处理"
    for status in ("未通过", "待补充", "待复核", "未检查"):
        if counts[status]:
            return status
    if counts["通过"]:
        return "全部通过"
    return "不适用"


@dataclass(frozen=True)
class AuditProductRow:
    product: object
    counts: dict[str, int]
    overall: str


@dataclass(frozen=True)
class AuditSnapshot:
    summary: dict[str, int]
    category_counts: dict[str, dict[str, int]]
    rows: tuple[AuditProductRow, ...]
    checks: tuple[object, ...]
    category: str | None
    batch_state: str
    available: bool = True

    def visible_checks(self, product_id: str, *, all_categories: bool = False):
        if not self.available:
            return ()
        return tuple(
            item
            for item in self.checks
            if getattr(item, "product_id", None) == product_id
            and (all_categories or not self.category or getattr(item, "category", None) == self.category)
        )


def build_audit_snapshot(
    products: Sequence[object],
    checks: Sequence[object],
    *,
    category: str | None = None,
    exceptions_only: bool = False,
    running: bool = False,
    available: bool = True,
) -> AuditSnapshot:
    """Derive filtered rows without changing any full-batch count."""
    all_checks = tuple(checks)
    total = _counts(all_checks)
    summary = {
        "products": len(products),
        "checks": len(all_checks),
        "通过": total["通过"],
        "需处理": sum(total[value] for value in ACTION_STATUSES),
        "未检查": total["未检查"],
        "不适用": total["不适用"],
    }
    categories = dict.fromkeys(CATEGORY_NAMES)
    for item in all_checks:
        categories.setdefault(getattr(item, "category", ""), None)
    category_counts = {
        name: _counts(item for item in all_checks if getattr(item, "category", None) == name)
        for name in categories
        if name
    }
    scoped = tuple(
        item for item in all_checks if not category or getattr(item, "category", None) == category
    )
    rows = []
    for product in products:
        product_checks = [
            item for item in scoped if getattr(item, "product_id", None) == product.id
        ]
        counts = _counts(product_checks)
        if available and exceptions_only and not any(counts[value] for value in QUEUE_STATUSES):
            continue
        rows.append(
            AuditProductRow(product, counts, _overall(counts) if available else "未检查")
        )
    state = (
        "unavailable"
        if not available
        else "running"
        if running
        else "empty"
        if not products and not all_checks
        else "ready"
    )
    return AuditSnapshot(
        summary, category_counts, tuple(rows), all_checks, category, state, available
    )


class AuditTable(RichTable):
    """Compact workbench table; the shared stable-ID and scrolling API is unchanged."""

    def __init__(self, parent, columns, *, rowheight=64):
        super().__init__(parent, columns, rowheight=rowheight)
        self.body_font.configure(size=-14)
        self.small_font.configure(size=-13)
        self.bold_font.configure(size=-14)
        self.header.configure(height=28)
        self.canvas.configure(height=240 if rowheight == 24 else 384)

    def _paint(self):
        self._pending = None
        c = self.canvas
        width, height = c.winfo_width(), c.winfo_height()
        c.delete("all")
        self.header.delete("all")
        weights = [weight for _, weight in self.columns]
        widths = [width * weight / sum(weights) for weight in weights]
        rounded(self.header, 0, 0, width, 28, fill="#EDF5FF", radius=5)
        x = 0
        for (title, _), w in zip(self.columns, widths):
            self.header.create_text(x + 8, 14, anchor="w", text=title,
                                    font=self.bold_font, fill="#111B65")
            x += w
        total = len(self.records) * self.rowheight
        region = (0, 0, width, max(height, total))
        if region != self._region:
            self._region = region
            c.configure(scrollregion=region, yscrollincrement=1)
        first = max(0, int(c.canvasy(0) // self.rowheight))
        end = min(len(self.records), first + height // self.rowheight + 2)
        for index, iid in enumerate(tuple(self.records)[first:end], first):
            row, y = self.records[iid], index * self.rowheight
            cy = y + self.rowheight / 2
            if iid in self.selected:
                rounded(c, 0, y + 1, width - 1, y + self.rowheight - 1,
                        fill="#E5F0FF", radius=4)
            c.create_line(0, y + self.rowheight, width, y + self.rowheight, fill="#E9F0FB")
            x = 0
            values = row.get("display_values", row["values"])
            for col, (value, w) in enumerate(zip(values, widths)):
                px = x + 8
                if col:
                    c.create_line(x, y + 5, x, y + self.rowheight - 5, fill="#E5EDFA")
                image = row.get("image") if col == 0 else None
                if image:
                    c.create_image(px, cy, anchor="w", image=image)
                    px += image.width() + 7
                tone = row.get("badges", {}).get(col)
                if tone:
                    bg, fg = TONES[tone]
                    if self.rowheight > 30:
                        rounded(c, x + 4, cy - 15, x + w - 3, cy + 15, fill=bg, radius=5)
                    c.create_oval(x + 7, cy - 9, x + 25, cy + 9, fill=fg, outline="")
                    c.create_text(x + 16, cy, text="✓" if tone == "green" else "!" if tone == "red" else "·",
                                  font=self.bold_font, fill="#FFFFFF")
                    c.create_text(x + 31, cy, text=self._fit(value, w - 34, self.small_font),
                                  anchor="w", font=self.small_font, fill=fg)
                elif col == 0 and row.get("subtitle"):
                    c.create_text(px, cy - 11, text=self._fit(value, x + w - px - 5, self.body_font),
                                  anchor="w", font=self.body_font, fill="#111B65")
                    c.create_text(px, cy + 11, text=self._fit(row["subtitle"], x + w - px - 5, self.small_font),
                                  anchor="w", font=self.small_font, fill="#64719B")
                else:
                    lines = str(value).split("\n")
                    for line_index, line in enumerate(lines):
                        c.create_text(px, cy + (line_index - (len(lines) - 1) / 2) * 17,
                                      text=self._fit(line, x + w - px - 5, self.body_font),
                                      anchor="w", font=self.body_font,
                                      fill="#185BCB" if col in row.get("emphasis", ()) else "#111B65")
                x += w


class AuditView:
    """Full-batch summary, one-row-per-product queue, and selectable check detail."""

    def __init__(self, workbench, parent, session):
        from quote_app.desktop_controls import SoftEntry, SoftSelect
        from quote_app.desktop_ui import BG, BLUE, FONT, INK, LINE, MUTED, WHITE, button, card, label
        from quote_app.desktop_widgets import Artwork, SlimScrollbar

        self.workbench, self.parent, self.session = workbench, parent, session
        self.colors = dict(bg=BG, blue=BLUE, ink=INK, line=LINE, muted=MUTED, white=WHITE)
        self.font, self.card = FONT, card
        def pixel_label(parent, text="", *, size=14, **kwargs):
            return label(parent, text, size=-abs(size), **kwargs)
        def pixel_button(parent, text, command, **kwargs):
            control = button(parent, text=text, command=command, **kwargs)
            control._font.configure(size=-14)
            control._measure()
            return control
        self.label, self.button = pixel_label, pixel_button
        self.Artwork, self.RichTable, self.SlimScrollbar = Artwork, AuditTable, SlimScrollbar
        self.SoftEntry, self.SoftSelect = SoftEntry, SoftSelect
        self.artwork = Artwork(parent.winfo_toplevel())
        self.category: str | None = None
        self.exceptions_only = False
        self.show_all_selected = False
        self.group = None
        self.snapshot = build_audit_snapshot([], [])
        self._checks_by_list_index: list[object] = []
        self._products_by_list_index: list[object] = []
        self.container = tk.Frame(parent, bg=BG)
        self.container.grid(row=0, column=0, sticky="nsew")
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(0, weight=1)
        self.page_canvas = tk.Canvas(
            self.container, bg=BG, highlightthickness=0, borderwidth=0, yscrollincrement=1
        )
        self.page_bar = SlimScrollbar(
            self.container, orient="vertical", command=self.page_canvas.yview
        )
        self.page_canvas.configure(yscrollcommand=self.page_bar.set)
        self.page_canvas.grid(row=0, column=0, sticky="nsew")
        self.page_bar.grid(row=0, column=1, sticky="ns")
        self.root = tk.Frame(self.page_canvas, bg=BG)
        self.page_window = self.page_canvas.create_window((0, 0), window=self.root, anchor="nw")
        self.page_canvas.bind(
            "<Configure>",
            self._resize_page,
        )
        self.root.bind(
            "<Configure>",
            lambda _event: self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all")),
        )
        self.root._workbench_scroll_canvas = self.page_canvas
        self.page_canvas._workbench_scroll_canvas = self.page_canvas
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        self._build_summary()
        self._build_categories()
        self._build_workspace()
        self._build_footer()
        self.refresh()
        from quote_app.services.screenshot_review import revision
        self._scan_revision = revision()
        self._scan_timer = self.container.after(1000, self._poll_scans)

    def _poll_scans(self):
        from quote_app.services.screenshot_review import revision
        if not self.container.winfo_exists():
            return
        current = revision()
        if current != self._scan_revision:
            self._scan_revision = current
            self.refresh()
        self._scan_timer = self.container.after(1000, self._poll_scans)

    def _resize_page(self, event):
        columns = 6 if event.width >= 1100 else 3
        self._layout_categories(columns)
        self.page_canvas.itemconfigure(self.page_window, width=event.width)
        self._fit_page_height()

    def _fit_page_height(self, _event=None):
        columns = self._category_columns
        baseline = 84 if columns == 6 else 176
        extra = max(0, self.category_cards.winfo_reqheight() - baseline)
        minimum = (862 if columns == 6 else 956) + extra
        self.page_canvas.itemconfigure(self.page_window,
                                       height=max(self.page_canvas.winfo_height(), minimum))

    def _build_summary(self):
        row = tk.Frame(self.root, bg=self.colors["bg"], height=76)
        row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        row.grid_propagate(False)
        row.rowconfigure(0, weight=1)
        for index in range(4):
            row.columnconfigure(index, weight=1, uniform="audit-metric")
        self.metric_values = {}
        specs = (
            ("products", "本批次商品", "smartphone-blue", self.colors["ink"], "#EAF4FF", "款"),
            ("checks", "检查结果", "files-blue", self.colors["ink"], "#EAF4FF", "项"),
            ("通过", "通过", "circle-check-green", "#0B9975", "#E3F9F0", "项"),
            ("需处理", "待处理", "clock-orange", "#EC8A12", "#FFF3E4", "项"),
        )
        for index, (key, title, icon, color, tint, unit) in enumerate(specs):
            panel = self.card(row, padx=14, pady=8)
            panel.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 5 if index < 3 else 0))
            disc = tk.Canvas(panel, width=56, height=56, bg=self.colors["white"], highlightthickness=0)
            disc.grid(row=0, column=0, rowspan=2, padx=(0, 14))
            disc.create_oval(0, 0, 56, 56, fill=tint, outline="")
            image = self.artwork.get("ui-icons/" + icon, 30)
            if image:
                disc.create_image(28, 28, image=image)
            self.label(panel, title, size=14, bold=True).grid(row=0, column=1, sticky="w")
            numbers = tk.Frame(panel, bg=self.colors["white"])
            numbers.grid(row=1, column=1, sticky="w")
            value = self.label(numbers, "0", size=29, bold=True, color=color)
            value.pack(side="left")
            self.label(numbers, unit, size=14, color=self.colors["muted"]).pack(side="left", padx=7)
            self.metric_values[key] = value

    def _build_categories(self):
        area = tk.Frame(self.root, bg=self.colors["bg"])
        area.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        heading = tk.Frame(area, bg=self.colors["bg"], height=28)
        heading.pack(fill="x")
        self.label(heading, "全批次分类统计", size=16, bold=True, bg=self.colors["bg"]).pack(side="left")
        self.summary_extra = self.label(heading, "未检查 0 · 不适用 0", size=12, color=self.colors["muted"], bg=self.colors["bg"])
        self.summary_extra.pack(side="left", padx=10)
        self.scope_label = self.label(heading, "当前范围：全部检查", size=13, color=self.colors["blue"], bg=self.colors["bg"])
        self.scope_label.pack(side="right")
        self.category_cards = tk.Frame(area, bg=self.colors["bg"])
        self.category_cards.pack(fill="x", pady=(4, 0))
        self.category_cards.bind("<Configure>", self._fit_page_height)
        self.category_buttons, self.category_details, self.category_panels = {}, {}, []
        self.category_status_canvases, self.category_status_values = {}, {}
        self.category_status_font = tkfont.Font(root=self.root, family=self.font, size=-12)
        icons = ("box-blue", "globe-blue", "image-blue", "file-text-blue", "settings-blue", "chart-no-axes-column-increasing-blue")
        for name, icon in zip(CATEGORY_NAMES, icons):
            panel = self.card(self.category_cards, padx=8, pady=6, height=84)
            panel.grid_propagate(False)
            panel.columnconfigure(1, weight=1)
            image = self.artwork.get("ui-icons/" + icon, 26)
            tk.Label(panel, image=image, bg=self.colors["white"]).grid(row=0, column=0, padx=(0, 4))
            title = self.label(panel, name, size=14, bold=True, color=self.colors["ink"], cursor="hand2")
            title.grid(row=0, column=1, sticky="w")
            detail = self.label(panel, "尚无检查", size=12, color=self.colors["muted"], justify="left")
            # Retain the aggregate label API for accessibility and existing callers.
            # The visible counterpart separates statuses by their actual semantic color.
            statuses = tk.Canvas(panel, width=1, height=40, bg=self.colors["white"], highlightthickness=0, cursor="hand2")
            statuses.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            self.category_status_canvases[name] = statuses
            statuses.bind("<Configure>", lambda _event, value=name: self._paint_category_statuses(value))
            for control in (panel, title, detail, statuses):
                control.bind("<Button-1>", lambda _event, value=name: self._choose_category(value))
            self.category_buttons[name], self.category_details[name] = title, detail
            self.category_panels.append(panel)
        self._category_columns = None
        self._layout_categories(6)

    def _paint_category_statuses(self, name):
        canvas = self.category_status_canvases[name]
        canvas.delete("all")
        values = self.category_status_values.get(name, ())
        width = max(1, canvas.winfo_width())
        if not values:
            canvas.create_text(0, 0, anchor="nw", text=self.category_details[name].cget("text"),
                               font=self.category_status_font, fill=self.colors["muted"], width=width)
        else:
            line_height = self.category_status_font.metrics("linespace") + 2
            x, y, row_height = 0, 0, line_height
            for text, color in values:
                token_width = self.category_status_font.measure(text) + 17
                if x and x + token_width > width:
                    x, y, row_height = 0, y + row_height, line_height
                canvas.create_oval(x, y + 3, x + 8, y + 11, fill=color, outline="")
                text_id = canvas.create_text(x + 13, y, text=text, anchor="nw",
                                             font=self.category_status_font, fill=color,
                                             width=max(1, width - x - 13))
                row_height = max(row_height, canvas.bbox(text_id)[3] - y + 2)
                x += token_width + 5
        # Normal cards remain 84 px. Dense counts grow the canvas/card and page
        # scroll region together, so no real status is hidden below a fixed box.
        bounds = canvas.bbox("all")
        content_height = max(40, bounds[3] + 3 if bounds else 40)
        if int(canvas.cget("height")) != content_height:
            canvas.configure(height=content_height)
        panel_height = max(84, content_height + 44)
        if int(canvas.master.cget("height")) != panel_height:
            canvas.master.configure(height=panel_height)

    def _layout_categories(self, columns):
        if not hasattr(self, "category_panels") or self._category_columns == columns:
            return
        self._category_columns = columns
        for col in range(6):
            self.category_cards.columnconfigure(col, weight=1 if col < columns else 0,
                                                uniform="audit-category" if col < columns else "")
        for index, panel in enumerate(self.category_panels):
            row, col = divmod(index, columns)
            panel.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 4 if col < columns - 1 else 0),
                       pady=(0 if row == 0 else 8, 0))

    def _build_workspace(self):
        body = tk.Frame(self.root, bg=self.colors["bg"], height=555)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_propagate(False)
        body.columnconfigure(0, weight=42, uniform="audit-body")
        body.columnconfigure(1, weight=58, uniform="audit-body")
        body.rowconfigure(0, weight=1)
        self._build_product_panel(body)
        self._build_check_panel(body)

    def _build_product_panel(self, body):
        panel = self.card(body, padx=10, pady=9)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        title = tk.Frame(panel, bg=self.colors["white"])
        title.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.label(title, "商品稽核清单", size=20, bold=True).pack(side="left")
        self.queue_button = self.button(title, text="异常与待办", command=lambda: self._set_queue(True), padding=(8, 6))
        self.queue_button.pack(side="right")
        self.all_products_button = self.button(title, text="全部商品", command=lambda: self._set_queue(False), primary=True, padding=(8, 6))
        self.all_products_button.pack(side="right", padx=(5, 0))
        self.product_list = self.RichTable(panel, (("商品", 43), ("检查汇总", 35), ("状态", 22)), rowheight=64)
        self.product_list.grid(row=1, column=0, sticky="nsew")
        self.product_list.bind("<<TreeviewSelect>>", self._select_product)
        self.product_hint = self.label(panel, "", size=13, color=self.colors["muted"], justify="left", wraplength=250)
        self.product_hint.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        panel.bind("<Configure>", lambda event: self.product_hint.configure(wraplength=max(1, event.width - 24)))

    def _build_check_panel(self, body):
        panel = self.card(body, padx=10, pady=9)
        panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(5, weight=1)
        top = tk.Frame(panel, bg=self.colors["white"])
        top.grid(row=0, column=0, sticky="ew")
        image = self.artwork.phone(48)
        tk.Label(top, image=image, bg=self.colors["white"]).pack(side="left", padx=(0, 9))
        titles = tk.Frame(top, bg=self.colors["white"])
        titles.pack(side="left", fill="x", expand=True)
        self.label(titles, "单品稽核结果", size=20, bold=True).pack(anchor="w")
        self.product_title = self.label(titles, "选择商品查看稽核结果", size=16)
        self.product_title.pack(anchor="w")
        self.all_checks_button = self.button(top, text="全部检查", command=self._toggle_all_checks, padding=(6, 5))
        self.all_checks_button.pack(side="right")
        self.product_summary = self.label(panel, "", size=13, color=self.colors["muted"])
        self.product_summary.grid(row=1, column=0, sticky="w", pady=(3, 6))
        groups = tk.Frame(panel, bg=self.colors['white'])
        groups.grid(row=2, column=0, sticky='ew', pady=(0, 7))
        self.group_buttons = {}
        for key, title in ((None, '全部'), ('base', '基础资料'), ('official', '官网'), ('tmall', '天猫'), ('jd', '京东'), ('quote', '报价与报表')):
            button = self.button(groups, title, lambda k=key: self._choose_group(k), padding=(7, 4))
            button.pack(side='left', padx=(0, 4))
            self.group_buttons[key] = button
        self.check_list = self.RichTable(panel, (("稽核点", 34), ("检查摘要", 48), ("结果", 18)), rowheight=24)
        self.check_list.grid(row=3, column=0, sticky="ew")
        self.check_list.bind("<<TreeviewSelect>>", self._select_check)
        self.detail_heading = self.label(panel, "检查依据与处理", size=15, bold=True, bg="#EDF5FF")
        self.detail_heading.grid(row=4, column=0, sticky="ew", pady=(8, 0), ipady=6)
        self.detail = tk.Frame(panel, bg="#F8FAFE", padx=7, pady=6)
        self.detail.grid(row=5, column=0, sticky="nsew")
        self.detail.columnconfigure(0, weight=1, uniform='detail-evidence')
        self.detail.columnconfigure(1, weight=1, uniform='detail-evidence')
        self.detail.rowconfigure(0, weight=1)
        reader = tk.Frame(self.detail, bg='#F8FAFE')
        reader.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        reader.columnconfigure(0, weight=1)
        reader.rowconfigure(0, weight=1)
        text_canvas = tk.Canvas(reader, bg='#F8FAFE', borderwidth=0, highlightthickness=0,
                                width=1, height=130, yscrollincrement=1)
        text_canvas.grid(row=0, column=0, sticky='nsew')
        text_bar = self.SlimScrollbar(reader, orient='vertical', command=text_canvas.yview)
        text_bar.grid(row=0, column=1, sticky='ns')
        text_canvas.configure(yscrollcommand=text_bar.set)
        text_area = tk.Frame(text_canvas, bg='#F8FAFE')
        text_window = text_canvas.create_window(0, 0, window=text_area, anchor='nw')
        text_canvas.bind('<Configure>', lambda event: text_canvas.itemconfigure(text_window, width=event.width))
        text_area.bind('<Configure>', lambda event: text_canvas.configure(scrollregion=text_canvas.bbox('all')))
        text_area._workbench_scroll_canvas = text_canvas
        text_canvas._workbench_scroll_canvas = text_canvas
        self.comparison = self.label(text_area, "请选择稽核点", size=14, bg="#F8FAFE", wraplength=285, justify="left", width=1)
        self.comparison.pack(anchor="nw", fill="x")
        self.reason = self.label(text_area, "", size=13, color=self.colors["muted"], bg="#F8FAFE", wraplength=285, justify="left", width=1)
        self.reason.pack(anchor="nw", fill="x", pady=(5, 0))
        text_area.bind('<Configure>', self._rewrap_detail, add='+')
        self.evidence_frame = tk.Frame(self.detail, bg="#F8FAFE")
        self.evidence_frame.grid(row=0, column=1, sticky="nsew")
        self.review_conclusion = tk.StringVar(self.root, "通过")
        self.review_reason = tk.StringVar(self.root)
        self.review_operator = tk.StringVar(self.root)
        actions = tk.Frame(panel, bg=self.colors["white"])
        actions.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.correct_button = self.button(actions, text="修正报价", command=self._correct, padding=(8, 5))
        self.correct_button.pack(side="right", padx=(7, 0))
        self.review_button = self.button(actions, text="人工复核", command=self._review, primary=True, padding=(8, 5))
        self.review_button.pack(side="right", padx=(7, 0))
        self.button(actions, text="查看完整依据", command=self._show_detail_dialog, padding=(8, 5)).pack(side="right")

    def _rewrap_detail(self, event):
        width = max(1, event.width - 4)
        self.comparison.configure(wraplength=width)
        self.reason.configure(wraplength=width)

    def _build_footer(self):
        footer = self.card(self.root, padx=12, pady=10, height=56)
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        footer.grid_propagate(False)
        footer.columnconfigure(0, weight=1)
        self.final_state = self.label(footer, "尚未执行稽核", size=14, bold=True)
        self.final_state.grid(row=0, column=0, sticky="w")
        self.export_button = self.button(footer, text="导出草稿", command=self.export_report, padding=(16, 7))
        self.export_button.grid(row=0, column=1, padx=(8, 0))
        self.final_button = self.button(footer, text="确认最终报送版", command=lambda: self.export_report(final=True), primary=True, padding=(16, 7))
        self.final_button.grid(row=0, column=2, padx=(8, 0))

    def _set_queue(self, enabled):
        self.exceptions_only = enabled
        self.refresh()

    def _choose_group(self, group):
        self.group = group
        self.show_all_selected = True
        self._fill_checks()

    def _choose_category(self, category):
        self.group = None
        self.category = None if self.category == category else category
        self.show_all_selected = False
        self.refresh()

    def _toggle_queue(self):
        self.exceptions_only = not self.exceptions_only
        self.refresh()

    def _toggle_all_checks(self):
        self.show_all_selected = not self.show_all_selected
        self.group = None
        self._fill_checks()

    def _select_product(self, _event=None):
        selection = self.product_list.selection()
        if not selection:
            return
        product = next(
            (item for item in self._products_by_list_index if item.id == selection[0]), None
        )
        if product is None:
            return
        self.workbench.review_product_id = product.id
        self.show_all_selected = self.group is not None
        self._fill_checks()

    def _selected_product(self):
        product_id = getattr(self.workbench, "review_product_id", None)
        return next((item for item in self.session.products if item.id == product_id), None)

    def _fill_checks(self):
        product = self._selected_product()
        previous = self.check_list.selection()
        for iid in self.check_list.get_children():
            self.check_list.delete(iid)
        self._checks_by_list_index = []
        if product is None:
            self.product_title.configure(text="选择商品查看稽核结果")
            self.product_summary.configure(text="")
            self._show_check(None)
            return
        specification = getattr(product, "specification", "") or getattr(product, "material_code", "")
        self.product_title.configure(
            text=f"{product.title} · {specification}" if specification else product.title
        )
        checks = self.snapshot.visible_checks(product.id, all_categories=self.show_all_selected)
        if self.group:
            checks = [c for c in checks if (c.code[0] in 'AD' and not getattr(c, 'channel', '') if self.group == 'base' else
                      c.code[0] in 'EF' if self.group == 'quote' else getattr(c, 'channel', '') == self.group)]
        for key, button in self.group_buttons.items():
            button.configure(style="Primary.TButton" if key == self.group else "Workbench.TButton")
        counts = _counts(checks)
        single_scope = "全部检查" if self.show_all_selected or not self.category else self.category
        single_scope = {'base': '基础资料', 'quote': '报价与报表', 'official': '官网', 'tmall': '天猫', 'jd': '京东'}.get(self.group, single_scope)
        pending = sum(counts[s] for s in ("待复核", "待补充", "未检查"))
        if self.snapshot.available:
            summary_text = (
                f"单品范围：{single_scope} · 共 {len(checks)} 项 · "
                f"过 {counts['通过']} / 未过 {counts['未通过']} / 待 {pending}"
            )
        else:
            summary_text = f"单品范围：{single_scope} · 结果不可用，全部商品待重检"
        self.product_summary.configure(text=summary_text)
        for item in checks:
            summary = getattr(item, "title", item.code)
            comparison = getattr(item, "comparison", "")
            tone = (
                "green"
                if item.status == "通过"
                else "red"
                if item.status == "未通过"
                else "orange"
                if item.status in QUEUE_STATUSES
                else "muted"
            )
            self.check_list.insert(
                "",
                "end",
                iid=item.id,
                values=(summary, comparison or "暂无检查摘要", item.status),
                badges={2: tone},
                emphasis=(0,),
            )
            self._checks_by_list_index.append(item)
        if checks:
            selected = next((c for c in checks if previous and c.id == previous[0]), checks[0])
            self.check_list.selection_set(selected.id)
            self._show_check(selected)
        else:
            self._show_check(
                None,
                "稽核结果不可用，请重试"
                if not self.snapshot.available
                else "当前范围没有该商品的检查项",
            )
        self.all_checks_button.configure(text="返回分类" if self.show_all_selected else "全部检查")

    def _select_check(self, _event=None):
        selection = self.check_list.selection()
        if selection:
            item = next(
                (check for check in self._checks_by_list_index if check.id == selection[0]), None
            )
            if item is not None:
                self._show_check(item)

    def _show_detail_dialog(self):
        item = getattr(self, "current_check", None)
        if item is None:
            return
        dialog = tk.Toplevel(self.parent)
        dialog.title("检查依据")
        dialog.transient(self.parent.winfo_toplevel())
        dialog.resizable(True, False)
        shell = tk.Frame(dialog, bg=self.colors["white"], padx=18, pady=16)
        shell.pack(fill="both", expand=True)
        self.label(shell, f"{item.code} · {item.title}", size=15, bold=True).pack(
            anchor="w", pady=(0, 10)
        )
        self.label(
            shell,
            getattr(item, "comparison", "") or "暂无可展示的对照数据",
            size=14,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", fill="x")
        self.label(
            shell,
            "判断原因：" + (getattr(item, "reason", "") or "尚无说明"),
            size=13,
            color=self.colors["muted"],
            wraplength=520,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(7, 10))
        evidence = tuple(getattr(item, "evidence_paths", ()) or ())
        if evidence:
            actions = tk.Frame(shell, bg=self.colors["white"])
            actions.pack(fill="x")
            for index, path in enumerate(evidence[:3]):
                path = Path(path)
                self.button(
                    actions,
                    text=f"查看证据 {index + 1} · {path.name}",
                    command=lambda value=path: self.workbench._open_path(value),
                    padding=(8, 5),
                ).pack(side="left", padx=(0, 5))
        else:
            self.label(shell, "暂无附件证据", size=13, color=self.colors["muted"]).pack(
                anchor="w"
            )
        self.button(shell, text="关闭", command=dialog.destroy, padding=(12, 7)).pack(
            anchor="e", pady=(12, 0)
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _show_check(self, item, empty="请选择稽核点"):
        for child in self.evidence_frame.winfo_children():
            child.destroy()
        self.current_check = item
        if item is None:
            self.detail_heading.configure(text="检查依据与处理")
            self.comparison.configure(text=empty)
            self.reason.configure(text="")
            self.review_button.configure(state="disabled")
            return
        self.detail_heading.configure(text=f"检查依据与处理 · {item.code} {item.title}")
        self.comparison.configure(text=getattr(item, "comparison", "") or "暂无可展示的对照数据")
        self.reason.configure(text="判断原因：" + (getattr(item, "reason", "") or "尚无说明"))
        evidence = tuple(getattr(item, "evidence_paths", ()) or ())
        self.label(self.evidence_frame, "证据预览", size=13, bold=True, bg="#F8FAFE").pack(anchor="w")
        self._evidence_image = None
        preview_path = None
        from PIL import Image, ImageTk
        for path in evidence:
            try:
                candidate = Path(path)
                if not candidate.is_file():
                    continue
                with Image.open(candidate) as source:
                    source = source.convert("RGB")
                    source.thumbnail((270, 92), Image.Resampling.LANCZOS)
                    self._evidence_image = ImageTk.PhotoImage(source, master=self.root)
                preview_path = candidate
                break
            except (OSError, ValueError, tk.TclError):
                continue
        if preview_path:
            preview = tk.Label(self.evidence_frame, image=self._evidence_image, bg="#F8FAFE", cursor="hand2")
            preview.pack(anchor="w", pady=(3, 0))
            preview.bind("<Button-1>", lambda _event, path=preview_path: self.workbench._open_path(path))
            self.label(self.evidence_frame, "点击查看原始截图 ↗", size=12, color=self.colors["blue"], bg="#F8FAFE").pack(anchor="w")
        else:
            self.label(self.evidence_frame, "工作簿依据（点击查看）" if evidence and all(Path(a).suffix.lower() == ".xlsx" for a in evidence) else "暂无可预览的本地截图" if evidence else "本项依据见左侧自动检查说明", size=13,
                       color=self.colors["muted"], bg="#F8FAFE").pack(anchor="w", pady=(14, 0))
            if evidence:
                self.button(self.evidence_frame, text="查看附件", command=lambda: self.workbench._open_path(Path(evidence[0])), padding=(8, 5)).pack(anchor="w", pady=5)
        reviewable = bool(getattr(item, "human_reviewable", False))
        self.review_button.configure(state="normal" if reviewable else "disabled")

    def _review(self):
        item = getattr(self, "current_check", None)
        if item is None or not getattr(item, "human_reviewable", False):
            messagebox.showwarning("不能人工复核", "该检查为确定性规则，不能人工覆盖。", parent=self.parent.winfo_toplevel())
            return
        self.review_conclusion.set("通过")
        self.review_reason.set("")
        self.review_operator.set("")
        dialog = tk.Toplevel(self.parent)
        dialog.title("人工复核")
        dialog.transient(self.parent.winfo_toplevel())
        dialog.resizable(False, False)
        shell = tk.Frame(dialog, bg=self.colors["white"], padx=18, pady=16)
        shell.pack(fill="both", expand=True)
        self.label(shell, f"{item.code} · {item.title}", size=14, bold=True).grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        review_select = self.SoftSelect(
            shell,
            textvariable=self.review_conclusion,
            values=("通过", "未通过"),
            width=300,
        )
        review_select.grid(row=1, column=0, sticky="ew")
        reason_entry = self.SoftEntry(
            shell, textvariable=self.review_reason, placeholder="复核理由（必填）", width=300
        )
        reason_entry.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        operator_entry = self.SoftEntry(
            shell, textvariable=self.review_operator, placeholder="操作人（必填）", width=300
        )
        operator_entry.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        actions = tk.Frame(shell, bg=self.colors["white"])
        actions.grid(row=4, column=0, sticky="e", pady=(12, 0))
        self.button(actions, text="取消", command=dialog.destroy, padding=(12, 7)).pack(
            side="left", padx=(0, 7)
        )
        self.button(
            actions,
            text="提交复核",
            command=lambda: self._submit_review(dialog),
            primary=True,
            padding=(12, 7),
        ).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        reason_entry.entry.focus_set()

    def _submit_review(self, dialog):
        item = getattr(self, "current_check", None)
        reason, operator = self.review_reason.get().strip(), self.review_operator.get().strip()
        if not reason or not operator:
            messagebox.showwarning("信息不完整", "复核理由和操作人均为必填。", parent=dialog)
            return
        try:
            self.session.review(item.id, self.review_conclusion.get(), reason, operator)
        except (ValueError, OSError) as error:
            messagebox.showerror("复核失败", str(error), parent=dialog)
            return
        dialog.destroy()
        self.refresh()

    def _correct(self):
        product = self._selected_product()
        if product is not None:
            self.workbench.review_product_id = product.id
        self.workbench.show_page("decision")

    def export_report(self, final=False):
        try:
            path = self.session.export_report(final=final)
        except (ValueError, OSError) as error:
            messagebox.showerror("无法导出稽核报告", str(error), parent=self.parent.winfo_toplevel())
            return None
        messagebox.showinfo("稽核报告已导出", str(path), parent=self.parent.winfo_toplevel())
        return path

    @staticmethod
    def _product_count_text(counts):
        lines = [f"{counts['通过']}通过 · {counts['未通过']}未通过"]
        pending = [f"{counts[status]}{status}" for status in ("待复核", "待补充", "未检查") if counts[status]]
        if pending:
            lines.append(" · ".join(pending))
        return "\n".join(lines)

    def refresh(self):
        read_error = None
        try:
            checks = list(self.session.all_checks())
        except (ValueError, OSError) as error:
            checks = []
            read_error = error
        products = list(getattr(self.session, "products", ()) or ())
        self.snapshot = build_audit_snapshot(
            products,
            checks,
            category=self.category,
            exceptions_only=self.exceptions_only,
            running=bool(getattr(self.session, "running", False)),
            available=read_error is None,
        )
        for key, widget in self.metric_values.items():
            widget.configure(
                text=(
                    "—"
                    if read_error is not None and key in {"checks", "通过", "需处理"}
                    else str(self.snapshot.summary[key])
                )
            )
        self.summary_extra.configure(
            text=(
                "结果不可用 · 全部商品待重检"
                if read_error is not None
                else f"未检查 {self.snapshot.summary['未检查']} · 不适用 {self.snapshot.summary['不适用']}"
            )
        )
        self.scope_label.configure(
            text="当前范围："
            + (self.category or "全部检查")
            + (" · 结果不可用" if read_error is not None else "")
        )
        for name, widget in self.category_details.items():
            counts = self.snapshot.category_counts[name]
            pieces = [f"{counts[s]}{s}" for s in ("通过", "未通过", "待复核", "待补充", "未检查") if counts[s]]
            widget.configure(
                text="结果不可用" if read_error is not None else "\n".join(" · ".join(pieces[i:i + 2]) for i in range(0, len(pieces), 2)) or "尚无检查"
            )
            self.category_status_values[name] = (
                () if read_error is not None else tuple(
                    (f"{counts[status]}{status}", STATUS_TONES[status][1])
                    for status in ("通过", "未通过", "待复核", "待补充", "未检查") if counts[status]
                )
            )
            self._paint_category_statuses(name)
            self.category_buttons[name].configure(
                bg=self.colors["blue"] if name == self.category else self.colors["white"],
                fg=self.colors["white"] if name == self.category else self.colors["blue"],
            )
        self.queue_button.configure(style="Primary.TButton" if self.exceptions_only else "Workbench.TButton")
        self.all_products_button.configure(style="Workbench.TButton" if self.exceptions_only else "Primary.TButton")
        self.product_hint.configure(
            text=f"共 {len(self.snapshot.rows)} 款商品  ·  选择商品，查看该商品的全部稽核点"
        )
        for iid in self.product_list.get_children():
            self.product_list.delete(iid)
        self._products_by_list_index = []
        for row in self.snapshot.rows:
            p = row.product
            issue = sum(row.counts[s] for s in QUEUE_STATUSES)
            tone = (
                "red"
                if row.counts["未通过"]
                else "green"
                if row.overall == "全部通过"
                else "orange"
                if issue
                else "muted"
            )
            pending = sum(row.counts[s] for s in ("待复核", "待补充", "未检查"))
            summary = (
                "待重检"
                if not self.snapshot.available
                else f"{row.counts['通过']}过  {row.counts['未通过']}未  {pending}待"
            )
            self.product_list.insert(
                "",
                "end",
                iid=p.id,
                values=(p.title, summary, row.overall),
                display_values=(p.title, self._product_count_text(row.counts) if self.snapshot.available else "待重检", row.overall),
                subtitle=getattr(p, "specification", "") or getattr(p, "material_code", ""),
                image=self.artwork.phone(36),
                badges={2: tone},
                emphasis=(1,),
            )
            self._products_by_list_index.append(p)
        ids = {row.product.id for row in self.snapshot.rows}
        selected = getattr(self.workbench, "review_product_id", None)
        if selected not in ids:
            selected = self.snapshot.rows[0].product.id if self.snapshot.rows else None
            self.workbench.review_product_id = selected
        if selected is not None:
            self.product_list.selection_set(selected)
        if read_error is not None:
            self.final_state.configure(
                text=f"稽核读取失败：{read_error} · 当前结果按未检查处理，暂不可确认报送",
                fg="#E1251B",
            )
        elif self.snapshot.batch_state == "running":
            self.final_state.configure(text="稽核执行中 · 未完成项目仍计入检查范围", fg="#D88119")
        elif self.snapshot.batch_state == "empty":
            self.final_state.configure(text="完成取价与报价生成后，可在此查看本批次稽核结果", fg=self.colors["muted"])
        elif self.snapshot.summary["需处理"] or self.snapshot.summary["未检查"]:
            self.final_state.configure(text=f"{self.snapshot.summary['需处理']} 项需处理 · {self.snapshot.summary['未检查']} 项未检查，暂不可确认报送", fg="#E1251B")
        else:
            self.final_state.configure(text="当前批次无未解决事项，可由服务执行最终版本校验", fg="#0B9975")
        can_final = (
            read_error is None
            and self.snapshot.batch_state == "ready"
            and not self.snapshot.summary["需处理"]
            and not self.snapshot.summary["未检查"]
            and bool(checks)
        )
        self.final_button.configure(state="normal" if can_final else "disabled")
        self._fill_checks()

    def destroy(self):
        if getattr(self, '_scan_timer', None):
            self.container.after_cancel(self._scan_timer)
        if self.container.winfo_exists():
            self.container.destroy()

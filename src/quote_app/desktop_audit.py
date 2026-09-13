"""Native audit workbench backed only by the local review session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from typing import Iterable, Sequence


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


class AuditView:
    """Full-batch summary, one-row-per-product queue, and selectable check detail."""

    def __init__(self, workbench, parent, session):
        from quote_app.desktop_controls import SoftEntry, SoftSelect
        from quote_app.desktop_ui import BG, BLUE, FONT, INK, LINE, MUTED, WHITE, button, card, label
        from quote_app.desktop_widgets import Artwork, RichTable, SlimScrollbar

        self.workbench, self.parent, self.session = workbench, parent, session
        self.colors = dict(bg=BG, blue=BLUE, ink=INK, line=LINE, muted=MUTED, white=WHITE)
        self.font, self.label, self.button, self.card = FONT, label, button, card
        self.Artwork, self.RichTable, self.SlimScrollbar = Artwork, RichTable, SlimScrollbar
        self.SoftEntry, self.SoftSelect = SoftEntry, SoftSelect
        self.artwork = Artwork(parent.winfo_toplevel())
        self.category: str | None = None
        self.exceptions_only = False
        self.show_all_selected = False
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
            lambda event: self.page_canvas.itemconfigure(self.page_window, width=event.width),
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

    def _build_summary(self):
        row = tk.Frame(self.root, bg=self.colors["bg"])
        row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for index in range(4):
            row.columnconfigure(index, weight=1, uniform="audit-metric")
        self.metric_values = {}
        specs = (
            ("products", "本批次商品", "package", self.colors["ink"]),
            ("checks", "检查结果", "list-checks", self.colors["ink"]),
            ("通过", "通过", "circle-check", "#0B9975"),
            ("需处理", "需处理", "clock-3", "#D88119"),
        )
        for index, (key, title, icon, color) in enumerate(specs):
            panel = self.card(row, padx=14, pady=10)
            panel.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 5 if index < 3 else 0))
            image = self.artwork.get("ui-icons/" + icon + "-blue", 24)
            if image:
                tk.Label(panel, image=image, bg=self.colors["white"]).grid(row=0, column=0, rowspan=2, padx=(0, 10))
            self.label(panel, title, size=10, color=self.colors["muted"]).grid(row=0, column=1, sticky="w")
            value = self.label(panel, "0", size=20, bold=True, color=color)
            value.grid(row=1, column=1, sticky="w")
            self.metric_values[key] = value
        self.summary_extra = self.label(row, "未检查 0 · 不适用 0", size=9, color=self.colors["muted"], bg=self.colors["bg"])
        self.summary_extra.grid(row=1, column=0, columnspan=4, sticky="e", pady=(3, 0))

    def _build_categories(self):
        area = tk.Frame(self.root, bg=self.colors["bg"])
        area.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        heading = tk.Frame(area, bg=self.colors["bg"])
        heading.pack(fill="x", pady=(0, 5))
        self.label(heading, "全批次分类统计", size=13, bold=True, bg=self.colors["bg"]).pack(side="left")
        self.scope_label = self.label(heading, "当前范围：全部检查", size=10, color=self.colors["blue"], bg=self.colors["bg"])
        self.scope_label.pack(side="right")
        cards = tk.Frame(area, bg=self.colors["bg"])
        cards.pack(fill="x")
        self.category_buttons = {}
        self.category_details = {}
        for column in range(3):
            cards.columnconfigure(column, weight=1, uniform="audit-category")
        for index, name in enumerate(CATEGORY_NAMES):
            row, column = divmod(index, 3)
            panel = self.card(cards, padx=9, pady=5)
            panel.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 3, 3 if column < 2 else 0),
                pady=(0 if row == 0 else 3, 3 if row == 0 else 0),
            )
            title = tk.Label(
                panel,
                text=name,
                font=(self.font, 10, "bold"),
                fg=self.colors["blue"],
                bg=self.colors["white"],
                cursor="hand2",
                anchor="w",
                padx=4,
                pady=2,
            )
            title.bind("<Button-1>", lambda _event, value=name: self._choose_category(value))
            title.grid(row=0, column=0, sticky="ew")
            detail = self.label(panel, "尚无检查", size=9, color=self.colors["muted"])
            detail.grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.category_buttons[name], self.category_details[name] = title, detail

    def _build_workspace(self):
        body = tk.Frame(self.root, bg=self.colors["bg"])
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=4, uniform="audit-body")
        body.columnconfigure(1, weight=7, uniform="audit-body")
        body.rowconfigure(0, weight=1)
        self._build_product_panel(body)
        self._build_check_panel(body)

    def _list_with_scrollbar(self, parent, **kwargs):
        frame = tk.Frame(parent, bg=self.colors["white"])
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        widget = tk.Listbox(
            frame,
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
            selectborderwidth=0,
            font=(self.font, 11),
            bg=self.colors["white"],
            fg=self.colors["ink"],
            selectbackground="#EAF1FF",
            selectforeground=self.colors["ink"],
            exportselection=False,
            height=kwargs.pop("height", 2),
            **kwargs,
        )
        bar = self.SlimScrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=bar.set)
        widget.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        return frame, widget

    def _build_product_panel(self, body):
        panel = self.card(body, padx=13, pady=12)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        title = tk.Frame(panel, bg=self.colors["white"])
        title.grid(row=0, column=0, sticky="ew")
        self.label(title, "商品稽核清单", size=16, bold=True).pack(side="left")
        self.queue_button = self.button(title, text="异常与待办", command=self._toggle_queue, padding=(9, 6))
        self.queue_button.pack(side="right")
        self.product_hint = self.label(panel, "一条商品规格记录一行", size=9, color=self.colors["muted"])
        self.product_hint.grid(row=1, column=0, sticky="w", pady=(5, 7))
        self.product_list = self.RichTable(
            panel, (("商品", 178), ("检查汇总", 112), ("状态", 82)), rowheight=66
        )
        self.product_list.grid(row=2, column=0, sticky="nsew")
        self.product_list.bind("<<TreeviewSelect>>", self._select_product)

    def _build_check_panel(self, body):
        panel = self.card(body, padx=13, pady=12)
        panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=2)
        panel.rowconfigure(5, weight=3)
        top = tk.Frame(panel, bg=self.colors["white"])
        top.grid(row=0, column=0, sticky="ew")
        self.product_title = self.label(top, "选择商品查看稽核结果", size=16, bold=True)
        self.product_title.pack(fill="x")
        self.all_checks_button = self.button(top, text="查看该商品全部检查", command=self._toggle_all_checks, padding=(9, 6))
        self.all_checks_button.pack(anchor="e", pady=(3, 0))
        self.product_summary = self.label(panel, "", size=10, color=self.colors["muted"])
        self.product_summary.grid(row=1, column=0, sticky="w", pady=(3, 7))
        self.check_list = self.RichTable(
            panel, (("稽核点", 260), ("编号", 70), ("结果", 80)), rowheight=42
        )
        self.check_list.grid(row=3, column=0, sticky="nsew")
        self.check_list.bind("<<TreeviewSelect>>", self._select_check)
        self.detail_heading = self.label(panel, "检查依据与处理", size=13, bold=True)
        self.detail_heading.grid(row=4, column=0, sticky="w", pady=(10, 5))
        self.detail = tk.Frame(panel, bg="#F8FAFE", padx=9, pady=7)
        self.detail.grid(row=5, column=0, sticky="nsew")
        self.detail.columnconfigure(0, weight=1)
        self.comparison = self.label(self.detail, "请选择稽核点", size=11, bg="#F8FAFE", wraplength=560, justify="left")
        self.comparison.grid(row=0, column=0, columnspan=3, sticky="w")
        self.reason = self.label(self.detail, "", size=10, color=self.colors["muted"], bg="#F8FAFE", wraplength=560, justify="left")
        self.reason.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 5))
        self.evidence_frame = tk.Frame(self.detail, bg="#F8FAFE")
        self.evidence_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        self.review_conclusion = tk.StringVar(self.root, "通过")
        self.review_reason = tk.StringVar(self.root)
        self.review_operator = tk.StringVar(self.root)
        self.review_button = self.button(
            self.detail, text="人工复核", command=self._review, primary=True, padding=(8, 5)
        )
        self.review_button.grid(row=3, column=2, sticky="e")
        self.correct_button = self.button(
            self.detail, text="修正报价", command=self._correct, padding=(8, 5)
        )
        self.correct_button.grid(row=3, column=0, sticky="w")
        self.detail.bind("<Configure>", self._rewrap_detail)

    def _rewrap_detail(self, event):
        width = max(180, event.width - 24)
        self.comparison.configure(wraplength=width)
        self.reason.configure(wraplength=width)

    def _build_footer(self):
        footer = self.card(self.root, padx=12, pady=9)
        footer.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        footer.columnconfigure(0, weight=1)
        self.final_state = self.label(footer, "尚未执行稽核", size=11, bold=True)
        self.final_state.grid(row=0, column=0, sticky="w")
        self.export_button = self.button(footer, text="导出草稿", command=self.export_report, padding=(12, 7))
        self.export_button.grid(row=0, column=1, padx=(8, 0))
        self.final_button = self.button(footer, text="确认最终报送版", command=lambda: self.export_report(final=True), primary=True, padding=(12, 7))
        self.final_button.grid(row=0, column=2, padx=(8, 0))

    def _choose_category(self, category):
        self.category = None if self.category == category else category
        self.show_all_selected = False
        self.refresh()

    def _toggle_queue(self):
        self.exceptions_only = not self.exceptions_only
        self.refresh()

    def _toggle_all_checks(self):
        self.show_all_selected = not self.show_all_selected
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
        self.show_all_selected = False
        self._fill_checks()

    def _selected_product(self):
        product_id = getattr(self.workbench, "review_product_id", None)
        return next((item for item in self.session.products if item.id == product_id), None)

    def _fill_checks(self):
        product = self._selected_product()
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
        counts = _counts(checks)
        single_scope = "全部检查" if self.show_all_selected or not self.category else self.category
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
            if comparison:
                summary = f"{summary} · {comparison}"
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
                values=(summary, item.code, item.status),
                badges={2: tone},
                emphasis=(0,),
            )
            self._checks_by_list_index.append(item)
        if checks:
            self.check_list.selection_set(checks[0].id)
            self._show_check(checks[0])
        else:
            self._show_check(
                None,
                "稽核结果不可用，请重试"
                if not self.snapshot.available
                else "当前范围没有该商品的检查项",
            )
        self.all_checks_button.configure(text="返回当前分类" if self.show_all_selected else "查看该商品全部检查")

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
            size=11,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", fill="x")
        self.label(
            shell,
            "判断原因：" + (getattr(item, "reason", "") or "尚无说明"),
            size=10,
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
            self.label(shell, "暂无附件证据", size=9, color=self.colors["muted"]).pack(
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
            self.comparison.configure(text=empty)
            self.reason.configure(text="")
            self.review_button.configure(state="disabled")
            return
        self.detail_heading.configure(text=f"检查依据与处理 · {item.code} {item.title}")
        self.comparison.configure(text=getattr(item, "comparison", "") or "暂无可展示的对照数据")
        self.reason.configure(text="判断原因：" + (getattr(item, "reason", "") or "尚无说明"))
        evidence = tuple(getattr(item, "evidence_paths", ()) or ())
        if evidence:
            for index, path in enumerate(evidence[:3]):
                path = Path(path)
                control = self.button(self.evidence_frame, text=f"查看证据 {index + 1} · {path.name}", command=lambda value=path: self.workbench._open_path(value), padding=(8, 5))
                control.pack(side="left", padx=(0, 5))
        else:
            self.label(self.evidence_frame, "暂无附件证据", size=9, color=self.colors["muted"], bg="#F8FAFE").pack(side="left")
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
                text="结果不可用" if read_error is not None else " · ".join(pieces) or "尚无检查"
            )
            self.category_buttons[name].configure(
                bg=self.colors["blue"] if name == self.category else self.colors["white"],
                fg=self.colors["white"] if name == self.category else self.colors["blue"],
            )
        self.queue_button.configure(text="全部商品" if self.exceptions_only else "异常与待办")
        self.product_hint.configure(
            text=f"当前范围：{self.category or '全部检查'} · "
            f"{'异常与待办' if self.exceptions_only else '全部商品'}\n"
            "过/未/待：通过/未通过/待处理"
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
                subtitle=getattr(p, "specification", "") or getattr(p, "material_code", ""),
                image=self.artwork.phone(42),
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
        if self.container.winfo_exists():
            self.container.destroy()

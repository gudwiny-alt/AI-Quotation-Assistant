"""Native, local post-generation quotation form. No collection callbacks live here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from quote_app.desktop_controls import SoftEntry, SoftSelect
from quote_app.desktop_widgets import SlimScrollbar


def money_preview(value: str) -> str:
    """Keep invalid in-progress input visible instead of silently coercing it to zero."""
    if not value.strip():
        return "—"
    raw = value.replace(",", "").replace("¥", "").strip()
    if len(raw) > 1000:
        return value
    try:
        number = Decimal(raw)
        if not number.is_finite() or not -300 <= number.adjusted() <= 300:
            return value
        formatted = f"{number:,f}"
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return "¥" + formatted
    except InvalidOperation:
        return value


@dataclass(frozen=True, slots=True)
class ChannelPresentation:
    price: str
    status: str


def channel_presentation(records) -> ChannelPresentation:
    """Describe collection progress without treating an observed price as audited."""
    if not records:
        return ChannelPresentation("—", "待取价")

    record = records[-1]
    price = money_preview(record.price)
    if record.state == "technical_failure":
        status = "采集失败"
    elif record.state == "waiting_for_login":
        status = "等待验证"
    elif record.state == "running":
        status = "采集中"
    elif record.state in {"failed", "stopped"}:
        status = "采集失败"
    elif record.state == "paused":
        status = "已暂停"
    elif record.state == "succeeded" and record.outcome == "price_found":
        status = "待复核"
    elif record.outcome:
        status = record.outcome_label
    else:
        status = "待取价"
    return ChannelPresentation(price, status)


class DecisionView:
    def __init__(self, workbench, parent, session):
        from quote_app.desktop_ui import BG, BLUE, FONT, GREEN, INK, LINE, MUTED, ORANGE, WHITE
        from quote_app.desktop_ui import button, card, label

        self.wb, self.parent, self.session = workbench, parent, session
        self.BG, self.BLUE, self.FONT, self.GREEN = BG, BLUE, FONT, GREEN
        self.INK, self.LINE, self.MUTED, self.ORANGE, self.WHITE = INK, LINE, MUTED, ORANGE, WHITE
        # These reference drawings use logical pixels; Tk's positive font sizes
        # are points and inflate both text and the measured layout on macOS.
        def pixel_label(parent, text="", *, size=14, **kwargs):
            return label(parent, text, size=-abs(size), **kwargs)

        def pixel_button(parent, text, command, **kwargs):
            widget = button(parent, text, command, **kwargs)
            widget._font.configure(size=-14)
            widget._measure()
            return widget

        self.button, self.card, self.label = pixel_button, card, pixel_label
        self.product = None
        self.vars = {}
        self.traces = []
        self.dead = False
        self.loading = False
        self.dialog = None
        self.content = None
        self.search_var = tk.StringVar(parent)
        self.show_list = not bool(getattr(workbench, "review_product_id", None))
        self._build()

    def destroy(self):
        self.dead = True
        self._clear_traces()
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()

    def _clear_traces(self):
        for variable, trace in self.traces:
            variable.trace_remove("write", trace)
        self.traces.clear()

    def _reset(self):
        self._clear_traces()
        for child in self.parent.winfo_children():
            child.destroy()
        self.content = self.wb._scrollable(self.parent)

    def _wrap(self, parent, text="", **kwargs):
        kwargs.setdefault("width", 1)
        widget = self.label(parent, text, justify="left", **kwargs)
        widget.bind("<Configure>", lambda e: widget.configure(wraplength=max(80, e.width - 4)))
        return widget

    def _build(self):
        self._reset()
        products = self.session.products
        if not products:
            self._empty()
            return
        self.product = next(
            (p for p in products if p.id == getattr(self.wb, "review_product_id", None)),
            products[0],
        )
        if self.show_list:
            self._products()
        else:
            self.wb.review_product_id = self.product.id
            self._form()

    def _empty(self):
        panel = self.card(self.content, padx=28, pady=38)
        panel.grid(row=0, column=0, sticky="ew", pady=12)
        panel.columnconfigure(0, weight=1)
        self.wb._icon_label(panel, "file-text", size=48).grid(row=0, column=0, pady=(0, 18))
        self.label(
            panel,
            "等待本次报价表生成" if self.session.running else "开始补充报价决策信息",
            size=18,
            bold=True,
        ).grid(row=1, column=0)
        self._wrap(
            panel,
            "完成自动取价并生成报价表后，可选择商品，补齐手工价格并查看智能报价提示。也可以打开本机已生成的报价表继续填写。",
            color=self.MUTED,
        ).grid(row=2, column=0, sticky="ew", pady=16)
        self.button(
            panel,
            "打开已有报价表",
            self.wb.open_review_workbook,
            primary=True,
            state="disabled" if self.session.running else "normal",
        ).grid(row=3, column=0, pady=8)
        self.button(panel, "前往数据准备", lambda: self.wb.show_overview_tab("data")).grid(
            row=4, column=0, pady=8
        )

    def _products(self):
        top = self.card(self.content, padx=18, pady=16)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.label(top, "本批次商品 · 选择后填写与试算", size=16, bold=True).grid(
            row=0, column=0, sticky="w"
        )
        self.button(top, "打开已有报价表", self.wb.open_review_workbook).grid(
            row=0, column=1, padx=(12, 0)
        )
        self._wrap(
            top,
            "填写结果写回对应商品的 K / L / M / P / Q / AO 列；其他自动生成内容保持原样。",
            color=self.MUTED,
            size=11,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 14))
        SoftEntry(
            top, textvariable=self.search_var, placeholder="搜索机型、规格或物料编码", width=260
        ).grid(row=2, column=0, columnspan=2, sticky="ew")
        self.list_body = tk.Frame(self.content, bg=self.BG)
        self.list_body.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.list_body.columnconfigure(0, weight=1)
        self.traces.append(
            (self.search_var, self.search_var.trace_add("write", lambda *_: self._product_rows()))
        )
        self._product_rows()

    def _product_rows(self):
        for child in self.list_body.winfo_children():
            child.destroy()
        query = self.search_var.get().strip().casefold()
        filtered = [
            p
            for p in self.session.products
            if query in f"{p.title} {p.specification} {p.material_code}".casefold()
        ]
        for index, product in enumerate(filtered):
            panel = self.card(self.list_body, padx=16, pady=14)
            panel.grid(row=index, column=0, sticky="ew", pady=(0, 8))
            panel.columnconfigure(1, weight=1)
            photo = self.wb.artwork.phone(48)
            self.label(panel, image=photo or "").grid(row=0, column=0, rowspan=2, padx=(0, 12))
            self._wrap(panel, product.title, size=14, bold=True).grid(row=0, column=1, sticky="ew")
            self._wrap(
                panel,
                f"{product.specification}  ·  {product.material_code}",
                color=self.MUTED,
                size=10,
            ).grid(row=1, column=1, sticky="ew", pady=(5, 0))
            self.label(
                panel,
                money_preview(product.values.get("K", "")),
                color=self.BLUE,
                bold=True,
                size=15,
            ).grid(row=0, column=2, padx=16)
            self.label(panel, "当月报价", color=self.MUTED, size=10).grid(row=1, column=2)
            self.button(
                panel, "填写与试算  →", lambda p=product: self.select(p.id), primary=True
            ).grid(row=0, column=3, rowspan=2)
        if not filtered:
            self.label(self.list_body, "没有匹配的商品", bg=self.BG, color=self.MUTED).grid(pady=24)

    def select(self, product_id):
        self.wb.review_product_id = product_id
        self.show_list = False
        self._build()

    def back_to_products(self):
        self.show_list = True
        self._build()

    def _move(self, delta):
        products = self.session.products
        index = products.index(self.product) + delta
        if 0 <= index < len(products):
            self.select(products[index].id)

    def _price_entry(self, parent, key, *, emphasis=False):
        entry = SoftEntry(parent, textvariable=self.vars[key], placeholder="填写价格", width=120)
        entry.entry.configure(font=(self.FONT, -26 if emphasis else -23, "bold"), fg=self.INK)
        entry.hint.configure(font=(self.FONT, -14), text="")
        entry.entry.place_configure(x=40, width=-53, y=7, height=28)
        self.label(entry, "¥", size=24 if emphasis else 21, bold=True, padx=0, pady=0,
                   borderwidth=0).place(x=13, y=5, width=26, height=32)
        self.entries.append(entry.entry)
        return entry

    def _form(self):
        self.vars = {
            key: tk.StringVar(self.parent, value=self.product.values.get(key, ""))
            for key in ("K", "L", "M", "P", "Q", "AO")
        }
        self.entries = []
        stage = self.card(self.content, padx=18, pady=9)
        stage.grid(row=0, column=0, sticky="ew")
        stage.columnconfigure(0, weight=1)
        steps = tk.Canvas(stage, height=30, bg=self.WHITE, highlightthickness=0)
        steps.grid(row=0, column=0, sticky="ew")
        check_icon = self.wb.artwork.get("ui-icons/circle-check-white", 25)
        def draw_steps(event):
            steps.delete("all")
            for index, title in enumerate(("自动取价已完成", "补充价格与试算", "写回报价表")):
                x = event.width * (index + .32) / 3
                active = index < 2
                if index < 2:
                    steps.create_line(x + 153, 15, event.width * (index + 1.32) / 3 - 26,
                                      15, fill=self.BLUE if index == 0 else "#B9CAE3")
                steps.create_oval(x - 15, 0, x + 15, 30, fill=self.BLUE if active else "#E7EDF6", outline="")
                if index == 0 and check_icon:
                    steps.create_image(x, 15, image=check_icon)
                else:
                    steps.create_text(x, 15, text=str(index + 1), fill=self.WHITE if active else self.MUTED,
                                      font=(self.FONT, -18, "bold"))
                steps.create_text(x + 27, 15, text=title, anchor="w", fill=self.BLUE if active else self.MUTED,
                                  font=(self.FONT, -17, "bold" if index == 1 else "normal"))
        steps.bind("<Configure>", draw_steps)

        strip = self.card(self.content, padx=14, pady=8)
        strip.grid(row=1, column=0, sticky="ew", pady=10)
        strip.columnconfigure(1, weight=1)
        self.label(strip, image=self.wb.artwork.phone(64) or "").grid(
            row=0, column=0, rowspan=2, padx=(0, 14))
        product_heading = tk.Frame(strip, bg=self.WHITE)
        product_heading.grid(row=0, column=1, sticky="ew")
        product_heading.columnconfigure(0, weight=1)
        self._wrap(product_heading, self.product.title + "  ·  " + self.product.specification,
                   size=19, bold=True).grid(row=0, column=0, sticky="ew")
        nav = tk.Frame(strip, bg=self.WHITE)
        nav.grid(row=0, column=2, rowspan=2, padx=(12, 0))
        i = self.session.products.index(self.product)
        self.button(nav, "‹", lambda: self._move(-1), state="normal" if i else "disabled",
                    padding=(12, 9)).pack(side="left")
        self.label(nav, f"当前商品 {i + 1} / {len(self.session.products)}", size=14,
                   color=self.MUTED).pack(side="left", padx=10)
        self.button(nav, "›", lambda: self._move(1),
                    state="normal" if i + 1 < len(self.session.products) else "disabled",
                    padding=(12, 9)).pack(side="left")
        channels = tk.Frame(strip, bg=self.WHITE)
        channels.grid(row=1, column=1, sticky="ew", pady=(3, 0))
        channel_widgets = []
        for ci, (channel, title) in enumerate((("jd", "京东"), ("tmall", "天猫"), ("official", "官网"))):
            channels.columnconfigure(ci, weight=1, uniform="decision-channel")
            group = tk.Frame(channels, bg=self.WHITE)
            group.grid(row=0, column=ci, sticky="ew", padx=(0, 8))
            group.columnconfigure(1, weight=1)
            photo = self.wb.artwork.channel(channel, self.product.title, 24)
            self.label(group, image=photo or "").grid(row=0, column=0, padx=(0, 5))
            presentation = channel_presentation([r for r in self.product.channels if r.channel == channel])
            price_label = self._wrap(group, f"{title}  {presentation.price}", size=16, bold=True)
            price_label.grid(row=0, column=1, sticky="ew")
            status_label = self.label(group, presentation.status, size=11, color=self.MUTED)
            status_label.grid(row=0, column=2, sticky="e", padx=(4, 0))
            channel_widgets.append((price_label, status_label))
        evidence_button = self.button(channels, "查看取价证据 ›", self.open_evidence, padding=(6, 3))
        evidence_button.grid(row=0, column=3, sticky="e")
        strip_mode = [None]
        def resize_strip(event):
            compact = event.width < 1000
            if strip_mode[0] != compact:
                strip_mode[0] = compact
                channels.grid_configure(columnspan=2 if compact else 1)
                nav.grid_configure(rowspan=1 if compact else 2)
                evidence_button.configure(text="取价证据 ›" if compact else "查看取价证据 ›")
                for price_label, status_label in channel_widgets:
                    price_label.grid_configure(columnspan=2 if compact else 1)
                    price_label.configure(font=(self.FONT, -14 if compact else -16, "bold"))
                    status_label.grid_configure(row=1 if compact else 0, column=1 if compact else 2,
                                                columnspan=2 if compact else 1, sticky="w" if compact else "e")
        strip.bind("<Configure>", resize_strip, add="+")

        columns = tk.Frame(self.content, bg=self.BG)
        columns.grid(row=2, column=0, sticky="ew")
        columns.columnconfigure(0, weight=1, uniform="decision-column")
        columns.columnconfigure(1, weight=1, uniform="decision-column")
        left = self.card(columns, padx=16, pady=10)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left.columnconfigure(0, weight=1)
        self.label(left, "产品经理填写", size=21, bold=True).grid(row=0, column=0, sticky="w")
        self.label(left, "单位：元 / 台", color=self.MUTED, size=14).grid(row=0, column=0, sticky="e")
        fields = tk.Frame(left, bg=self.WHITE)
        fields.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        for col in range(2):
            fields.columnconfigure(col, weight=1, uniform="manual")
        for index, (key, title) in enumerate((("L", "终端公司采购价（L）"), ("M", "全国采购系统均价（M）"),
                                              ("P", "渠道买断价（P）"), ("Q", "分销零售价（Q）"))):
            box = tk.Frame(fields, bg=self.WHITE)
            box.grid(row=index // 2, column=index % 2, sticky="ew",
                     padx=(0, 10) if index % 2 == 0 else (10, 0), pady=(0, 10))
            box.columnconfigure(0, weight=1)
            self._wrap(box, title, size=15).grid(row=0, column=0, sticky="ew", pady=(0, 5))
            self._price_entry(box, key).grid(row=1, column=0, sticky="ew")
        self.kbox = tk.Frame(left, bg="#FFF4EC", padx=8, pady=6)
        self.kbox.grid(row=2, column=0, sticky="ew")
        self.kbox.columnconfigure(0, weight=1)
        month = self.session.month
        self.label(self.kbox, f"{month.year}年{month.month}月铺货结算报价（K）", size=16,
                   bold=True, bg="#FFF4EC").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self._price_entry(self.kbox, "K", emphasis=True).grid(row=1, column=0, sticky="ew")
        self.ceiling_hint = self._wrap(self.kbox, color=self.ORANGE, size=14, bg="#FFF4EC")
        self.ceiling_hint.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        self.ceiling_label = self._wrap(left, color=self.BLUE, size=19, bold=True, bg="#EAF5FF")
        self.ceiling_label.grid(row=3, column=0, sticky="ew", ipady=9, pady=(0, 10))
        self.label(left, "报价说明（写入 AO 备注）", size=15).grid(row=4, column=0, sticky="w", pady=(0, 5))
        self.remarks = tk.Text(left, height=2, width=1, relief="flat", highlightthickness=1,
            highlightbackground=self.LINE, highlightcolor=self.BLUE, borderwidth=0,
            bg=self.WHITE, fg=self.INK, font=(self.FONT, -14), wrap="word", padx=9, pady=6)
        self.remarks.grid(row=5, column=0, sticky="ew")
        self.remarks.insert("1.0", self.vars["AO"].get())
        self.remarks.edit_modified(False)
        self.remarks.bind("<<Modified>>", self._remarks_changed)
        attach = tk.Frame(left, bg=self.WHITE)
        attach.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.button(attach, "添加依据附件", self._attachments, padding=(8, 4),
                    image=self.wb.artwork.get("ui-icons/file-text-blue", 17) or "").pack(side="left")
        self.attachment_label = self.label(attach, color=self.MUTED, size=12)
        self.attachment_label.pack(side="left", padx=8)

        right = self.card(columns, padx=16, pady=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.columnconfigure(0, weight=1)
        right_header = tk.Frame(right, bg=self.WHITE)
        right_header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        right_header.columnconfigure(0, weight=1)
        self.label(right_header, "智能报价提示", size=21, bold=True).grid(row=0, column=0, sticky="w")
        self.rule_summary = self.label(right_header, color=self.ORANGE, size=14, bg="#FFF4EC", padx=9, pady=5)
        self.rule_summary.grid(row=0, column=1, sticky="e")
        area = tk.Frame(right, bg=self.WHITE, height=282)
        area.grid(row=1, column=0, sticky="ew")
        area.grid_propagate(False)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.rule_canvas = tk.Canvas(area, bg=self.WHITE, highlightthickness=0, yscrollincrement=1)
        self.rule_canvas.grid(row=0, column=0, sticky="nsew")
        scroll = SlimScrollbar(area, orient="vertical", command=self.rule_canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.rule_canvas.configure(yscrollcommand=scroll.set)
        self.rule_body = tk.Frame(self.rule_canvas, bg=self.WHITE)
        self.rule_body.columnconfigure(0, weight=1)
        self.rule_body._workbench_scroll_canvas = self.rule_canvas
        self.rule_canvas._workbench_scroll_canvas = self.rule_canvas
        item = self.rule_canvas.create_window((0, 0), window=self.rule_body, anchor="nw")
        self.rule_canvas.bind("<Configure>", lambda e: self.rule_canvas.itemconfigure(item, width=e.width))
        self.rule_body.bind("<Configure>", lambda e: self.rule_canvas.configure(scrollregion=self.rule_canvas.bbox("all")))
        self.label(right, "加价率按精确值判断，不按显示位数四舍五入放行", size=12,
                   color=self.MUTED).grid(row=2, column=0, sticky="w", pady=(6, 6))
        self.dates = tk.Frame(right, bg="#EFF7FF", padx=9, pady=7)
        self.dates.grid(row=3, column=0, sticky="ew")
        for col in range(2):
            self.dates.columnconfigure(col, weight=1, uniform="decision-date")
        date_labels = []
        for col, icon in enumerate(("clock", "box")):
            date_panel = tk.Frame(self.dates, bg="#EFF7FF")
            date_panel.grid(row=0, column=col, sticky="ew", padx=5)
            date_panel.columnconfigure(1, weight=1)
            self.label(date_panel, image=self.wb.artwork.get(f"ui-icons/{icon}-blue", 25) or "",
                       bg="#EFF7FF").grid(row=0, column=0, padx=(0, 9))
            text = self._wrap(date_panel, size=13, bg="#EFF7FF", color=self.MUTED)
            text.grid(row=0, column=1, sticky="ew")
            date_labels.append(text)
        self.dates_label, self.entry_date_label = date_labels
        self.qualification_button = self.button(right, "补充资格与日期依据 ›", self._qualifications, padding=(4, 2))
        self.qualification_button.grid(row=4, column=0, sticky="e", pady=(3, 0))
        self.youfu = tk.Frame(right, bg="#FFF5E8", padx=10, pady=8)
        self.youfu.grid(row=5, column=0, sticky="ew", pady=(5, 0))
        self.youfu.columnconfigure(0, weight=1)
        self.youfu_note = self._wrap(self.youfu, size=15, bold=True, bg="#FFF5E8", color=self.ORANGE)
        self.youfu_note.grid(row=0, column=0, sticky="ew")
        actions = tk.Frame(self.youfu, bg="#FFF5E8")
        actions.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        for col in range(2):
            actions.columnconfigure(col, weight=1, uniform="youfu-action")
        self.button(actions, "是，列入不报价", lambda: self._set_youfu("是"), padding=(7, 4)).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.button(actions, "否，继续报价校验", lambda: self._set_youfu("否"), padding=(7, 4)).grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.rule_advice = self._wrap(right, color=self.ORANGE, bg="#FFF4EC", size=14)
        self.rule_advice.grid(row=6, column=0, sticky="ew", ipady=9, pady=(6, 0))

        preview = self.card(self.content, padx=14, pady=6)
        preview.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        preview.columnconfigure(1, weight=1)
        self.label(preview, "本次报价表待写入内容", size=18, bold=True).grid(row=0, column=0, sticky="w")
        self._wrap(preview, f"  {self.session.quote_path.name if self.session.quote_path else '尚未生成工作簿'}  ·  第 {self.product.output_row} 行  ·  待写回",
                   size=13, color=self.BLUE).grid(row=0, column=1, sticky="ew", padx=(16, 0))
        cells = tk.Frame(preview, bg=self.WHITE)
        cells.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self.preview_values = {}
        for i, (key, title) in enumerate((("product", "商品"), ("K", "K 当月报价"), ("L", "L 采购价"),
                                          ("M", "M 全国均价"), ("P", "P 买断价"), ("Q", "Q 零售价"))):
            cells.columnconfigure(i, weight=1, uniform="preview")
            self.label(cells, title, size=13, bg="#EFF7FF", anchor="center").grid(row=0, column=i, sticky="ew", ipady=3)
            value = self.label(cells, self.product.title if key == "product" else "", size=14,
                               bold=True, bg="#E0EDFF", anchor="center", width=1)
            value.grid(row=1, column=i, sticky="ew", ipady=4)
            if key == "product":
                value.bind("<Configure>", lambda e, w=value: self._fit_preview(w))
            else:
                self.preview_values[key] = value
                value.bind("<Configure>", lambda e, w=value: self._fit_preview(w))
        footer = tk.Frame(self.content, bg=self.BG)
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 3))
        footer.columnconfigure(0, weight=1)
        self.feedback = self._wrap(footer, size=14, color=self.ORANGE, bg="#FFF4EC")
        self.feedback.grid(row=0, column=0, sticky="ew", padx=(0, 14), ipady=9)
        self.save_button = self.button(footer, "保存并写回报价表", self._save, primary=True, padding=(28, 14))
        self.save_button.grid(row=0, column=1, padx=(0, 10))
        self.confirm_button = self.button(footer, "确认本品报价", self._confirm, padding=(23, 14))
        self.confirm_button.grid(row=0, column=2)
        for var in self.vars.values():
            self.traces.append((var, var.trace_add("write", self._changed)))
        self._update_rules()

    def _fit_preview(self, widget):
        """Fit ordinary monetary values on one line without growing the table."""
        from tkinter import font
        measured = font.Font(root=self.parent, family=self.FONT, size=-14, weight="bold")
        available = max(1, widget.winfo_width() - 6)
        for size in range(14, 8, -1):
            measured.configure(size=-size)
            if measured.measure(widget.cget("text")) <= available:
                break
        widget.configure(font=(self.FONT, -size, "bold"))

    def _remarks_changed(self, _event=None):
        if self.dead or not self.remarks.edit_modified():
            return
        self.vars["AO"].set(self.remarks.get("1.0", "end-1c"))
        self.remarks.edit_modified(False)

    def _changed(self, *_args):
        if self.dead or self.loading or self.product is None:
            return
        self.product.values.update({key: var.get() for key, var in self.vars.items()})
        self._update_rules()

    def _update_rules(self):
        from quote_app.services.review_support import can_confirm, price_ceiling

        checks = self.session.evaluate(self.product)
        rules = [c for c in checks if c.category == "报价规则与资格"]
        failed = any(c.status == "未通过" for c in rules)
        pending = any(c.status in {"待复核", "待补充", "未检查"} for c in rules)
        youfu_check = next((c for c in rules if c.code == "E06"), None)
        ctx = self.product.context
        show_youfu = bool(youfu_check and ctx.get("entry_date") and youfu_check.reason.startswith(
            ("入库超过14个月", "已人工确认优福包", "已确认非优福包")))
        self.rule_summary.configure(text="优福包属性待确认" if show_youfu and ctx.get("youfu") not in {"是", "否"}
            else "当月报价需要调整" if failed else "仍有事项待核验" if pending else "报价规则已通过")
        for child in self.rule_body.winfo_children():
            child.destroy()
        table = tk.Frame(self.rule_body, bg=self.LINE, padx=1, pady=1)
        table.grid(row=0, column=0, sticky="ew")
        for col, weight in enumerate((31, 45, 24)):
            table.columnconfigure(col, weight=weight, uniform="rule-table")
        for col, heading in enumerate(("检查项", "当前比较", "结果")):
            self.label(table, heading, size=14, anchor="center", bg="#E8F4FE", width=1).grid(
                row=0, column=col, sticky="ew", ipady=7, padx=(0, 1))
        ordered = sorted(rules, key=lambda c: {"E02": 0, "E03": 1, "E01": 2}.get(c.code, 3))
        for index, check in enumerate(ordered, 1):
            color = {"通过": self.GREEN, "未通过": self.ORANGE, "不适用": self.MUTED}.get(check.status, self.ORANGE)
            for col, (text, ink) in enumerate(((check.title, self.INK),
                    (check.comparison or check.reason, self.INK),
                    (("✓  " if check.status == "通过" else "!  " if check.status == "未通过" else "") + check.status, color))):
                self._wrap(table, text, size=14 if col != 1 else 13, color=ink, width=1,
                           anchor="center").grid(row=index, column=col, sticky="nsew", ipady=5,
                                                  padx=(0, 1), pady=(1, 0))
        ceiling, reason = price_ceiling(self.product)
        self.ceiling_label.configure(text=f"当前价格上限   {money_preview(str(ceiling)) if ceiling is not None else '待补充依据'}")
        # Use the same bounded decimal parser as the service. In-progress enormous
        # exponents must never cause an unbounded subtraction/format operation.
        from quote_app.services.review_workbook import money
        from decimal import localcontext
        k = money(self.vars["K"].get())
        difference = None
        if ceiling is not None and k is not None and k > ceiling:
            with localcontext() as precision:
                precision.prec = max(28, len(k.as_tuple().digits) + abs(k.as_tuple().exponent) +
                                     len(ceiling.as_tuple().digits) + abs(ceiling.as_tuple().exponent) + 2)
                difference = money_preview(str(k - ceiling))
        self.ceiling_hint.configure(text=f"!  需下调至少 {difference}，才能满足当前价格上限" if difference
            else "依据已知价格计算；其他资格与口径仍须核验")
        self.dates_label.configure(text=f"首次报价：\n{ctx.get('first_quote_date') or '待补充'}")
        self.entry_date_label.configure(text=f"入库时间：\n{ctx.get('entry_date') or '待补充'}")
        if show_youfu:
            self.youfu.grid()
            self.dates.grid_remove()
            self.youfu_note.configure(text=youfu_check.reason)
            self.rule_advice.grid_remove()
        else:
            self.youfu.grid_remove()
            self.dates.grid()
            self.rule_advice.grid()
            self.rule_advice.configure(text=f"!  建议将当月报价调整至 {money_preview(str(ceiling))} 或以下，再次确认全部规则。"
                if difference else "请完成全部待核验事项，再确认本品报价。")
        self.attachment_label.configure(text=f"已关联 {len(self.product.attachments)} 个附件")
        for key, control in self.preview_values.items():
            control.configure(text=money_preview(self.vars[key].get()))
            self._fit_preview(control)
        editable = not self.session.running and self.session.quote_path is not None
        self.save_button.configure(state="normal" if editable else "disabled")
        self.confirm_button.configure(
            state="normal" if editable and can_confirm(checks) else "disabled"
        )
        for entry in self.entries:
            entry.configure(state="normal" if editable else "disabled")
        self.remarks.configure(state="normal" if editable else "disabled")
        self.feedback.configure(
            text=("未通过规则：可保存草稿，不可确认报送" if failed else
                  "仍有待核验事项，暂不可确认报价" if not can_confirm(checks) else "本品检查已完成，可确认报价")
            if editable
            else "本次报价表尚未就绪，请等待任务完成。"
        )

    def _set_youfu(self, value):
        self.product.context["youfu"] = value
        self._update_rules()
        self.feedback.configure(text="已更新本品优福包确认，请保存草稿以保留处理记录。")

    def _qualifications(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        dialog = self.dialog = tk.Toplevel(self.parent)
        dialog.title("补充资格与日期依据")
        dialog.configure(bg=self.WHITE)
        dialog.geometry("560x550")
        dialog.minsize(520, 520)
        dialog.transient(self.wb.root)
        dialog.columnconfigure(0, weight=1)
        box = tk.Frame(dialog, bg=self.WHITE, padx=22, pady=20)
        box.grid(sticky="nsew")
        box.columnconfigure(1, weight=1)
        self.label(box, "人工补充依据", size=17, bold=True).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self._wrap(
            box,
            "仅填写已经核实的信息。此处会记录为人工补充，不能把历史关联记录当作当前在库。日期格式：YYYY-MM-DD。",
            color=self.MUTED,
            size=10,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 18))
        variables = {}
        definitions = (
            ("category", "商品分类", ("未确认", "手机", "多形态")),
            ("stock", "一级库库存", ("未确认", "在库", "不在库")),
            ("entry_date", "入总部一级库日期", None),
            ("first_quote_date", "首次报价日期", None),
        )
        for row, (key, title, options) in enumerate(definitions, 2):
            self.label(box, title, size=11).grid(
                row=row, column=0, sticky="w", padx=(0, 14), pady=6
            )
            var = variables[key] = tk.StringVar(
                dialog, value=self.product.context.get(key, "") or (options[0] if options else "")
            )
            control = (
                SoftSelect(box, textvariable=var, values=options, width=270)
                if options
                else SoftEntry(box, textvariable=var, placeholder="YYYY-MM-DD", width=270)
            )
            control.grid(row=row, column=1, sticky="ew", pady=6)
        self.label(box, "依据来源 / 核实说明", size=11).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 6)
        )
        note = tk.Text(
            box,
            height=4,
            width=1,
            font=(self.FONT, 11),
            wrap="word",
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.LINE,
            padx=8,
            pady=8,
        )
        note.grid(row=7, column=0, columnspan=2, sticky="ew")
        note.insert("1.0", self.product.context.get("qualification_note", ""))

        def apply():
            values = {key: var.get().strip() for key, var in variables.items()}
            try:
                for key in ("entry_date", "first_quote_date"):
                    if values[key]:
                        parsed = date.fromisoformat(values[key])
                        if parsed > date.today():
                            raise ValueError("日期不能晚于今天")
                reason = note.get("1.0", "end-1c").strip()
                if not reason:
                    raise ValueError("请填写依据来源或核实说明")
            except ValueError as error:
                messagebox.showerror("请核对补充信息", str(error), parent=dialog)
                return
            if values["entry_date"] != self.product.context.get("entry_date"):
                self.product.context["youfu"] = "未确认"
            self.product.context.update(values, qualification_note=reason)
            dialog.destroy()
            self._update_rules()

        self.button(box, "应用并返回", apply, primary=True).grid(
            row=8, column=0, columnspan=2, sticky="e", pady=(16, 0)
        )

    def _attachments(self):
        paths = filedialog.askopenfilenames(parent=self.wb.root, title="关联报价依据附件")
        for path in paths:
            candidate = Path(path)
            if candidate not in self.product.attachments:
                self.product.attachments.append(candidate)
        self._update_rules()

    def open_evidence(self):
        if self.product is None:
            return
        paths = list(
            dict.fromkeys(r.evidence_path for r in self.product.channels if r.evidence_path)
        )
        if not paths:
            messagebox.showinfo(
                "取价证据",
                "当前商品没有已关联的截图记录。可前往价格情报查看本次渠道执行情况。",
                parent=self.wb.root,
            )
            return
        menu = tk.Menu(self.parent, tearoff=False)
        for path in paths:
            menu.add_command(label=path.name, command=lambda p=path: self.wb._open_path(p))
        menu.tk_popup(self.parent.winfo_pointerx(), self.parent.winfo_pointery())

    def _save(self):
        try:
            path = self.session.save(self.product)
        except (ValueError, OSError) as error:
            messagebox.showerror("报价草稿未保存", str(error), parent=self.wb.root)
            return
        self._update_rules()
        self.feedback.configure(
            text=f"已保存草稿并写回 {path.name} 第 {self.product.output_row} 行；未通过或待处理状态仍保留。",
            fg=self.GREEN,
        )
        self.wb.append_log(f"报价决策：{self.product.title} 手工填写部分已保存为草稿。")

    def _confirm(self):
        try:
            self.session.confirm_product(self.product)
        except (ValueError, OSError) as error:
            messagebox.showerror("暂不能确认本品报价", str(error), parent=self.wb.root)
            return
        self._update_rules()
        self.feedback.configure(
            text="本品报价已确认；最终报送版仍需在稽核工作台完成全批次检查。", fg=self.GREEN
        )

    def refresh(self):
        if self.dead:
            return
        if not self.show_list and self.product is not None:
            self._update_rules()

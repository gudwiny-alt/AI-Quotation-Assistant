"""Read-only AI advice surface. Worker arguments contain no Tk or business objects."""

from __future__ import annotations

import copy
from datetime import datetime
from queue import Empty, Queue
from threading import Thread
import time
import tkinter as tk

from .desktop_controls import SoftEntry
from .desktop_ui import BG, WHITE, BLUE, INK, MUTED, FONT, label, button
from .services.ai_gateway import (
    AIError,
    AISettings,
    generate_analysis,
    load_settings,
    save_settings,
)
from .services.ai_prompt import SYSTEM_PROMPT
from .services.ai_summary import SECTION_TITLES, build_snapshot

NAVY = "#173B77"
ACCENTS = ("#D98B16", "#2463EB", "#159987")


def _request(settings, snapshot, queue, gateway):
    try:
        result = gateway(settings, snapshot, SYSTEM_PROMPT)
        queue.put((result, ""))
    except AIError as error:
        queue.put((None, str(error)))
    except Exception:
        # Neither response bodies nor traceback/credentials are suitable UI messages.
        queue.put((None, "本次分析未完成，请检查模型设置或稍后重试。"))


class SummaryState:
    """In-memory analysis retained across page navigation; no persistence or business methods."""

    def __init__(self):
        self.busy = False
        self.snapshot = None
        self.result = None
        self.error = ""
        self.pending = None
        self.queue = Queue()

    def start(self, settings, snapshot, *, gateway=generate_analysis):
        if self.busy:
            return
        self.busy = True
        self.error = ""
        self.pending = copy.deepcopy(snapshot)
        Thread(
            target=_request, args=(settings, self.pending, self.queue, gateway), daemon=True
        ).start()

    def collect(self):
        try:
            result, error = self.queue.get_nowait()
        except Empty:
            return False
        self.busy = False
        self.error = error
        if result is not None:
            self.result, self.snapshot = result, self.pending
        self.pending = None
        return True


def settings_path(workbench):
    return workbench.app.app_paths.data_dir / "ai-settings.json"


def show_ai_settings(workbench):
    path = settings_path(workbench)
    try:
        settings = load_settings(path)
        problem = ""
    except AIError as error:
        settings = AISettings()
        problem = str(error)
    dialog = tk.Toplevel(workbench.root)
    dialog.title("AI 模型服务 · 仅保存在本机")
    dialog.configure(bg=WHITE)
    dialog.geometry("700x540")
    dialog.minsize(580, 520)
    dialog.transient(workbench.root)
    body = tk.Frame(dialog, bg=WHITE, padx=24, pady=22)
    body.pack(fill="both", expand=True)
    label(body, "终端公司 AI 大模型平台", size=-24, bold=True).pack(anchor="w")
    label(
        body,
        "使用兼容 Chat Completions 的接口；点击生成分析时才发送当前批次文字数据。",
        size=-14,
        color=MUTED,
        wraplength=630,
        justify="left",
    ).pack(anchor="w", pady=(8, 16))
    variables = []
    for title, value in (
        ("API Base URL", settings.base_url),
        ("模型名称", settings.model),
        ("API Key", ""),
    ):
        label(body, title, size=-15, bold=True).pack(anchor="w", pady=(6, 4))
        variable = tk.StringVar(dialog, value=value)
        field = SoftEntry(
            body,
            textvariable=variable,
            placeholder="已保存，留空保留；填写新 Key 可替换"
            if title == "API Key" and settings.api_key
            else "",
        )
        field.pack(fill="x")
        if title == "API Key":
            field.entry.configure(show="•")
        variables.append(variable)
    remove = tk.BooleanVar(dialog, value=False)
    tk.Checkbutton(
        body,
        text="清除本机已保存的 Key",
        variable=remove,
        bg=WHITE,
        fg=MUTED,
        font=(FONT, -14),
        highlightthickness=0,
    ).pack(anchor="w", pady=(8, 2))
    label(
        body,
        "Key 保存在应用数据目录的本机私有配置中，不进入代码、安装包或 GitHub。",
        size=-13,
        color=MUTED,
        wraplength=630,
        justify="left",
    ).pack(anchor="w", pady=(5, 8))
    status = label(body, problem, size=-14, color=ACCENTS[0], wraplength=620, justify="left")
    status.pack(anchor="w")
    actions = tk.Frame(body, bg=WHITE)
    actions.pack(fill="x", side="bottom", pady=(12, 0))

    def save():
        key = "" if remove.get() else variables[2].get().strip() or settings.api_key
        try:
            save_settings(
                path, AISettings(variables[0].get().strip(), variables[1].get().strip(), key)
            )
        except (AIError, OSError):
            status.configure(text="设置未保存，请核对服务地址、模型名称及本机目录权限。")
            return
        variables[2].set("")
        dialog.destroy()
        if workbench.page == "ai_summary" and workbench._review_view is not None:
            workbench._review_view.refresh(force=True)

    button(actions, "保存本机设置", save, primary=True).pack(side="right")
    button(actions, "取消", dialog.destroy).pack(side="right", padx=10)
    dialog.grab_set()
    return dialog


class AISummaryView:
    def __init__(self, workbench, parent, session):
        self.workbench, self.session = workbench, session
        if getattr(workbench, "_ai_state", None) is None:
            workbench._ai_state = SummaryState()
        self.state = workbench._ai_state
        self.snapshot = None
        self.snapshot_error = ""
        self.last_check = 0
        self.closed = False
        self.after_id = None
        self.content = workbench._scrollable(parent)
        toolbar = tk.Frame(self.content, bg=BG)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(3, 10))
        toolbar.columnconfigure(0, weight=1)
        self.metadata = label(toolbar, "", size=-14, color=MUTED, bg=BG)
        self.metadata.grid(row=0, column=0, sticky="w")
        button(
            toolbar, "打开报价表", lambda: workbench.open_review_workbook(destination="ai_summary")
        ).grid(row=0, column=1, padx=5)
        button(toolbar, "查看数据依据", self.show_sources).grid(row=0, column=2, padx=5)
        button(toolbar, "模型设置", lambda: show_ai_settings(workbench)).grid(row=0, column=3)
        self.notice = label(self.content, "", size=-14, color=ACCENTS[0], bg=BG, justify="left")
        self.notice.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        hero = tk.Frame(self.content, bg=NAVY, padx=24, pady=22)
        hero.grid(row=2, column=0, sticky="ew")
        label(hero, "本批次综合判断", size=-17, color="#BBD5FF", bold=True, bg=NAVY).pack(
            anchor="w"
        )
        self.conclusion = self._wrapped(hero, "尚未生成本批次分析", -25, WHITE, NAVY, True)
        self.conclusion.pack(fill="x", pady=(12, 9))
        self.hero_hint = self._wrapped(
            hero, "以实际批次数据形成经营建议，由人员判断与决策。", -14, "#C5D7F4", NAVY
        )
        self.hero_hint.pack(fill="x")
        self.priorities = tk.Frame(self.content, bg=BG)
        self.priorities.grid(row=3, column=0, sticky="ew", pady=14)
        self.sections = tk.Frame(self.content, bg=BG)
        self.sections.grid(row=4, column=0, sticky="ew")
        self.footer = self._wrapped(
            self.content,
            "AI 仅提供建议，不修改业务数据、报价或稽核结论。首次生成前，请在模型设置填写本机 Key。",
            -14,
            MUTED,
            BG,
        )
        self.footer.grid(row=5, column=0, sticky="ew", pady=(14, 18))
        self.tiles = []
        self.priority_tiles = []
        self.content.bind("<Configure>", self._layout, add="+")
        self.refresh(force=True)
        self.render()
        self._poll()

    @staticmethod
    def _wrapped(parent, text, size, color=INK, bg=WHITE, bold=False):
        widget = label(
            parent, text, size=size, color=color, bg=bg, bold=bold, justify="left", wraplength=600
        )
        widget.bind(
            "<Configure>", lambda e: widget.configure(wraplength=max(80, e.width - 4)), add="+"
        )
        return widget

    def _layout(self, event=None):
        width = self.content.winfo_width()
        columns = 2 if width >= 890 else 1
        signature = (columns, tuple(self.tiles), tuple(self.priority_tiles))
        if getattr(self, "_layout_signature", None) == signature:
            return
        self._layout_signature = signature
        for column in range(2):
            self.sections.columnconfigure(
                column,
                weight=1 if column < columns else 0,
                uniform="insights" if column < columns else "",
            )
        for index, tile in enumerate(self.tiles):
            tile.grid(
                row=index // columns,
                column=index % columns,
                sticky="nsew",
                padx=(0, 12 if columns == 2 and index % 2 == 0 else 0),
                pady=(0, 12),
            )
        count = 3 if width >= 890 else 1
        for column in range(3):
            self.priorities.columnconfigure(
                column,
                weight=1 if column < count else 0,
                uniform="priorities" if column < count else "",
            )
        for index, tile in enumerate(self.priority_tiles):
            tile.grid(
                row=index // count,
                column=index % count,
                sticky="nsew",
                padx=(0, 12 if count == 3 and index < 2 else 0),
                pady=(0, 5),
            )

    def refresh(self, force=False):
        if self.closed:
            return
        if force or time.monotonic() - self.last_check > 5:
            self.last_check = time.monotonic()
            try:
                self.snapshot = build_snapshot(self.session)
                self.snapshot_error = ""
            except (ValueError, OSError) as error:
                self.snapshot = None
                self.snapshot_error = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "数据暂时不可读，请检查本机报价文件。"
                )
            self._status()

    def _status(self):
        stale = self.state.result is not None and (
            self.snapshot is None
            or self.snapshot["snapshot_id"] != self.state.snapshot["snapshot_id"]
        )
        try:
            ready = load_settings(settings_path(self.workbench)).ready
        except AIError:
            ready = False
        notice = self.state.error or self.snapshot_error
        if self.state.busy:
            notice = "正在生成分析，可继续查看其他页面；本次请求使用点击时的数据快照。"
        elif stale:
            notice = "当前数据已变化，以下为旧快照建议，请重新生成分析。" + (
                " " + notice if notice else ""
            )
        elif not notice and not ready:
            notice = "请先在“模型设置”填写 API Key，然后生成本批次分析。"
        elif not notice:
            notice = "分析采用当前报价及稽核快照；建议由人员判断，不替代正式业务结论。"
        self.notice.configure(text=notice)
        count = len(self.snapshot["products"]) if self.snapshot else 0
        self.metadata.configure(text=f"本批次 {count} 款商品 · 文字数据分析 · 原图不上传")
        self.workbench.start_button.configure(
            text="正在生成…"
            if self.state.busy
            else "重新生成分析"
            if self.state.result
            else "生成分析",
            command=self.generate,
            state="disabled" if self.state.busy or self.snapshot is None else "normal",
        )

    def _poll(self):
        if self.closed:
            return
        if self.state.collect():
            self.refresh(force=True)
            self.render()
        self.after_id = self.workbench.root.after(250, self._poll)

    def generate(self):
        self.refresh(force=True)
        if self.snapshot is None or self.state.busy:
            return
        try:
            settings = load_settings(settings_path(self.workbench))
        except AIError as error:
            self.notice.configure(text=str(error))
            return
        if not settings.ready:
            show_ai_settings(self.workbench)
            return
        self.state.start(settings, self.snapshot)
        self._status()

    @staticmethod
    def _local_time(value):
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return str(value)

    def render(self):
        result = self.state.result
        # Old batch remains labelled, never relabelled as a current result.
        analysis = result["analysis"] if result else {}
        self.conclusion.configure(text=analysis.get("overall_conclusion", "尚未生成本批次分析"))
        hint = "结合业务执行、经营决策与风险控制，形成整批管理建议。"
        if result:
            snapshot = self.state.snapshot
            hint = f"分析快照：{snapshot['quote_month']} · {len(snapshot['products'])} 款商品  |  {result['model']}  |  {self._local_time(result['generated_at'])}"
        self.hero_hint.configure(text=hint)
        for parent in (self.priorities, self.sections):
            for child in parent.winfo_children():
                child.destroy()
        self.tiles, self.priority_tiles = [], []
        priorities = analysis.get("management_priorities", [])
        for index in range(3):
            tile = tk.Frame(
                self.priorities, bg=WHITE, highlightthickness=1, highlightbackground="#CAD6E7"
            )
            tk.Frame(tile, bg=ACCENTS[index], height=3).pack(fill="x")
            inner = tk.Frame(tile, bg=WHITE, padx=16, pady=14)
            inner.pack(fill="both", expand=True)
            label(
                inner, f"0{index + 1}  管理优先事项", size=-14, bold=True, color=ACCENTS[index]
            ).pack(anchor="w")
            value = priorities[index] if index < len(priorities) else None
            title = (
                value["recommendation"]
                if value
                else "生成后显示重点建议"
                if not result
                else "无其他优先建议"
            )
            self._wrapped(inner, title, -17, bold=True).pack(fill="x", pady=(9, 8))
            self._wrapped(
                inner,
                value["rationale"]
                if value
                else "围绕本批次实际数据，识别值得优先关注的经营事项。"
                if not result
                else "模型未提出更多有依据的优先事项。",
                -14,
                "#425570",
            ).pack(fill="x")
            if value:
                button(inner, "查看建议与依据", lambda item=value: self.show_priority(item)).pack(
                    anchor="w", pady=(10, 0)
                )
            self.priority_tiles.append(tile)
        sections = {s["id"]: s for s in analysis.get("sections", [])}
        placeholders = (
            "统筹本批次执行进展与管理重点。",
            "理解外部价格覆盖及跨渠道价格结构。",
            "识别报价约束、毛利空间及调价周期影响。",
            "归纳共同异常、复核依赖与剩余不确定性。",
        )
        icons = ("layout-dashboard", "chart-no-axes-column-increasing", "file-text", "shield-check")
        for index, (key, title) in enumerate(
            zip(("batch", "market", "pricing", "risk"), SECTION_TITLES)
        ):
            tile = tk.Frame(
                self.sections,
                bg=WHITE,
                padx=20,
                pady=18,
                highlightthickness=1,
                highlightbackground="#CAD6E7",
            )
            heading = tk.Frame(tile, bg=WHITE)
            heading.pack(fill="x")
            self.workbench._icon_label(heading, icons[index], size=30).pack(
                side="left", padx=(0, 10)
            )
            label(heading, title, size=-21, bold=True, color="#123363").pack(side="left")
            label(heading, "AI 建议", size=-12, color=BLUE, bg="#EFF5FF", padx=7, pady=3).pack(
                side="right"
            )
            section = sections.get(key)
            self._wrapped(
                tile, section["summary"] if section else placeholders[index], -18, bold=True
            ).pack(fill="x", pady=(16, 9))
            paragraphs = (
                section["paragraphs"][:2]
                if section
                else ["生成后将在这里展示基于当前批次数据的分析，不影响现有取价、报价与稽核流程。"]
            )
            excerpt = "\n\n".join(paragraphs)
            if len(excerpt) > 260:
                excerpt = excerpt[:260] + "…"
            self._wrapped(tile, excerpt, -15, "#425570").pack(fill="x", pady=(0, 15))
            actions = tk.Frame(tile, bg=WHITE)
            actions.pack(fill="x", side="bottom")
            button(
                actions,
                "展开全文",
                lambda s=section: self.show_section(s),
                state="normal" if section else "disabled",
            ).pack(side="left")
            button(
                actions,
                "查看依据",
                lambda s=section: self.show_sources(s.get("evidence_refs", []) if s else []),
                state="normal" if section else "disabled",
            ).pack(side="right")
            self.tiles.append(tile)
        self._layout()
        self._status()

    def _read_dialog(self, title, text):
        dialog = tk.Toplevel(self.workbench.root)
        dialog.title(title)
        dialog.geometry("820x640")
        dialog.configure(bg=WHITE)
        dialog.transient(self.workbench.root)
        label(dialog, title, size=-24, bold=True, padx=22, pady=18).pack(anchor="w")
        body = tk.Frame(dialog, bg=WHITE)
        body.pack(fill="both", expand=True, padx=22)
        scroll = tk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        field = tk.Text(
            body,
            wrap="word",
            font=(FONT, -16),
            bg=WHITE,
            fg=INK,
            relief="flat",
            padx=8,
            pady=8,
            yscrollcommand=scroll.set,
        )
        field.insert("1.0", text)
        field.configure(state="disabled")
        field.pack(fill="both", expand=True)
        scroll.configure(command=field.yview)
        button(dialog, "关闭", dialog.destroy).pack(anchor="e", padx=22, pady=15)
        return dialog

    def show_section(self, section):
        if section:
            self._read_dialog(
                section["title"],
                section["summary"]
                + "\n\n"
                + "\n\n".join(section["paragraphs"])
                + "\n\n"
                + "\n".join("分析边界：" + v for v in section["limitations"]),
            )

    def show_priority(self, item):
        self._read_dialog(
            "管理建议 · 仅供参考",
            item["recommendation"]
            + "\n\n依据："
            + item["rationale"]
            + "\n\n适用条件："
            + item["conditions"]
            + "\n\n"
            + self._source_text(item["evidence_refs"]),
        )

    def _source_text(self, refs=None):
        snapshot = self.state.snapshot if self.state.result else self.snapshot
        if not snapshot:
            return "暂无可展示的批次数据。"
        header = f"报价月份：{snapshot['quote_month']}  ·  商品 {len(snapshot['products'])} 款\n"
        header += "以下为生成分析所依据的只读快照；报价或证据变更后应重新生成。\n\n"
        if refs is None:
            refs = ["batch", "rules"] + [p["ref"] for p in snapshot["products"]]
        if self.state.result:
            result = self.state.result
            usage = result.get("usage", {})
            token_text = (
                "；".join(
                    f"{name} {usage[key]}"
                    for key, name in [
                        ("prompt_tokens", "输入Token"),
                        ("completion_tokens", "输出Token"),
                        ("total_tokens", "总Token"),
                    ]
                    if key in usage
                )
                or "服务未提供用量"
            )
            header += f"模型：{result['model']}；生成时间：{self._local_time(result['generated_at'])}；耗时：{result['elapsed_seconds']} 秒\n{token_text}\n规则版本：{snapshot['rule_version']}\n\n"
        lines = []
        for ref in refs:
            if ref == "batch":
                coverage = snapshot["coverage"]
                lines.append(
                    f"批次覆盖：{coverage['products']} 款商品，{coverage['channel_records']} 条渠道记录，{coverage['price_found']} 条已取价格。\n"
                    + "稽核检查："
                    + "、".join(f"{k} {v} 项" for k, v in coverage["check_statuses"].items())
                    + f"\n其中 {coverage['human_reviewed_checks']} 项采用有效人工复核。\n"
                    + "\n".join(snapshot["missing_context"])
                )
                continue
            if ref == "rules":
                lines.append(
                    "规则口径：手机加价率按采购价为基数，最高4.5%；毛利率按结算价为基数。\n"
                    + snapshot["rules"]["quotation_caps"]
                    + "\n"
                    + snapshot["rules"]["periodic"]
                    + "\n"
                    + snapshot["rules"]["youfu"]
                )
                continue
            product = next((p for p in snapshot["products"] if p["ref"] == ref), None)
            if product:
                values = product["values"]
                rows = [
                    snapshot["sources"][ref],
                    "价格（元/台）："
                    + "；".join(
                        f"{name} {values[key] or '未填写'}"
                        for key, name in [
                            ("K", "当月报价"),
                            ("L", "采购价"),
                            ("M", "全国均价"),
                            ("P", "买断价"),
                            ("Q", "分销零售价"),
                        ]
                    ),
                    f"产品经理：{product['confirmation']}；当前内容{'已写回' if product['written'] else '未写回'}。",
                    f"已知价格上限：{product['ceiling']['amount'] or '暂无'}；{product['ceiling']['basis']}",
                ]
                trial = product["trial"]
                if trial:
                    rows.append(
                        f"毛利率 {float(trial['margin']):.2f}%；采购加价率 {float(trial['markup']):.6f}%；单台价差 {trial['difference']} 元。"
                    )
                rows.extend(self._channel_text(c) for c in product["channels"])
                rows.append("报价说明：" + (product["quotation_note"] or "未填写"))
                lines.append("\n".join(rows))
                continue
            check = next((c for c in snapshot["checks"] if c["ref"] == ref), None)
            if check:
                lines.append(
                    f"{check['title']}：{check['status']}\n比较依据：{check['comparison']}\n判断说明：{check['reason']}"
                    + (
                        "\n有效人工复核理由：" + check["human_review_reason"]
                        if check["human_review_applied"]
                        else ""
                    )
                )
                continue
            channel = next(
                (c for p in snapshot["products"] for c in p["channels"] if c["ref"] == ref), None
            )
            if channel:
                lines.append(snapshot["sources"][ref] + "\n" + self._channel_text(channel))
        return header + (
            "\n\n".join(lines) if lines else "该建议未引用具体来源编号，请结合分析边界判断。"
        )

    @staticmethod
    def _channel_text(channel):
        from .desktop_state import CHANNEL_LABELS, OUTCOME_LABELS, STATE_LABELS

        return (
            f"{CHANNEL_LABELS.get(channel['channel'], channel['channel'])}：价格 {channel['price'] or '未取到'}；"
            f"{OUTCOME_LABELS.get(channel['outcome'], '暂无业务结果')}；"
            f"{STATE_LABELS.get(channel['task_state'], channel['task_state'])}；"
            f"来源：{channel['source_host'] or '未关联链接'}"
        )

    def show_sources(self, refs=None):
        self._read_dialog("分析数据依据 · 只读快照", self._source_text(refs))

    def destroy(self):
        self.closed = True
        if self.after_id:
            self.workbench.root.after_cancel(self.after_id)
            self.after_id = None

"""Read-only desktop projections of existing task events and persisted checkpoints.

Opening the normal task repository recovers interrupted tasks. UI browsing must
never do that, so these small queries use SQLite's read-only URI mode instead.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from quote_app.browser.worker import WorkerEvent
from quote_app.domain.models import QuoteRow
from quote_app.services.full_pipeline import FullPipelineResult
from quote_app.tasks.models import WebsiteObservationCheckpoint, WebsiteResult, WebsiteTask
from quote_app.tasks.serialization import from_payload

CHANNEL_LABELS = {"official": "官网", "tmall": "天猫", "jd": "京东"}
STATE_LABELS = {
    "pending": "待处理",
    "running": "采集中",
    "waiting_for_login": "等待登录",
    "paused": "已暂停",
    "succeeded": "渠道完成",
    "technical_failure": "技术失败",
    "created": "已创建",
    "completed": "已完成",
    "stopped": "已停止",
    "failed": "未完成",
}
OUTCOME_LABELS = {
    "price_found": "已获取价格",
    "no_model": "无该机型",
    "sold_out": "已售罄",
    "capacity_unavailable": "容量不可用",
    "color_unavailable": "颜色不可用",
}


def compact_path(value: str, limit: int = 34) -> str:
    if not value.strip():
        return "尚未选择"
    name = Path(value).name
    if len(name) <= limit:
        return name
    suffix = Path(name).suffix
    return name[: max(1, limit - len(suffix) - 1)] + "…" + suffix


@dataclass(slots=True)
class TaskRow:
    task_id: str
    model_name: str = ""
    channel: str = ""
    price: str = ""
    outcome: str = ""
    state: str = "pending"
    evidence_state: str = "pending"
    evidence_path: Path | None = None
    url: str = ""
    error: str = ""
    specification: str = ""

    @property
    def outcome_label(self) -> str:
        return OUTCOME_LABELS.get(self.outcome, "尚未获取")

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def evidence_label(self) -> str:
        return {"complete": "截图已保存", "missing": "截图文件缺失"}.get(
            self.evidence_state, "截图待补" if self.outcome else "尚无证据"
        )


@dataclass(slots=True)
class DesktopState:
    rows: list[TaskRow] = field(default_factory=list)
    quote_rows: tuple[QuoteRow, ...] = ()
    run_id: str | None = None
    running: bool = False
    quote_path: Path | None = None
    report_path: Path | None = None
    summary: str = "尚未开始任务"

    @property
    def evidence_count(self) -> int:
        return sum(row.evidence_state == "complete" for row in self.rows)

    def begin_run(self) -> None:
        self.rows.clear()
        self.quote_rows = ()
        self.run_id = None
        self.quote_path = self.report_path = None
        self.running = True
        self.summary = "任务运行中 · 等待渠道事件"

    def apply_event(self, event: WorkerEvent) -> None:
        self.run_id = event.run_id
        row = next((item for item in self.rows if item.task_id == event.task_id), None)
        if row is None:
            row = TaskRow(event.task_id)
            self.rows.append(row)
        for key in ("model_name", "channel"):
            value = event.data.get(key)
            if isinstance(value, str):
                setattr(row, key, value)
        if event.event in {"result", "observation"}:
            row.outcome = str(event.data.get("outcome") or "")
            price = event.data.get("price")
            row.price = str(price) if row.outcome == "price_found" and price is not None else ""
            row.error = ""
            row.state = "succeeded" if event.event == "result" else "running"
            row.evidence_state = "complete" if event.event == "result" else "pending"
        elif event.event == "technical_failure":
            row.state = "technical_failure"
            row.error = str(event.data.get("error_code") or "UNKNOWN")
        elif event.event == "waiting_for_login":
            row.state = "waiting_for_login"
        elif event.event == "progress":
            row.state = "running"
        self.summary = f"{CHANNEL_LABELS.get(row.channel, row.channel)} / {row.model_name or row.task_id} · {row.state_label}"

    def finish(self, result: FullPipelineResult | BaseException) -> None:
        self.running = False
        if isinstance(result, BaseException):
            self.summary = f"任务未完成：{result}"
            return
        self.quote_rows = result.rows
        self.quote_path, self.report_path = result.quote_path, result.report_path
        self.summary = (
            f"输出已生成 · {result.summary.total_rows} 条商品 · "
            f"待登录 {result.website_summary.waiting_for_login} · "
            f"技术失败 {result.website_summary.technical_failure}"
        )


@dataclass(frozen=True, slots=True)
class HistoryRun:
    run_id: str
    state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    runs: tuple[HistoryRun, ...] = ()
    error: str = ""


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_history(path: Path) -> HistorySnapshot:
    if not path.is_file():
        return HistorySnapshot()
    try:
        with closing(_readonly_connection(path)) as connection:
            rows = connection.execute(
                "SELECT run_id, state, created_at, updated_at FROM quotation_runs "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return HistorySnapshot(tuple(HistoryRun(**dict(row)) for row in rows))
    except (sqlite3.Error, OSError, ValueError) as error:
        return HistorySnapshot(error=f"任务记录暂不可读取：{error}")


def read_task_rows(path: Path, run_id: str) -> tuple[list[TaskRow], str]:
    """Read one saved run; preserve observations when final capture failed."""
    if not path.is_file():
        return [], "尚无已保存任务"
    try:
        with closing(_readonly_connection(path)) as connection:
            records = connection.execute(
                "SELECT t.payload_json, t.state, r.result_json, o.payload_json AS observation "
                "FROM website_tasks t LEFT JOIN website_results r ON t.task_id=r.task_id AND t.generation=r.generation "
                "LEFT JOIN website_observations o ON t.task_id=o.task_id AND t.generation=o.generation "
                "WHERE t.run_id=? ORDER BY t.rowid",
                (run_id,),
            ).fetchall()
        rows = []
        for record in records:
            task = from_payload(json.loads(record["payload_json"]))
            if not isinstance(task, WebsiteTask):
                raise ValueError("任务数据类型不匹配")
            row = TaskRow(
                task.task_id,
                task.model_name,
                task.channel.value,
                state=record["state"],
                specification=f"{task.ram} / {task.storage} / {task.color}",
            )
            if record["observation"]:
                observed = from_payload(json.loads(record["observation"]))
                if isinstance(observed, WebsiteObservationCheckpoint):
                    row.outcome, row.url = observed.outcome.value, observed.url
                    row.price = str(observed.price) if observed.price is not None else ""
            if record["result_json"]:
                result = from_payload(json.loads(record["result_json"]))
                if isinstance(result, WebsiteResult):
                    if result.outcome:
                        row.outcome = result.outcome.value
                        row.price = str(result.price) if result.price is not None else ""
                    row.url = result.url or row.url
                    row.error = result.error_code or ""
                    if result.evidence:
                        row.evidence_path = result.evidence.path
                        row.evidence_state = (
                            "complete" if row.evidence_path.is_file() else "missing"
                        )
            rows.append(row)
        return rows, ""
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as error:
        return [], f"任务明细暂不可读取：{error}"


def quote_channel_prices(row: QuoteRow) -> tuple[str, str, str]:
    """Official, Tmall, JD in UI order, using the verified writeback columns."""

    def value(column: str) -> str:
        raw = row.cells.get(column)
        return "—" if raw is None or raw == "" else str(raw)

    return value("AK"), value("AJ"), value("AI")

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from quote_app.core.normalization import normalize_brand, normalize_code, normalize_text
from quote_app.domain.models import Issue, QuoteMonth, QuoteRow, WebQuery
from quote_app.tasks.models import (
    SCHEMA_VERSION,
    InputFingerprint,
    RunRecord,
    RunState,
    WebsiteChannel,
    WebsiteTask,
)
from quote_app.tasks.serialization import to_payload

SUPPORTED_BRANDS = frozenset(
    {"HONOR", "华为", "维沃", "欧珀", "小米", "苹果", "ZTE中兴"}
)
SOURCE_ROLES = ("base", "marketing", "bop")
SNAPSHOT_TYPE = "quote_rows"

_CHANNEL_ORDER = (
    WebsiteChannel.JD,
    WebsiteChannel.TMALL,
    WebsiteChannel.OFFICIAL,
)
_WEB_QUERY_FIELDS = ("brand", "model_name", "ram", "storage", "color")
_WEB_QUERY_FIELD_LABELS = {
    "brand": "品牌",
    "model_name": "型号",
    "ram": "运行内存",
    "storage": "存储容量",
    "color": "颜色",
}


@dataclass(frozen=True, slots=True)
class TaskBuildIssue:
    code: str
    message: str
    source_row_number: int
    output_row_number: int
    material_code: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskBuildResult:
    tasks: tuple[WebsiteTask, ...]
    issues: tuple[TaskBuildIssue, ...]


def create_run_record(
    quote_month: QuoteMonth,
    base_path: Path,
    marketing_path: Path,
    bop_path: Path,
    output_dir: Path,
    browser_profile_dir: Path,
    rows: Sequence[QuoteRow],
) -> RunRecord:
    """Create a new immutable run identity over exact inputs and ordered rows."""

    source_paths = (base_path, marketing_path, bop_path)
    fingerprints = tuple(
        _fingerprint_file(role, path)
        for role, path in zip(SOURCE_ROLES, source_paths, strict=True)
    )
    snapshot = _snapshot_rows(rows)
    snapshot_sha256 = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    run = RunRecord(
        run_id=str(uuid4()),
        schema_version=SCHEMA_VERSION,
        quote_month=quote_month,
        input_fingerprints=fingerprints,
        output_dir=Path(output_dir),
        browser_profile_dir=Path(browser_profile_dir),
        associated_rows_snapshot=snapshot,
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256=snapshot_sha256,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=now,
        updated_at=now,
    )
    # Reuse the durable boundary's allow-list and credential rejection rules.
    to_payload(run)
    return run


def build_website_tasks(
    run: RunRecord,
    rows: Sequence[QuoteRow],
) -> TaskBuildResult:
    """Build deterministic channel tasks without reading presentation cells."""

    _verify_run_snapshot(run, rows)
    tasks: list[WebsiteTask] = []
    issues: list[TaskBuildIssue] = []

    for output_row_number, row in enumerate(rows, start=2):
        query_values = _normalized_query(row.web_query)
        missing_fields = tuple(
            field_name
            for field_name in _WEB_QUERY_FIELDS
            if not query_values[field_name]
        )
        material_code = normalize_code(row.material_code)

        if missing_fields:
            issues.append(
                TaskBuildIssue(
                    code="WEB_FIELDS_MISSING",
                    message=(
                        f"基础表第{row.source_row_number}行缺少网站查询字段："
                        f"{'、'.join(_WEB_QUERY_FIELD_LABELS[field] for field in missing_fields)}"
                    ),
                    source_row_number=row.source_row_number,
                    output_row_number=output_row_number,
                    material_code=material_code,
                    fields=missing_fields,
                )
            )
            continue

        brand = query_values["brand"]
        if brand not in SUPPORTED_BRANDS:
            issues.append(
                TaskBuildIssue(
                    code="UNSUPPORTED_BRAND",
                    message=(
                        f"基础表第{row.source_row_number}行品牌暂不支持自动查价：{brand}"
                    ),
                    source_row_number=row.source_row_number,
                    output_row_number=output_row_number,
                    material_code=material_code,
                    fields=("brand",),
                )
            )
            continue

        if not material_code:
            issues.append(
                TaskBuildIssue(
                    code="MATERIAL_CODE_MISSING",
                    message=f"基础表第{row.source_row_number}行缺少物料编码",
                    source_row_number=row.source_row_number,
                    output_row_number=output_row_number,
                    material_code="",
                    fields=("material_code",),
                )
            )
            continue

        for channel in _CHANNEL_ORDER:
            tasks.append(
                WebsiteTask(
                    task_id=_task_id(
                        run.run_id,
                        output_row_number,
                        material_code,
                        channel,
                    ),
                    run_id=run.run_id,
                    source_row_number=row.source_row_number,
                    output_row_number=output_row_number,
                    material_code=material_code,
                    brand=brand,
                    model_name=query_values["model_name"],
                    ram=query_values["ram"],
                    storage=query_values["storage"],
                    color=query_values["color"],
                    channel=channel,
                )
            )

    return TaskBuildResult(tasks=tuple(tasks), issues=tuple(issues))


def _fingerprint_file(source_role: str, path: Path) -> InputFingerprint:
    normalized_path = Path(path).expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    with normalized_path.open("rb") as source:
        before = os.fstat(source.fileno())
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    path_after = normalized_path.stat()
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(path_after)
    ):
        raise ValueError("输入文件在读取过程中发生变化，请停止编辑后重试")
    return InputFingerprint(
        source_role=source_role,
        path=normalized_path,
        sha256=digest.hexdigest(),
        byte_size=after.st_size,
        modified_ns=after.st_mtime_ns,
    )


def _stat_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _snapshot_rows(rows: Sequence[QuoteRow]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_type": SNAPSHOT_TYPE,
        "rows": [_snapshot_row(row) for row in rows],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _verify_run_snapshot(run: RunRecord, rows: Sequence[QuoteRow]) -> None:
    if run.associated_rows_snapshot is not None:
        stored_snapshot = run.associated_rows_snapshot.encode("utf-8")
    else:
        snapshot_path = run.associated_rows_snapshot_path
        if snapshot_path is None:
            raise ValueError("运行记录缺少报价行快照")
        try:
            stored_snapshot = snapshot_path.read_bytes()
        except OSError as exc:
            raise ValueError("运行记录的报价行快照无法读取") from exc

    stored_digest = hashlib.sha256(stored_snapshot).hexdigest()
    if stored_digest != run.associated_rows_snapshot_sha256:
        raise ValueError("运行记录的报价行快照校验失败")

    current_snapshot = _snapshot_rows(rows).encode("utf-8")
    if hashlib.sha256(current_snapshot).hexdigest() != stored_digest:
        raise ValueError("报价行与运行记录快照不一致")


def _snapshot_row(row: QuoteRow) -> dict[str, Any]:
    return {
        "source_row_number": row.source_row_number,
        "material_code": row.material_code,
        "cells": {
            str(column): _snapshot_value(value)
            for column, value in row.cells.items()
        },
        "issues": [_snapshot_issue(issue) for issue in row.issues],
        "web_query": {
            field_name: getattr(row.web_query, field_name)
            for field_name in _WEB_QUERY_FIELDS
        },
    }


def _snapshot_issue(issue: Issue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "message": issue.message,
        "fatal": issue.fatal,
        "row_number": issue.row_number,
        "source": issue.source,
    }


def _snapshot_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return {"value_type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"value_type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"value_type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"value_type": "time", "value": value.isoformat()}
    raise TypeError(f"unsupported quote-row snapshot value: {type(value).__name__}")


def _normalized_query(query: WebQuery) -> dict[str, str]:
    brand_text = normalize_text(query.brand)
    return {
        "brand": normalize_brand(brand_text) if brand_text else "",
        "model_name": normalize_text(query.model_name),
        "ram": normalize_text(query.ram),
        "storage": normalize_text(query.storage),
        "color": normalize_text(query.color),
    }


def _task_id(
    run_id: str,
    output_row_number: int,
    material_code: str,
    channel: WebsiteChannel,
) -> str:
    identity = f"{run_id}|{output_row_number}|{material_code}|{channel.value}"
    return str(uuid5(NAMESPACE_URL, identity))

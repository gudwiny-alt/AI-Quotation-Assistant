from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import platform as host_platform

from PIL import Image, UnidentifiedImageError

from quote_app.domain.models import InputPaths, Issue, QuoteMonth, QuoteRow
from quote_app.evidence.validation import EvidenceFileAudit, read_validated_evidence
from quote_app.evidence.models import (
    MacCapturePolicy,
    accepts_capture_validation,
    validate_mac_capture_policy,
)
from quote_app.excel.quote_writer import (
    QuoteEvidenceImage,
    QuoteWriteRequest,
    write_quote_workbook,
)
from quote_app.excel.report_writer import (
    ReportWriteRequest,
    RunSummary,
    assess_rows,
    summarize_assessments,
    write_execution_report,
)
from quote_app.services.core_pipeline import CorePipelineError
from quote_app.services.web_binding_validation import validate_website_task_bindings
from quote_app.resources import bundled_resource_path
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
)


def default_template_path() -> Path:
    """Locate the bundled quote template in source and frozen application runs."""
    return bundled_resource_path("resources/templates/quote_template.xlsx")


DEFAULT_TEMPLATE_PATH = default_template_path()
_CHANNEL_ORDER = (
    WebsiteChannel.JD,
    WebsiteChannel.TMALL,
    WebsiteChannel.OFFICIAL,
)
_CHANNEL_COLUMNS = {
    WebsiteChannel.JD: ("AI", "AL"),
    WebsiteChannel.TMALL: ("AJ", "AM"),
    WebsiteChannel.OFFICIAL: ("AK", "AN"),
}
_LEGAL_NO_OUTCOMES = frozenset(
    (
        BusinessOutcome.NO_MODEL,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        BusinessOutcome.COLOR_UNAVAILABLE,
        BusinessOutcome.SOLD_OUT,
    )
)
_WEB_OUTPUT_COLUMNS = (
    "AH",
    "R",
    "S",
    "AI",
    "AJ",
    "AK",
    "AL",
    "AM",
    "AN",
)


@dataclass(frozen=True, slots=True)
class WebToExcelRequest:
    quote_month: QuoteMonth
    rows: tuple[QuoteRow, ...]
    tasks: tuple[WebsiteTask, ...]
    results: tuple[WebsiteResult, ...]
    output_dir: Path
    observations: tuple[WebsiteObservationCheckpoint, ...] = ()
    waiting_task_ids: frozenset[str] = frozenset()
    template_path: str | Path = DEFAULT_TEMPLATE_PATH
    input_paths: InputPaths | None = None
    run_at: datetime | None = None
    capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT
    quote_destination_path: Path | None = None
    report_destination_path: Path | None = None
    report_quote_path: Path | None = None

    def __post_init__(self) -> None:
        validate_mac_capture_policy(
            self.capture_acceptance_policy,
            platform_name=host_platform.system(),
        )


@dataclass(frozen=True, slots=True)
class WebToExcelResult:
    quote_path: Path
    report_path: Path
    summary: RunSummary
    rows: tuple[QuoteRow, ...]


def write_web_results_to_excel(
    request: WebToExcelRequest,
) -> WebToExcelResult:
    if not isinstance(request, WebToExcelRequest):
        raise TypeError("request must be a WebToExcelRequest")
    rows = _copy_rows(request.rows)
    _validated_result_index(
        rows,
        request.tasks,
        request.results,
        request.observations,
        capture_acceptance_policy=request.capture_acceptance_policy,
    )
    evidence_payloads, report_results = _prepare_evidence(
        request.results,
        capture_acceptance_policy=request.capture_acceptance_policy,
    )
    publishable_result_by_task = {
        result.task_id: result
        for result in report_results
    }
    observation_by_task = {
        observation.task_id: observation
        for observation in request.observations
    }
    images = _map_results(
        rows,
        request.tasks,
        publishable_result_by_task,
        observation_by_task,
        evidence_payloads,
    )

    quote_path = write_quote_workbook(
        QuoteWriteRequest(
            quote_month=request.quote_month,
            rows=list(rows),
            template_path=request.template_path,
            output_dir=request.output_dir,
            evidence_images=images,
            destination_path=request.quote_destination_path,
        )
    )
    assessments = assess_rows(
        list(rows),
        request.tasks,
        report_results,
        request.observations,
        request.waiting_task_ids,
        True,
        capture_acceptance_policy=request.capture_acceptance_policy,
    )
    summary = summarize_assessments(assessments)
    try:
        report_path = write_execution_report(
            ReportWriteRequest(
                quote_month=request.quote_month,
                rows=list(rows),
                output_dir=request.output_dir,
                input_paths=request.input_paths,
                quote_path=request.report_quote_path or quote_path,
                run_at=request.run_at,
                website_tasks=request.tasks,
                website_results=report_results,
                website_observations=request.observations,
                waiting_task_ids=request.waiting_task_ids,
                website_run=True,
                capture_acceptance_policy=request.capture_acceptance_policy,
                destination_path=request.report_destination_path,
            )
        )
    except Exception:
        if request.quote_destination_path is not None:
            raise
        try:
            quote_path.unlink(missing_ok=True)
        except OSError:
            raise _output_error(
                "ROLLBACK_FAILED",
                (
                    "执行报告生成失败，且本次报价表无法自动回滚；"
                    f"请手工删除孤立文件：{quote_path}"
                ),
            ) from None
        raise _output_error(
            "OUTPUT_PAIR_FAILED",
            "执行报告生成失败，本次报价表已自动回滚，未留下不完整输出",
        ) from None
    return WebToExcelResult(
        quote_path=quote_path,
        report_path=report_path,
        summary=summary,
        rows=rows,
    )


def _copy_rows(rows: tuple[QuoteRow, ...]) -> tuple[QuoteRow, ...]:
    return tuple(
        QuoteRow(
            source_row_number=row.source_row_number,
            material_code=row.material_code,
            cells=dict(row.cells),
            issues=list(row.issues),
            web_query=row.web_query,
        )
        for row in rows
    )


def _validated_result_index(
    rows: tuple[QuoteRow, ...],
    tasks: tuple[WebsiteTask, ...],
    results: tuple[WebsiteResult, ...],
    observations: tuple[WebsiteObservationCheckpoint, ...],
    *,
    capture_acceptance_policy: MacCapturePolicy,
) -> dict[str, WebsiteResult]:
    tasks_by_id = validate_website_task_bindings(rows, tasks)

    result_by_task: dict[str, WebsiteResult] = {}
    for result in results:
        _require_accepted_evidence(result, capture_acceptance_policy)
        if result.task_id in result_by_task:
            raise ValueError("website results contain a duplicate task_id")
        if result.task_id not in tasks_by_id:
            raise ValueError("website result does not belong to a supplied task")
        result_by_task[result.task_id] = result
    observation_by_task: dict[str, WebsiteObservationCheckpoint] = {}
    for observation in observations:
        if observation.task_id in observation_by_task:
            raise ValueError("website observations contain a duplicate task_id")
        if observation.task_id not in tasks_by_id:
            raise ValueError("website observation does not belong to a supplied task")
        observation_by_task[observation.task_id] = observation
    return result_by_task


def _prepare_evidence(
    results: tuple[WebsiteResult, ...],
    *,
    capture_acceptance_policy: MacCapturePolicy,
) -> tuple[dict[str, bytes], tuple[WebsiteResult, ...]]:
    payloads: dict[str, bytes] = {}
    report_results: list[WebsiteResult] = []
    for result in results:
        _require_accepted_evidence(result, capture_acceptance_policy)
        if result.state is not TaskState.SUCCEEDED:
            report_results.append(result)
            continue
        evidence = result.evidence
        if evidence is None:
            report_results.append(
                _evidence_failure_result(
                    result,
                    EvidenceFileAudit(
                        payload=None,
                        error_code="EVIDENCE_MISSING",
                        error_message="正式截图记录缺失",
                    ),
                )
            )
            continue
        if result.diagnostic_path is not None:
            audit = EvidenceFileAudit(
                payload=None,
                error_code="EVIDENCE_DIAGNOSTIC",
                error_message="诊断截图不能作为正式截图",
            )
        else:
            audit = read_validated_evidence(
                evidence,
                capture_acceptance_policy=capture_acceptance_policy,
            )
        if audit.payload is None:
            report_results.append(_evidence_failure_result(result, audit))
            continue
        try:
            with Image.open(BytesIO(audit.payload)) as image:
                dimensions = image.size
                image.verify()
        except (OSError, UnidentifiedImageError):
            report_results.append(
                _evidence_failure_result(
                    result,
                    EvidenceFileAudit(
                        payload=None,
                        error_code="EVIDENCE_IMAGE_INVALID",
                        error_message="正式截图不是可读取的图片",
                    ),
                )
            )
            continue
        if dimensions != (evidence.pixel_width, evidence.pixel_height):
            report_results.append(
                _evidence_failure_result(
                    result,
                    EvidenceFileAudit(
                        payload=None,
                        error_code="EVIDENCE_DIMENSIONS",
                        error_message="正式截图像素尺寸校验失败",
                    ),
                )
            )
            continue
        payloads[result.task_id] = audit.payload
        report_results.append(result)
    return payloads, tuple(report_results)


def _require_accepted_evidence(
    result: WebsiteResult,
    capture_acceptance_policy: MacCapturePolicy,
) -> None:
    if result.state is not TaskState.SUCCEEDED:
        return
    evidence = result.evidence
    if evidence is None:
        return
    if (
        evidence.validation_code == "CAPTURE_OK_MAC_VISUAL_REVIEW"
        and not accepts_capture_validation(
        evidence.validation_code,
        policy=capture_acceptance_policy,
        platform_name=host_platform.system(),
        )
    ):
        raise ValueError("capture validation is not accepted for this publication")


def _evidence_failure_result(
    result: WebsiteResult,
    audit: EvidenceFileAudit,
) -> WebsiteResult:
    return WebsiteResult(
        task_id=result.task_id,
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url=None,
        evidence=None,
        diagnostic_path=None,
        error_code=audit.error_code or "EVIDENCE_INVALID",
        error_message=audit.error_message or "正式截图未通过发布验证",
    )


def _map_results(
    rows: tuple[QuoteRow, ...],
    tasks: tuple[WebsiteTask, ...],
    result_by_task: dict[str, WebsiteResult],
    observation_by_task: dict[str, WebsiteObservationCheckpoint],
    evidence_payloads: dict[str, bytes],
) -> tuple[QuoteEvidenceImage, ...]:
    for row in rows:
        for column in _WEB_OUTPUT_COLUMNS:
            row.cells.pop(column, None)

    by_row: dict[
        int,
        dict[WebsiteChannel, WebsiteResult | WebsiteObservationCheckpoint],
    ] = {}
    images: list[QuoteEvidenceImage] = []
    for task in tasks:
        complete_result = result_by_task.get(task.task_id)
        observation = observation_by_task.get(task.task_id)
        source = (
            complete_result
            if (
                complete_result is not None
                and complete_result.state is TaskState.SUCCEEDED
            )
            else observation
        )
        if source is None:
            continue
        row = rows[task.output_row_number - 2]
        by_row.setdefault(task.output_row_number, {})[task.channel] = source
        price_column, evidence_column = _CHANNEL_COLUMNS[task.channel]
        if source.outcome is BusinessOutcome.PRICE_FOUND:
            row.cells[price_column] = source.price
        elif source.outcome in _LEGAL_NO_OUTCOMES:
            row.cells[price_column] = "无"
        else:
            raise ValueError("website result has an unsupported business outcome")
        if (
            complete_result is not None
            and complete_result.state is TaskState.SUCCEEDED
            and (payload := evidence_payloads.get(complete_result.task_id)) is not None
        ):
            images.append(
                QuoteEvidenceImage(
                    anchor=f"{evidence_column}{task.output_row_number}",
                    payload=payload,
                )
            )

    for output_row_number, row in enumerate(rows, start=2):
        _map_minimum(row, by_row.get(output_row_number, {}))
    return tuple(images)


def _map_minimum(
    row: QuoteRow,
    channel_results: dict[
        WebsiteChannel,
        WebsiteResult | WebsiteObservationCheckpoint,
    ],
) -> None:
    numeric: list[tuple[Decimal, int, str]] = []
    for priority, channel in enumerate(_CHANNEL_ORDER):
        result = channel_results.get(channel)
        if (
            result is not None
            and result.outcome is BusinessOutcome.PRICE_FOUND
            and result.price is not None
            and result.url is not None
        ):
            numeric.append((result.price, priority, result.url))
    if numeric:
        price, _priority, url = min(
            numeric,
            key=lambda item: (item[0], item[1]),
        )
        row.cells["AH"] = price
        row.cells["R"] = price
        row.cells["S"] = url
        return

    all_legal_no = len(channel_results) == len(_CHANNEL_ORDER) and all(
        result.outcome in _LEGAL_NO_OUTCOMES for result in channel_results.values()
    )
    if all_legal_no:
        row.cells["AH"] = "无"
        row.cells["R"] = "无"
        row.cells["S"] = "无"


def _output_error(code: str, message: str) -> CorePipelineError:
    return CorePipelineError(
        (
            Issue(
                code=code,
                message=message,
                fatal=True,
            ),
        ),
        heading="报价输出未完成",
    )

"""End-to-end composition from local source workbooks to final outputs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quote_app.core.association import associate_rows
from quote_app.core.normalization import normalize_brand
from quote_app.core.precheck import precheck_inputs
from quote_app.domain.models import InputPaths, Issue, QuoteMonth, QuoteRow
from quote_app.evidence.models import MacCapturePolicy
from quote_app.excel.report_writer import RunSummary
from quote_app.services.core_pipeline import CorePipelineError
from quote_app.services.incremental_publication import IncrementalExcelPublisher
from quote_app.services.readiness import ReadinessCheck
from quote_app.services.web_run import (
    WebsiteRunController,
    WebsiteRunRequest,
    WebsiteRunSnapshot,
    WebsiteRunSummary,
    run_website_tasks,
)
from quote_app.services.web_to_excel import (
    DEFAULT_TEMPLATE_PATH,
    WebToExcelRequest,
    write_web_results_to_excel,
)
from quote_app.tasks.builder import (
    TaskBuildIssue,
    build_website_tasks,
    create_run_record,
)
from quote_app.tasks.models import (
    TaskState,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.repository import SQLiteTaskRepository


WebsiteRunner = Callable[[WebsiteRunRequest], WebsiteRunSummary]
RuntimeReadinessChecker = Callable[[], ReadinessCheck]


class RuntimeReadinessError(RuntimeError):
    """The local browser/capture environment is not ready for website work."""

    def __init__(self, readiness: ReadinessCheck) -> None:
        if not isinstance(readiness, ReadinessCheck):
            raise TypeError("readiness must be ReadinessCheck")
        if readiness.ready:
            raise ValueError("ready environments must not raise RuntimeReadinessError")
        self.readiness = readiness
        super().__init__("；".join(readiness.messages))


@dataclass(frozen=True, slots=True)
class FullPipelineRequest:
    paths: InputPaths
    quote_month: QuoteMonth
    browser_profile_dir: Path
    database_path: Path
    evidence_dir: Path
    template_path: str | Path = DEFAULT_TEMPLATE_PATH
    capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT
    runtime_readiness: RuntimeReadinessChecker | None = None
    controller: WebsiteRunController | None = None
    selected_brand: str | None = None


@dataclass(frozen=True, slots=True)
class FullPipelineResult:
    quote_path: Path
    report_path: Path
    summary: RunSummary
    rows: tuple[QuoteRow, ...]
    website_summary: WebsiteRunSummary


def run_full_pipeline(
    request: FullPipelineRequest,
    *,
    website_runner: WebsiteRunner = run_website_tasks,
) -> FullPipelineResult:
    """Create one durable website run and publish the resulting Excel pair."""
    if not isinstance(request, FullPipelineRequest):
        raise TypeError("request must be a FullPipelineRequest")
    if not callable(website_runner):
        raise TypeError("website_runner must be callable")
    selected_brand = _normalize_selected_brand(request.selected_brand)
    if request.runtime_readiness is not None and not callable(
        request.runtime_readiness
    ):
        raise TypeError("runtime_readiness must be callable")
    if request.runtime_readiness is not None:
        readiness = request.runtime_readiness()
        if not isinstance(readiness, ReadinessCheck):
            raise TypeError("runtime_readiness must return ReadinessCheck")
        if not readiness.ready:
            raise RuntimeReadinessError(readiness)

    rows = _prepare_rows(
        request.paths,
        request.quote_month,
        selected_brand=selected_brand,
    )
    run = create_run_record(
        request.quote_month,
        request.paths.base,
        request.paths.marketing,
        request.paths.bop,
        request.paths.output_dir,
        request.browser_profile_dir,
        rows,
    )
    task_build = build_website_tasks(run, rows)
    _apply_task_build_issues(rows, task_build.issues)

    with SQLiteTaskRepository(request.database_path) as repository:
        repository.create_run(run)

    publisher = IncrementalExcelPublisher(
        quote_month=request.quote_month,
        rows=tuple(rows),
        tasks=task_build.tasks,
        output_dir=request.paths.output_dir,
        template_path=request.template_path,
        input_paths=request.paths,
        capture_acceptance_policy=request.capture_acceptance_policy,
    )
    publisher.publish(
        WebsiteRunSnapshot(
            observations=(),
            results=(),
            waiting_task_ids=frozenset(),
        )
    )

    website_request = WebsiteRunRequest(
        run_id=run.run_id,
        tasks=task_build.tasks,
        profile_dir=request.browser_profile_dir,
        evidence_dir=request.evidence_dir,
        database_path=request.database_path,
        controller=request.controller,
        checkpoint_sink=publisher.publish,
    )
    website_summary = _run_or_skip(website_request, website_runner)
    results = _load_saved_results(request.database_path, task_build.tasks)
    observations = _load_saved_observations(request.database_path, task_build.tasks)
    waiting_task_ids = _load_waiting_task_ids(
        request.database_path,
        task_build.tasks,
    )
    publisher.publish(
        WebsiteRunSnapshot(
            observations=observations,
            results=results,
            waiting_task_ids=waiting_task_ids,
        )
    )
    output = write_web_results_to_excel(
        WebToExcelRequest(
            quote_month=request.quote_month,
            rows=tuple(rows),
            tasks=task_build.tasks,
            results=results,
            observations=observations,
            output_dir=request.paths.output_dir,
            waiting_task_ids=waiting_task_ids,
            template_path=request.template_path,
            input_paths=request.paths,
            capture_acceptance_policy=request.capture_acceptance_policy,
        )
    )
    return FullPipelineResult(
        quote_path=output.quote_path,
        report_path=output.report_path,
        summary=output.summary,
        rows=output.rows,
        website_summary=website_summary,
    )


def _prepare_rows(
    paths: InputPaths,
    quote_month: QuoteMonth,
    *,
    selected_brand: str | None = None,
) -> list[QuoteRow]:
    precheck = precheck_inputs(paths, quote_month)
    if precheck.fatal_issues:
        raise CorePipelineError(precheck.fatal_issues)

    rows = associate_rows(
        precheck.identified_sheets["base"],
        precheck.identified_sheets["marketing"],
        precheck.identified_sheets["bop"],
        quote_month,
    )
    rows = [row for row in rows if row.material_code]
    if not rows:
        raise CorePipelineError(
            (
                Issue(
                    code="NO_QUOTABLE_ROWS",
                    message="基础表没有可报价的非空物料编码",
                    fatal=True,
                ),
            )
        )
    _apply_base_precheck_issues(rows, precheck.row_issues)
    return _select_brand_rows(rows, selected_brand)


def _normalize_selected_brand(selected_brand: str | None) -> str | None:
    if selected_brand is None:
        return None
    if not isinstance(selected_brand, str):
        raise TypeError("selected_brand must be a string or None")
    normalized = normalize_brand(selected_brand)
    if not normalized:
        raise ValueError("selected_brand must not be blank")
    return normalized


def _select_brand_rows(
    rows: list[QuoteRow], selected_brand: str | None
) -> list[QuoteRow]:
    if selected_brand is None:
        return rows
    matching = [
        row
        for row in rows
        if normalize_brand(row.cells.get("B")) == selected_brand
    ]
    if not matching:
        raise CorePipelineError(
            (
                Issue(
                    code="SELECTED_BRAND_NOT_FOUND",
                    message=f"未找到可用于荣耀闭环穿测的 {selected_brand} 记录",
                    fatal=True,
                ),
            )
        )
    return matching


def _apply_base_precheck_issues(
    rows: list[QuoteRow],
    issues: tuple[Issue, ...],
) -> None:
    by_row: dict[int, list[Issue]] = {}
    for issue in issues:
        if issue.source == "base" and issue.row_number is not None:
            by_row.setdefault(issue.row_number, []).append(issue)
    for row in rows:
        row.issues.extend(by_row.get(row.source_row_number, ()))


def _apply_task_build_issues(
    rows: list[QuoteRow],
    issues: tuple[TaskBuildIssue, ...],
) -> None:
    for task_issue in issues:
        row_number = task_issue.output_row_number
        if not 2 <= row_number < len(rows) + 2:
            raise ValueError("website task build issue does not bind to a quotation row")
        row = rows[row_number - 2]
        if not any(
            issue.code == task_issue.code and issue.message == task_issue.message
            for issue in row.issues
        ):
            row.issues.append(
                Issue(
                    code=task_issue.code,
                    message=task_issue.message,
                    fatal=False,
                    row_number=row.source_row_number,
                )
            )


def _run_or_skip(
    request: WebsiteRunRequest,
    website_runner: WebsiteRunner,
) -> WebsiteRunSummary:
    if request.tasks:
        return website_runner(request)
    return WebsiteRunSummary(
        succeeded=0,
        waiting_for_login=0,
        technical_failure=0,
        evidence_paths=(),
    )


def _load_saved_results(
    database_path: Path,
    tasks: tuple[WebsiteTask, ...],
) -> tuple[WebsiteResult, ...]:
    results: list[WebsiteResult] = []
    with SQLiteTaskRepository(database_path) as repository:
        for task in tasks:
            result = repository.load_result(task.task_id)
            if result is not None:
                results.append(result)
    return tuple(results)


def _load_saved_observations(
    database_path: Path,
    tasks: tuple[WebsiteTask, ...],
) -> tuple[WebsiteObservationCheckpoint, ...]:
    observations: list[WebsiteObservationCheckpoint] = []
    with SQLiteTaskRepository(database_path) as repository:
        for task in tasks:
            observation = repository.load_observation(task.task_id)
            if observation is not None:
                observations.append(observation)
    return tuple(observations)


def _load_waiting_task_ids(
    database_path: Path,
    tasks: tuple[WebsiteTask, ...],
) -> frozenset[str]:
    with SQLiteTaskRepository(database_path) as repository:
        return frozenset(
            task.task_id
            for task in tasks
            if repository.task_state(task.task_id) is TaskState.WAITING_FOR_LOGIN
        )

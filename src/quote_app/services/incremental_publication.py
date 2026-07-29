"""Stable, readable workbook snapshots published during a website run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import threading

from quote_app.domain.models import InputPaths, QuoteMonth, QuoteRow
from quote_app.evidence.models import MacCapturePolicy
from quote_app.services.web_run import WebsiteRunSnapshot
from quote_app.services.web_to_excel import (
    WebToExcelRequest,
    WebToExcelResult,
    write_web_results_to_excel,
)
from quote_app.tasks.models import WebsiteTask


@dataclass(frozen=True, slots=True)
class IncrementalPublicationPaths:
    quote_path: Path
    report_path: Path


class IncrementalExcelPublisher:
    """Publish immutable website snapshots without opening the task database."""

    def __init__(
        self,
        *,
        quote_month: QuoteMonth,
        rows: tuple[QuoteRow, ...],
        tasks: tuple[WebsiteTask, ...],
        output_dir: Path,
        template_path: str | Path,
        input_paths: InputPaths | None,
        capture_acceptance_policy: MacCapturePolicy,
        run_at: datetime | None = None,
    ) -> None:
        if not isinstance(output_dir, Path):
            raise TypeError("output_dir must be a Path")
        self._quote_month = quote_month
        self._rows = tuple(
            QuoteRow(
                source_row_number=row.source_row_number,
                material_code=row.material_code,
                cells=dict(row.cells),
                issues=list(row.issues),
                web_query=row.web_query,
            )
            for row in rows
        )
        self._tasks = tuple(tasks)
        self._output_dir = output_dir.expanduser().resolve()
        self._template_path = template_path
        self._input_paths = input_paths
        self._capture_acceptance_policy = capture_acceptance_policy
        self._run_at = run_at
        self._paths = IncrementalPublicationPaths(
            quote_path=(
                self._output_dir
                / f"{quote_month.year}年{quote_month.month:02d}月终端供货价报价表-处理中.xlsx"
            ),
            report_path=(
                self._output_dir
                / f"{quote_month.year}年{quote_month.month:02d}月报价执行报告-处理中.xlsx"
            ),
        )
        self._lock = threading.Lock()

    @property
    def paths(self) -> IncrementalPublicationPaths:
        return self._paths

    def publish(self, snapshot: WebsiteRunSnapshot) -> WebToExcelResult:
        if not isinstance(snapshot, WebsiteRunSnapshot):
            raise TypeError("snapshot must be a WebsiteRunSnapshot")
        with self._lock:
            return write_web_results_to_excel(
                WebToExcelRequest(
                    quote_month=self._quote_month,
                    rows=self._rows,
                    tasks=self._tasks,
                    results=snapshot.results,
                    observations=snapshot.observations,
                    waiting_task_ids=snapshot.waiting_task_ids,
                    output_dir=self._output_dir,
                    template_path=self._template_path,
                    input_paths=self._input_paths,
                    run_at=self._run_at,
                    capture_acceptance_policy=self._capture_acceptance_policy,
                    quote_destination_path=self._paths.quote_path,
                    report_destination_path=self._paths.report_path,
                )
            )

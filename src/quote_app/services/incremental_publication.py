"""Stable, readable workbook snapshots published during a website run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
import threading
from uuid import uuid4
import os

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
            staging = _staging_paths(self._output_dir)
            try:
                rendered = write_web_results_to_excel(
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
                        quote_destination_path=staging.quote_path,
                        report_destination_path=staging.report_path,
                        report_quote_path=self._paths.quote_path,
                    )
                )
                _commit_pair(staging, self._paths)
                return replace(
                    rendered,
                    quote_path=self._paths.quote_path,
                    report_path=self._paths.report_path,
                )
            finally:
                _remove_path(staging.quote_path)
                _remove_path(staging.report_path)


def _staging_paths(output_dir: Path) -> IncrementalPublicationPaths:
    token = uuid4().hex
    return IncrementalPublicationPaths(
        output_dir / f".incremental-{token}.quote-stage.xlsx",
        output_dir / f".incremental-{token}.report-stage.xlsx",
    )


def _backup_paths(output_dir: Path) -> IncrementalPublicationPaths:
    token = uuid4().hex
    return IncrementalPublicationPaths(
        output_dir / f".incremental-{token}.quote-backup.xlsx",
        output_dir / f".incremental-{token}.report-backup.xlsx",
    )


def _commit_pair(
    staging: IncrementalPublicationPaths,
    stable: IncrementalPublicationPaths,
) -> None:
    quote_exists = _path_exists(stable.quote_path)
    report_exists = _path_exists(stable.report_path)
    if quote_exists != report_exists:
        raise RuntimeError("处理中报价文件对不完整，无法安全更新")
    backups = _backup_paths(stable.quote_path.parent)
    try:
        if quote_exists:
            os.link(stable.quote_path, backups.quote_path)
            os.link(stable.report_path, backups.report_path)
        _replace_path(staging.quote_path, stable.quote_path)
        _replace_path(staging.report_path, stable.report_path)
    except Exception:
        _restore_pair(stable, backups, quote_exists)
        raise
    finally:
        _remove_path(backups.quote_path)
        _remove_path(backups.report_path)


def _restore_pair(
    stable: IncrementalPublicationPaths,
    backups: IncrementalPublicationPaths,
    had_previous_pair: bool,
) -> None:
    if had_previous_pair:
        _restore_path(backups.quote_path, stable.quote_path)
        _restore_path(backups.report_path, stable.report_path)
    else:
        _remove_path(stable.quote_path)
        _remove_path(stable.report_path)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _restore_path(source: Path, destination: Path) -> None:
    if _path_exists(source):
        os.replace(source, destination)


def _remove_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

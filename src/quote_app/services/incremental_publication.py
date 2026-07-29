"""Stable, readable workbook snapshots published during a website run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from uuid import uuid4
import os
from typing import Protocol

try:  # pragma: no cover - the alternate import is exercised with a fake.
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None  # type: ignore[assignment]
try:  # pragma: no cover - unavailable on POSIX.
    import msvcrt as _msvcrt
except ImportError:  # POSIX
    _msvcrt = None  # type: ignore[assignment]

from quote_app.domain.models import InputPaths, QuoteMonth, QuoteRow
from quote_app.evidence.models import MacCapturePolicy
from quote_app.services.web_run import WebsiteRunSnapshot
from quote_app.services.web_to_excel import (
    EvidenceValidationCache,
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
        self._evidence_cache = EvidenceValidationCache()
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
                        evidence_cache=self._evidence_cache,
                    )
                )
                with _pair_process_lock(
                    self._output_dir,
                    self._quote_month,
                ):
                    _commit_pair(staging, self._paths)
                return replace(
                    rendered,
                    quote_path=self._paths.quote_path,
                    report_path=self._paths.report_path,
                )
            finally:
                _remove_path(staging.quote_path)
                _remove_path(staging.report_path)


_LARGE_WORKLOAD_TASK_THRESHOLD = 100
_LARGE_WORKLOAD_BATCH_SIZE = 25
_MAX_PUBLICATION_DELAY_SECONDS = 5.0


class SnapshotPublisher(Protocol):
    def publish(self, snapshot: WebsiteRunSnapshot) -> WebToExcelResult: ...


class CoalescingSnapshotQueue:
    """One-slot queue whose latest durable snapshot supersedes older snapshots."""

    def __init__(
        self,
        *,
        task_count: int,
        max_delay_seconds: float = _MAX_PUBLICATION_DELAY_SECONDS,
    ) -> None:
        if type(task_count) is not int or task_count < 0:
            raise ValueError("task_count must be a non-negative integer")
        self._batch_size = (
            _LARGE_WORKLOAD_BATCH_SIZE
            if task_count >= _LARGE_WORKLOAD_TASK_THRESHOLD
            else 1
        )
        if (
            not isinstance(max_delay_seconds, int | float)
            or isinstance(max_delay_seconds, bool)
            or max_delay_seconds <= 0
        ):
            raise ValueError("max_delay_seconds must be positive")
        self._max_delay_seconds = float(max_delay_seconds)
        self._snapshot: WebsiteRunSnapshot | None = None
        self._offered = 0
        self._first_offered_at: float | None = None

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def pending(self) -> bool:
        return self._snapshot is not None

    @property
    def ready(self) -> bool:
        return self.pending and self._offered >= self._batch_size

    @property
    def deadline(self) -> float | None:
        if self._first_offered_at is None:
            return None
        return self._first_offered_at + self._max_delay_seconds

    def ready_at(self, now: float) -> bool:
        if not isinstance(now, int | float) or isinstance(now, bool):
            raise TypeError("now must be numeric")
        return self.ready or (
            self.pending
            and self.deadline is not None
            and float(now) >= self.deadline
        )

    def offer(
        self,
        snapshot: WebsiteRunSnapshot,
        *,
        offered_at: float = 0.0,
    ) -> None:
        if not isinstance(snapshot, WebsiteRunSnapshot):
            raise TypeError("snapshot must be a WebsiteRunSnapshot")
        if not isinstance(offered_at, int | float) or isinstance(offered_at, bool):
            raise TypeError("offered_at must be numeric")
        if not self.pending:
            self._first_offered_at = float(offered_at)
        self._snapshot = snapshot
        self._offered += 1

    def take(
        self,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> WebsiteRunSnapshot | None:
        ready = self.ready if now is None else self.ready_at(now)
        if not self.pending or (not force and not ready):
            return None
        snapshot = self._snapshot
        self._snapshot = None
        self._offered = 0
        self._first_offered_at = None
        return snapshot


class CoalescingPublicationWorker:
    """Render large-workload snapshots off the serial browser callback path."""

    def __init__(
        self,
        publisher: SnapshotPublisher,
        *,
        task_count: int,
        max_delay_seconds: float = _MAX_PUBLICATION_DELAY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(getattr(publisher, "publish", None)):
            raise TypeError("publisher must provide publish")
        if (
            not isinstance(max_delay_seconds, int | float)
            or isinstance(max_delay_seconds, bool)
            or max_delay_seconds <= 0
        ):
            raise ValueError("max_delay_seconds must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._publisher = publisher
        self._queue = CoalescingSnapshotQueue(
            task_count=task_count,
            max_delay_seconds=max_delay_seconds,
        )
        self._clock = clock
        self._condition = threading.Condition()
        self._force = False
        self._active = False
        self._stopping = False
        self._error: BaseException | None = None
        self._last_result: WebToExcelResult | None = None
        self._publication_count = 0
        self._thread = threading.Thread(
            target=self._run,
            name="quote-excel-publication",
            daemon=True,
        )
        self._thread.start()

    @property
    def batch_size(self) -> int:
        return self._queue.batch_size

    @property
    def publication_count(self) -> int:
        with self._condition:
            return self._publication_count

    def submit(self, snapshot: WebsiteRunSnapshot) -> None:
        with self._condition:
            self._raise_if_failed()
            if self._stopping:
                raise RuntimeError("publication worker is closed")
            self._queue.offer(snapshot, offered_at=self._clock())
            if self._queue.ready:
                self._condition.notify_all()

    def flush(self) -> WebToExcelResult | None:
        with self._condition:
            self._raise_if_failed()
            self._force = self._queue.pending
            self._condition.notify_all()
            self._condition.wait_for(
                lambda: self._error is not None
                or (not self._queue.pending and not self._active)
            )
            self._raise_if_failed()
            return self._last_result

    def close(self, *, suppress_error: bool = False) -> None:
        error: BaseException | None = None
        try:
            self.flush()
        except BaseException as caught:
            error = caught
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join()
        if error is not None and not suppress_error:
            raise error

    def _run(self) -> None:
        while True:
            with self._condition:
                snapshot = self._wait_for_snapshot()
                if snapshot is None:
                    return
                self._active = True
                self._force = False
            try:
                result = self._publisher.publish(snapshot)
            except BaseException as error:
                with self._condition:
                    self._error = error
                    self._active = False
                    self._condition.notify_all()
                return
            with self._condition:
                self._last_result = result
                self._publication_count += 1
                self._active = False
                self._condition.notify_all()

    def _wait_for_snapshot(self) -> WebsiteRunSnapshot | None:
        while True:
            if self._error is not None:
                return None
            if self._stopping and not self._queue.pending:
                return None
            if self._queue.pending:
                now = self._clock()
                if (
                    self._force
                    or self._queue.ready_at(now)
                    or self._stopping
                ):
                    return self._queue.take(force=True)
                deadline = self._queue.deadline
                if deadline is None:
                    raise AssertionError("pending snapshot has no deadline")
                remaining = deadline - now
                if remaining <= 0:
                    return self._queue.take(force=True)
                self._condition.wait(timeout=remaining)
                continue
            self._condition.wait()

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error


def _staging_paths(output_dir: Path) -> IncrementalPublicationPaths:
    token = uuid4().hex
    return IncrementalPublicationPaths(
        output_dir / f".incremental-{token}.quote-stage.xlsx",
        output_dir / f".incremental-{token}.report-stage.xlsx",
    )


@contextmanager
def _pair_process_lock(
    output_dir: Path,
    quote_month: QuoteMonth,
    *,
    blocking: bool = True,
):
    """Lock one normalized output-directory/month pair across app processes."""
    if not isinstance(output_dir, Path):
        raise TypeError("output_dir must be a Path")
    if not isinstance(quote_month, QuoteMonth):
        raise TypeError("quote_month must be a QuoteMonth")
    if type(blocking) is not bool:
        raise TypeError("blocking must be a bool")
    normalized_output = output_dir.expanduser().resolve()
    normalized_output.mkdir(parents=True, exist_ok=True)
    lock_path = normalized_output / (
        f".quote-pair-{quote_month.year}-{quote_month.month:02d}.lock"
    )
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        _lock_descriptor(descriptor, blocking=blocking)
        yield
    finally:
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _lock_descriptor(descriptor: int, *, blocking: bool) -> None:
    if _fcntl is not None:
        operation = _fcntl.LOCK_EX
        if not blocking:
            operation |= _fcntl.LOCK_NB
        _fcntl.flock(descriptor, operation)
        return
    if _msvcrt is None:
        raise RuntimeError("no supported cross-process file-lock backend")
    os.lseek(descriptor, 0, os.SEEK_END)
    if os.lseek(descriptor, 0, os.SEEK_CUR) == 0:
        os.write(descriptor, b"\0")
    os.lseek(descriptor, 0, os.SEEK_SET)
    mode = _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK
    try:
        _msvcrt.locking(descriptor, mode, 1)
    except OSError as error:
        if not blocking:
            raise BlockingIOError(
                error.errno,
                "incremental publication pair is locked",
            ) from error
        raise


def _unlock_descriptor(descriptor: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        return
    if _msvcrt is None:
        raise RuntimeError("no supported cross-process file-lock backend")
    os.lseek(descriptor, 0, os.SEEK_SET)
    _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)


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

from __future__ import annotations

from pathlib import Path
import threading
import multiprocessing
import os

import pytest

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import MacCapturePolicy
from quote_app.excel.report_writer import RunSummary
from quote_app.services import incremental_publication
from quote_app.services.web_run import WebsiteRunSnapshot
from quote_app.services.web_to_excel import WebToExcelResult


def _hold_incremental_pair_lock(
    output_dir: str,
    ready: object,
    release: object,
) -> None:
    from quote_app.services.incremental_publication import _pair_process_lock

    with _pair_process_lock(
        Path(output_dir),
        QuoteMonth(2026, 8),
    ):
        ready.send(True)  # type: ignore[attr-defined]
        release.recv()  # type: ignore[attr-defined]


def _publisher(tmp_path: Path) -> incremental_publication.IncrementalExcelPublisher:
    return incremental_publication.IncrementalExcelPublisher(
        quote_month=QuoteMonth(2026, 8),
        rows=(),
        tasks=(),
        output_dir=tmp_path,
        template_path=tmp_path / "template.xlsx",
        input_paths=None,
        capture_acceptance_policy=MacCapturePolicy.STRICT,
    )


def _result(request: object) -> WebToExcelResult:
    quote = request.quote_destination_path  # type: ignore[union-attr]
    report = request.report_destination_path  # type: ignore[union-attr]
    assert quote is not None and report is not None
    quote.write_bytes(b"new-quote")
    report.write_bytes(b"new-report")
    return WebToExcelResult(
        quote,
        report,
        RunSummary(0, 0, 0, 0, 0, 0),
        (),
    )


def test_render_failure_keeps_existing_pair_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: report render failure leaves a new quote beside old report."""
    publisher = _publisher(tmp_path)
    publisher.paths.quote_path.write_bytes(b"old-quote")
    publisher.paths.report_path.write_bytes(b"old-report")

    def fail_after_quote(request: object) -> WebToExcelResult:
        quote = request.quote_destination_path  # type: ignore[union-attr]
        assert quote is not None
        quote.write_bytes(b"new-quote")
        raise RuntimeError("report staging failed")

    monkeypatch.setattr(
        incremental_publication,
        "write_web_results_to_excel",
        fail_after_quote,
    )

    with pytest.raises(RuntimeError, match="report staging failed"):
        publisher.publish(WebsiteRunSnapshot((), (), frozenset()))

    assert publisher.paths.quote_path.read_bytes() == b"old-quote"
    assert publisher.paths.report_path.read_bytes() == b"old-report"
    assert not tuple(tmp_path.glob(".incremental-*"))


def test_second_replace_failure_restores_existing_pair_and_cleans_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a failed report replace permanently mixes generations."""
    publisher = _publisher(tmp_path)
    publisher.paths.quote_path.write_bytes(b"old-quote")
    publisher.paths.report_path.write_bytes(b"old-report")
    monkeypatch.setattr(incremental_publication, "write_web_results_to_excel", _result)
    real_replace = incremental_publication._replace_path

    def fail_report_replace(source: Path, destination: Path) -> None:
        if destination == publisher.paths.report_path and source.name.endswith(".report-stage.xlsx"):
            raise OSError("report replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(incremental_publication, "_replace_path", fail_report_replace)

    with pytest.raises(OSError, match="report replace failed"):
        publisher.publish(WebsiteRunSnapshot((), (), frozenset()))

    assert publisher.paths.quote_path.read_bytes() == b"old-quote"
    assert publisher.paths.report_path.read_bytes() == b"old-report"
    assert not tuple(tmp_path.glob(".incremental-*"))


def test_first_publish_failure_leaves_no_pair_and_success_returns_stable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a failed first publish leaves a one-sided stable workbook."""
    publisher = _publisher(tmp_path)

    def fail_after_quote(request: object) -> WebToExcelResult:
        quote = request.quote_destination_path  # type: ignore[union-attr]
        assert quote is not None
        quote.write_bytes(b"new-quote")
        raise RuntimeError("report staging failed")

    monkeypatch.setattr(
        incremental_publication,
        "write_web_results_to_excel",
        fail_after_quote,
    )
    with pytest.raises(RuntimeError, match="report staging failed"):
        publisher.publish(WebsiteRunSnapshot((), (), frozenset()))
    assert not publisher.paths.quote_path.exists()
    assert not publisher.paths.report_path.exists()
    assert not tuple(tmp_path.glob(".incremental-*"))

    monkeypatch.setattr(incremental_publication, "write_web_results_to_excel", _result)
    result = publisher.publish(WebsiteRunSnapshot((), (), frozenset()))

    assert result.quote_path == publisher.paths.quote_path
    assert result.report_path == publisher.paths.report_path
    assert publisher.paths.quote_path.read_bytes() == b"new-quote"
    assert publisher.paths.report_path.read_bytes() == b"new-report"
    assert not tuple(tmp_path.glob(".incremental-*"))


@pytest.mark.parametrize(
    ("row_count", "stage_events", "maximum_publications"),
    ((200, 1200, 49), (300, 1800, 73)),
)
def test_large_workload_coalescing_has_a_deterministic_publication_budget(
    row_count: int,
    stage_events: int,
    maximum_publications: int,
) -> None:
    """Break caught: 200–300 rows synchronously rebuild 1,200–1,800 workbook pairs."""
    queue = incremental_publication.CoalescingSnapshotQueue(
        task_count=row_count * 3,
    )
    snapshot = WebsiteRunSnapshot((), (), frozenset())
    publication_count = 0

    for _ in range(stage_events):
        queue.offer(snapshot)
        if queue.ready:
            assert queue.take() == snapshot
            publication_count += 1
    if queue.pending:
        assert queue.take(force=True) == snapshot
        publication_count += 1

    assert publication_count <= maximum_publications


def test_large_workload_submit_does_not_block_on_excel_render() -> None:
    """Break caught: browser callbacks wait for the current full XLSX render."""
    render_started = threading.Event()
    release_render = threading.Event()

    class Publisher:
        def publish(self, _snapshot: WebsiteRunSnapshot) -> WebToExcelResult:
            render_started.set()
            assert release_render.wait(2)
            return WebToExcelResult(
                Path("quote.xlsx"),
                Path("report.xlsx"),
                RunSummary(0, 0, 0, 0, 0, 0),
                (),
            )

    worker = incremental_publication.CoalescingPublicationWorker(
        Publisher(),  # type: ignore[arg-type]
        task_count=600,
    )
    try:
        for _ in range(worker.batch_size):
            worker.submit(WebsiteRunSnapshot((), (), frozenset()))
        assert render_started.wait(1)

        callback_returned = threading.Event()
        callback = threading.Thread(
            target=lambda: (
                worker.submit(WebsiteRunSnapshot((), (), frozenset())),
                callback_returned.set(),
            )
        )
        callback.start()
        assert callback_returned.wait(1)
        callback.join(timeout=1)
    finally:
        release_render.set()
        worker.close()


def test_large_workload_lone_price_snapshot_is_ready_within_five_seconds() -> None:
    """Break caught: one saved price waits indefinitely for a batch to fill."""
    queue = incremental_publication.CoalescingSnapshotQueue(
        task_count=600,
        max_delay_seconds=5,
    )
    snapshot = WebsiteRunSnapshot((), (), frozenset())
    queue.offer(snapshot, offered_at=100)

    assert queue.ready_at(104.999) is False
    assert queue.ready_at(105) is True
    assert queue.take(now=105) == snapshot


def test_publication_worker_surfaces_background_failure_on_flush() -> None:
    class Publisher:
        def publish(self, _snapshot: WebsiteRunSnapshot) -> WebToExcelResult:
            raise RuntimeError("background render failed")

    worker = incremental_publication.CoalescingPublicationWorker(
        Publisher(),  # type: ignore[arg-type]
        task_count=600,
    )
    for _ in range(worker.batch_size):
        worker.submit(WebsiteRunSnapshot((), (), frozenset()))

    with pytest.raises(RuntimeError, match="background render failed"):
        worker.flush()
    worker.close(suppress_error=True)


def test_processing_pair_lock_excludes_an_independent_process(
    tmp_path: Path,
) -> None:
    """Break caught: two app instances interleave quote/report pair replacement."""
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe(duplex=False)
    release_child, release_parent = context.Pipe(duplex=False)
    process = context.Process(
        target=_hold_incremental_pair_lock,
        args=(str(tmp_path), ready_child, release_child),
    )
    process.start()
    try:
        assert ready_parent.recv() is True
        with pytest.raises(BlockingIOError):
            with incremental_publication._pair_process_lock(
                tmp_path,
                QuoteMonth(2026, 8),
                blocking=False,
            ):
                pass
        release_parent.send(True)
        process.join(timeout=5)
        assert process.exitcode == 0
        with incremental_publication._pair_process_lock(
            tmp_path,
            QuoteMonth(2026, 8),
            blocking=False,
        ):
            pass
    finally:
        if process.is_alive():
            release_parent.send(True)
            process.join(timeout=5)
        process.close()


def test_processing_pair_lock_has_a_windows_stdlib_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: importing the Windows package fails because fcntl is mandatory."""
    calls: list[tuple[int, int, int]] = []

    class WindowsLocking:
        LK_LOCK = 1
        LK_NBLCK = 2
        LK_UNLCK = 3

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            calls.append((descriptor, mode, size))

    monkeypatch.setattr(incremental_publication, "_fcntl", None)
    monkeypatch.setattr(
        incremental_publication,
        "_msvcrt",
        WindowsLocking,
    )
    lock_path = tmp_path / "windows.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        incremental_publication._lock_descriptor(
            descriptor,
            blocking=False,
        )
        incremental_publication._unlock_descriptor(descriptor)
    finally:
        os.close(descriptor)

    assert [mode for _descriptor, mode, _size in calls] == [2, 3]
    assert all(size == 1 for _descriptor, _mode, size in calls)

from __future__ import annotations

from pathlib import Path

import pytest

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import MacCapturePolicy
from quote_app.excel.report_writer import RunSummary
from quote_app.services import incremental_publication
from quote_app.services.web_run import WebsiteRunSnapshot
from quote_app.services.web_to_excel import WebToExcelResult


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

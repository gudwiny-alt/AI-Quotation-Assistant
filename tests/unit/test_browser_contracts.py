from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceRectangle,
    EvidenceState,
)
from quote_app.domain.models import QuoteMonth
from quote_app.tasks.models import (
    SCHEMA_VERSION,
    BusinessOutcome,
    InputFingerprint,
    RunRecord,
    RunState,
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteTask,
)


class _NominalTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


def _evidence(
    tmp_path,
    *,
    state: EvidenceState = EvidenceState.NORMAL,
    validation_code: str = "CAPTURE_OK",
    annotations: tuple[EvidenceRectangle, ...] = (),
) -> EvidenceRecord:
    return EvidenceRecord(
        state=state,
        path=tmp_path / "evidence.png",
        sha256="a" * 64,
        pixel_width=1920,
        pixel_height=1080,
        captured_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        validation_code=validation_code,
        annotations=annotations,
    )


def _result(
    tmp_path,
    *,
    outcome: BusinessOutcome = BusinessOutcome.PRICE_FOUND,
    price: Decimal | None = Decimal("4499.50"),
    evidence: EvidenceRecord | None = None,
) -> WebsiteResult:
    return WebsiteResult(
        task_id="task-1",
        state=TaskState.SUCCEEDED,
        outcome=outcome,
        price=price,
        url="https://example.test/product",
        evidence=evidence or _evidence(tmp_path),
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )


def test_business_and_technical_states_are_distinct() -> None:
    assert BusinessOutcome.NO_MODEL.value == "no_model"
    assert TaskState.TECHNICAL_FAILURE.value == "technical_failure"
    assert TaskState.WAITING_FOR_LOGIN.value == "waiting_for_login"


@pytest.mark.parametrize(
    ("outcome", "evidence_state"),
    [
        (BusinessOutcome.NO_MODEL, EvidenceState.NO_MODEL),
        (BusinessOutcome.CAPACITY_UNAVAILABLE, EvidenceState.CAPACITY_UNAVAILABLE),
        (BusinessOutcome.COLOR_UNAVAILABLE, EvidenceState.COLOR_UNAVAILABLE),
    ],
)
def test_legal_no_requires_no_price_and_matching_validated_evidence(
    tmp_path, outcome: BusinessOutcome, evidence_state: EvidenceState
) -> None:
    result = _result(
        tmp_path,
        outcome=outcome,
        price=None,
        evidence=_evidence(tmp_path, state=evidence_state),
    )
    assert result.outcome is outcome

    with pytest.raises(ValueError, match="legal no requires price to be None"):
        _result(
            tmp_path,
            outcome=outcome,
            price=Decimal("0"),
            evidence=_evidence(tmp_path, state=evidence_state),
        )

    with pytest.raises(ValueError, match="evidence state does not match outcome"):
        _result(
            tmp_path,
            outcome=outcome,
            price=None,
            evidence=_evidence(tmp_path, state=EvidenceState.NORMAL),
        )


def test_legal_no_requires_url_and_validated_evidence(tmp_path) -> None:
    with pytest.raises(ValueError, match="business success requires validated evidence"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.SUCCEEDED,
            outcome=BusinessOutcome.NO_MODEL,
            price=None,
            url="https://example.test/search",
            evidence=None,
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )

    with pytest.raises(ValueError, match="business success requires a URL"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.SUCCEEDED,
            outcome=BusinessOutcome.NO_MODEL,
            price=None,
            url=None,
            evidence=_evidence(tmp_path, state=EvidenceState.NO_MODEL),
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )

    with pytest.raises(ValueError, match="business success requires validated evidence"):
        _result(
            tmp_path,
            outcome=BusinessOutcome.NO_MODEL,
            price=None,
            evidence=_evidence(
                tmp_path,
                state=EvidenceState.NO_MODEL,
                validation_code="CAPTURE_BLANK",
            ),
        )


@pytest.mark.parametrize("price", [None, Decimal("-0.01"), Decimal("NaN")])
def test_price_found_requires_a_finite_non_negative_price(tmp_path, price) -> None:
    with pytest.raises(ValueError, match="PRICE_FOUND requires a non-negative price"):
        _result(tmp_path, price=price)


def test_price_found_requires_normal_validated_evidence(tmp_path) -> None:
    result = _result(tmp_path)
    assert result.price == Decimal("4499.50")

    with pytest.raises(ValueError, match="evidence state does not match outcome"):
        _result(
            tmp_path,
            evidence=_evidence(tmp_path, state=EvidenceState.NO_MODEL),
        )


def test_sold_out_requires_stock_status_annotation(tmp_path) -> None:
    with pytest.raises(ValueError, match="SOLD_OUT evidence requires stock_status annotation"):
        _result(
            tmp_path,
            outcome=BusinessOutcome.SOLD_OUT,
            price=None,
            evidence=_evidence(tmp_path, state=EvidenceState.SOLD_OUT),
        )

    result = _result(
        tmp_path,
        outcome=BusinessOutcome.SOLD_OUT,
        price=None,
        evidence=_evidence(
            tmp_path,
            state=EvidenceState.SOLD_OUT,
            annotations=(EvidenceRectangle("stock_status", 100, 200, 300, 80),),
        ),
    )
    assert result.outcome is BusinessOutcome.SOLD_OUT


def test_technical_failure_requires_diagnostics_and_has_no_formal_evidence(tmp_path) -> None:
    result = WebsiteResult(
        task_id="task-1",
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url="https://example.test/product",
        evidence=None,
        diagnostic_path=tmp_path / "diagnostic.png",
        error_code="NETWORK_TIMEOUT",
        error_message="page did not load",
    )
    assert result.diagnostic_path == tmp_path / "diagnostic.png"

    with pytest.raises(ValueError, match="technical failure requires error code and message"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.TECHNICAL_FAILURE,
            outcome=None,
            price=None,
            url=None,
            evidence=None,
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )

    with pytest.raises(ValueError, match="technical failure cannot carry formal evidence"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.TECHNICAL_FAILURE,
            outcome=None,
            price=None,
            url=None,
            evidence=_evidence(tmp_path),
            diagnostic_path=None,
            error_code="CAPTURE_BLANK",
            error_message="blank image",
        )


@pytest.mark.parametrize(
    "state",
    [
        TaskState.PENDING,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_LOGIN,
        TaskState.PAUSED,
    ],
)
def test_incomplete_task_states_are_not_completed_results(tmp_path, state: TaskState) -> None:
    with pytest.raises(ValueError, match="WebsiteResult state must be completed"):
        WebsiteResult(
            task_id="task-1",
            state=state,
            outcome=None,
            price=None,
            url=None,
            evidence=None,
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )


def test_evidence_record_rejects_invalid_dimensions_and_rectangles(tmp_path) -> None:
    with pytest.raises(ValueError, match="pixel dimensions must be positive"):
        EvidenceRecord(
            state=EvidenceState.NORMAL,
            path=tmp_path / "bad.png",
            sha256="a" * 64,
            pixel_width=0,
            pixel_height=1080,
            captured_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
            validation_code="CAPTURE_OK",
            annotations=(),
        )

    with pytest.raises(ValueError, match="rectangle dimensions must be positive"):
        EvidenceRectangle("capacity", 0, 0, 0, 10)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("x", True),
        ("y", 1.5),
        ("width", False),
        ("height", 10.5),
    ],
)
def test_evidence_rectangle_requires_exact_integer_coordinates(
    field: str, value: object
) -> None:
    values = {"role": "capacity", "x": 0, "y": 0, "width": 10, "height": 10}
    values[field] = value
    with pytest.raises(ValueError, match="rectangle coordinates and dimensions must be integers"):
        EvidenceRectangle(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pixel_width", True),
        ("pixel_height", 1080.5),
    ],
)
def test_evidence_record_requires_exact_integer_pixels(
    tmp_path: Path, field: str, value: object
) -> None:
    values = {
        "state": EvidenceState.NORMAL,
        "path": tmp_path / "evidence.png",
        "sha256": "a" * 64,
        "pixel_width": 1920,
        "pixel_height": 1080,
        "captured_at": datetime(2026, 7, 25, tzinfo=timezone.utc),
        "validation_code": "CAPTURE_OK",
        "annotations": (),
    }
    values[field] = value
    with pytest.raises(ValueError, match="pixel dimensions must be integers"):
        EvidenceRecord(**values)


def test_evidence_record_rejects_wrong_enum_and_nominal_timezone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="state must be an EvidenceState"):
        EvidenceRecord(
            state="normal",
            path=tmp_path / "evidence.png",
            sha256="a" * 64,
            pixel_width=1920,
            pixel_height=1080,
            captured_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
            validation_code="CAPTURE_OK",
            annotations=(),
        )

    with pytest.raises(ValueError, match="captured_at must be timezone-aware"):
        EvidenceRecord(
            state=EvidenceState.NORMAL,
            path=tmp_path / "evidence.png",
            sha256="a" * 64,
            pixel_width=1920,
            pixel_height=1080,
            captured_at=datetime(2026, 7, 25, tzinfo=_NominalTimezone()),
            validation_code="CAPTURE_OK",
            annotations=(),
        )


def _fingerprints(tmp_path: Path) -> tuple[InputFingerprint, ...]:
    return tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=100,
            modified_ns=1_000,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )


def _run(tmp_path: Path) -> RunRecord:
    timestamp = datetime(2026, 7, 25, tzinfo=timezone.utc)
    return RunRecord(
        run_id="run-1",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=_fingerprints(tmp_path),
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot='{"rows":[]}',
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=timestamp,
        updated_at=timestamp,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("byte_size", True),
        ("byte_size", 10.5),
        ("modified_ns", False),
        ("modified_ns", 12.25),
    ],
)
def test_input_fingerprint_requires_exact_integer_metadata(
    tmp_path: Path, field: str, value: object
) -> None:
    values = {
        "source_role": "base",
        "path": tmp_path / "base.xlsx",
        "sha256": "a" * 64,
        "byte_size": 100,
        "modified_ns": 1_000,
    }
    values[field] = value
    with pytest.raises(ValueError, match="fingerprint size and mtime must be integers"):
        InputFingerprint(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_row_number", True),
        ("source_row_number", 7.5),
        ("output_row_number", False),
        ("output_row_number", 2.5),
    ],
)
def test_website_task_requires_exact_integer_rows(
    field: str, value: object
) -> None:
    values = {
        "task_id": "task-1",
        "run_id": "run-1",
        "source_row_number": 7,
        "output_row_number": 2,
        "material_code": "000123",
        "brand": "小米",
        "model_name": "Xiaomi 15",
        "ram": "12GB",
        "storage": "256GB",
        "color": "黑色",
        "channel": WebsiteChannel.JD,
    }
    values[field] = value
    with pytest.raises(ValueError, match="task row numbers must be integers"):
        WebsiteTask(**values)


def test_contract_enums_do_not_silently_accept_strings(
    tmp_path: Path,
) -> None:
    task = WebsiteTask(
        task_id="task-1",
        run_id="run-1",
        source_row_number=7,
        output_row_number=2,
        material_code="000123",
        brand="小米",
        model_name="Xiaomi 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.JD,
    )
    with pytest.raises(ValueError, match="channel must be a WebsiteChannel"):
        replace(task, channel="jd")
    with pytest.raises(ValueError, match="state must be a RunState"):
        replace(_run(tmp_path), state="created")
    with pytest.raises(ValueError, match="state must be a TaskState"):
        WebsiteResult(
            task_id="task-1",
            state="succeeded",
            outcome=BusinessOutcome.PRICE_FOUND,
            price=Decimal("1"),
            url="https://example.test",
            evidence=_evidence(tmp_path),
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_run_schema_version_requires_exact_integer(
    tmp_path: Path, schema_version: object
) -> None:
    with pytest.raises(ValueError, match="schema_version must be an integer"):
        replace(_run(tmp_path), schema_version=schema_version)


def test_run_rejects_nominal_timezone_and_invalid_quote_month_type(
    tmp_path: Path,
) -> None:
    nominal = datetime(2026, 7, 25, tzinfo=_NominalTimezone())
    with pytest.raises(ValueError, match="run timestamps must be timezone-aware"):
        replace(_run(tmp_path), created_at=nominal, updated_at=nominal)
    with pytest.raises(ValueError, match="quote_month must be a QuoteMonth"):
        replace(_run(tmp_path), quote_month={"year": 2026, "month": 8})


def test_result_price_requires_decimal_and_enum_outcome(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price must be a Decimal"):
        _result(tmp_path, price=4499.5)
    with pytest.raises(ValueError, match="outcome must be a BusinessOutcome"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.SUCCEEDED,
            outcome="price_found",
            price=Decimal("1"),
            url="https://example.test",
            evidence=_evidence(tmp_path),
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test/product",
        "https://example.test/product?token=secret",
        "https://example.test/product?access_token=secret",
        "https://example.test/product?password=secret",
        "https://example.test/access_token/secret/product",
        "https://example.test/product#access_token/secret",
    ],
)
def test_result_rejects_credentials_in_url(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="URL must not contain credentials"):
        replace(_result(tmp_path), url=url)


def test_result_allows_ordinary_security_words_in_url(tmp_path: Path) -> None:
    result = replace(
        _result(tmp_path),
        url=(
            "https://example.test/products/access_token-case"
            "?search=token-expired#authorization-help"
        ),
    )
    assert "access_token-case" in result.url


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer abc123",
        "Cookie: session=abc123",
        "cookie=session=abc123",
        "password=secret",
        "token: abc123",
    ],
)
def test_technical_failure_rejects_explicit_credentials_in_diagnostics(
    tmp_path: Path, message: str
) -> None:
    with pytest.raises(ValueError, match="diagnostic text must not contain credentials"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.TECHNICAL_FAILURE,
            outcome=None,
            price=None,
            url=None,
            evidence=None,
            diagnostic_path=tmp_path / "diagnostic.png",
            error_code="NETWORK",
            error_message=message,
        )


@pytest.mark.parametrize(
    "message",
    [
        "token expired before login",
        "cookie banner blocked the product selector",
        "authorization page was unavailable",
        "password field was not present",
    ],
)
def test_technical_failure_allows_ordinary_diagnostic_words(
    tmp_path: Path, message: str
) -> None:
    result = WebsiteResult(
        task_id="task-1",
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url=None,
        evidence=None,
        diagnostic_path=tmp_path / "diagnostic.png",
        error_code="  LOGIN_REQUIRED  ",
        error_message=f"  {message}  ",
    )
    assert result.error_code == "LOGIN_REQUIRED"
    assert result.error_message == message


def test_technical_failure_strips_before_blank_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="technical failure requires error code and message"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.TECHNICAL_FAILURE,
            outcome=None,
            price=None,
            url=None,
            evidence=None,
            diagnostic_path=tmp_path / "diagnostic.png",
            error_code="   ",
            error_message="network failed",
        )

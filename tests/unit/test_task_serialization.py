from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import EvidenceRecord, EvidenceRectangle, EvidenceState
from quote_app.tasks.models import (
    SCHEMA_VERSION,
    BusinessOutcome,
    InputFingerprint,
    RunRecord,
    RunState,
    TaskState,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.serialization import PayloadError, from_payload, to_payload


@pytest.fixture
def sample_evidence(tmp_path: Path) -> EvidenceRecord:
    return EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=tmp_path / "evidence.png",
        sha256="1" * 64,
        pixel_width=2560,
        pixel_height=1600,
        captured_at=datetime(2026, 7, 25, 9, 30, tzinfo=timezone.utc),
        validation_code="CAPTURE_OK",
        annotations=(EvidenceRectangle("price", 100, 120, 320, 80),),
    )


@pytest.fixture
def sample_result(tmp_path: Path, sample_evidence: EvidenceRecord) -> WebsiteResult:
    return WebsiteResult(
        task_id="task-1",
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("4499.50"),
        url="https://example.test/product",
        evidence=sample_evidence,
        diagnostic_path=tmp_path / "diagnostic.png",
        error_code=None,
        error_message=None,
    )


def test_versioned_payload_round_trips_paths_decimal_and_enums(sample_result) -> None:
    payload = to_payload(sample_result)
    loaded = from_payload(payload)

    assert loaded == sample_result
    assert loaded.price == Decimal("4499.50")
    assert isinstance(loaded.evidence.path, Path)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["data"]["price"] == "4499.50"
    assert payload["data"]["state"] == "succeeded"


def test_observation_checkpoint_round_trips_without_formal_evidence() -> None:
    observed_at = datetime(2026, 7, 30, 0, 15, tzinfo=timezone.utc)
    checkpoint = WebsiteObservationCheckpoint(
        task_id="task-honor-official",
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("4999"),
        url="https://www.honor.com/cn/shop/product/10086252969809.html",
        observed_at=observed_at,
    )

    assert from_payload(to_payload(checkpoint)) == checkpoint


@pytest.mark.parametrize(
    "price",
    [None, Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01")],
)
def test_observation_checkpoint_price_found_requires_a_finite_non_negative_price(
    price: Decimal | None,
) -> None:
    with pytest.raises(ValueError, match="PRICE_FOUND requires a non-negative price"):
        WebsiteObservationCheckpoint(
            task_id="task-honor-official",
            outcome=BusinessOutcome.PRICE_FOUND,
            price=price,
            url="https://www.honor.com/cn/shop/product/10086252969809.html",
            observed_at=datetime(2026, 7, 30, 0, 15, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "outcome",
    [
        BusinessOutcome.NO_MODEL,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        BusinessOutcome.COLOR_UNAVAILABLE,
        BusinessOutcome.SOLD_OUT,
    ],
)
def test_observation_checkpoint_legal_no_forbids_a_price(
    outcome: BusinessOutcome,
) -> None:
    with pytest.raises(ValueError, match="legal no requires price to be None"):
        WebsiteObservationCheckpoint(
            task_id="task-honor-official",
            outcome=outcome,
            price=Decimal("4999"),
            url="https://www.honor.com/cn/shop/product/10086252969809.html",
            observed_at=datetime(2026, 7, 30, 0, 15, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://www.honor.com/product",
        "https://user:password@www.honor.com/product",
        "https://www.honor.com/product?access_token=secret",
    ],
)
def test_observation_checkpoint_url_must_be_credential_free_http(
    url: str,
) -> None:
    with pytest.raises(ValueError):
        WebsiteObservationCheckpoint(
            task_id="task-honor-official",
            outcome=BusinessOutcome.NO_MODEL,
            price=None,
            url=url,
            observed_at=datetime(2026, 7, 30, 0, 15, tzinfo=timezone.utc),
        )


def test_observation_checkpoint_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        WebsiteObservationCheckpoint(
            task_id="task-honor-official",
            outcome=BusinessOutcome.NO_MODEL,
            price=None,
            url="https://www.honor.com/product",
            observed_at=datetime(2026, 7, 30, 0, 15),
        )


def test_all_contract_types_round_trip(tmp_path: Path) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=100 + index,
            modified_ns=1_000 + index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    records = (
        fingerprints[0],
        RunRecord(
            run_id="run-1",
            schema_version=SCHEMA_VERSION,
            quote_month=QuoteMonth(2026, 8),
            input_fingerprints=fingerprints,
            output_dir=tmp_path / "output",
            browser_profile_dir=tmp_path / "profile",
            associated_rows_snapshot='{"rows":[]}',
            associated_rows_snapshot_path=None,
            associated_rows_snapshot_sha256="a" * 64,
            quote_path=tmp_path / "output" / "quote.xlsx",
            report_path=None,
            state=RunState.CREATED,
            created_at=datetime(2026, 7, 25, 9, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 25, 9, tzinfo=timezone.utc),
        ),
        WebsiteTask(
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
        ),
    )

    for record in records:
        assert from_payload(to_payload(record)) == record


def test_snapshot_path_variant_round_trips(tmp_path: Path) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=index,
            modified_ns=index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    run = RunRecord(
        run_id="run-path",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot=None,
        associated_rows_snapshot_path=tmp_path / "rows.json",
        associated_rows_snapshot_sha256="b" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.RUNNING,
        created_at=datetime(2026, 7, 25, 9, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 25, 10, tzinfo=timezone.utc),
    )

    loaded = from_payload(to_payload(run))
    assert loaded == run
    assert isinstance(loaded.associated_rows_snapshot_path, Path)


def test_unknown_future_schema_version_is_rejected(sample_result) -> None:
    payload = to_payload(sample_result)
    payload["schema_version"] = SCHEMA_VERSION + 1

    with pytest.raises(PayloadError, match="unsupported schema_version: 2"):
        from_payload(payload)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_payload_schema_version_requires_an_exact_integer(
    sample_result, schema_version: object
) -> None:
    payload = to_payload(sample_result)
    payload["schema_version"] = schema_version

    with pytest.raises(PayloadError, match="schema_version must be an integer"):
        from_payload(payload)


@pytest.mark.parametrize(
    "field",
    ["password", "cookie", "cookie_value", "authorization", "authorization_header", "token"],
)
def test_credential_fields_are_rejected(sample_result, field: str) -> None:
    payload = to_payload(sample_result)
    payload["data"][field] = "secret"

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        from_payload(payload)


def test_serialized_payload_has_no_credential_fields(sample_result) -> None:
    encoded = json.dumps(to_payload(sample_result), sort_keys=True).lower()
    for forbidden in (
        '"password"',
        '"cookie"',
        '"cookie_value"',
        '"authorization"',
        '"authorization_header"',
        '"token"',
    ):
        assert forbidden not in encoded


def test_malformed_payload_is_rejected_with_stable_error() -> None:
    with pytest.raises(PayloadError, match="malformed payload"):
        from_payload({"schema_version": SCHEMA_VERSION, "payload_type": "WebsiteTask"})


def test_serializer_rejects_unsupported_objects() -> None:
    with pytest.raises(TypeError, match="unsupported payload type"):
        to_payload({"not": "a contract"})


@pytest.mark.parametrize(
    "snapshot",
    [
        '{"rows":[{"password":"secret"}]}',
        '{"rows":[{"cookie_value":"session=abc"}]}',
        '{"rows":[{"authorization_header":"Bearer abc"}]}',
        '{"rows":[{"access_token":"abc"}]}',
        '{"rows":[{"notes":"Authorization: Bearer abc"}]}',
        '{"rows":[{"notes":"cookie=session=abc"}]}',
        '{"rows":[{"notes":"password=secret"}]}',
        '{"rows":[{"notes":"token: abc"}]}',
    ],
)
def test_to_payload_rejects_credentials_embedded_in_snapshot_json(
    tmp_path: Path, snapshot: str
) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=index,
            modified_ns=index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    run = RunRecord(
        run_id="run-sensitive",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot=snapshot,
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        to_payload(run)


def test_from_payload_rejects_credentials_embedded_in_snapshot_json(
    tmp_path: Path,
) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=index,
            modified_ns=index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    run = RunRecord(
        run_id="run-sensitive",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot='{"rows":[]}',
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    payload = to_payload(run)
    payload["data"]["associated_rows_snapshot"] = (
        '{"rows":[{"nested":{"refresh_token":"secret"}}]}'
    )

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        from_payload(payload)


def test_snapshot_and_free_text_with_ordinary_security_words_are_allowed(
    tmp_path: Path,
) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=index,
            modified_ns=index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    run = RunRecord(
        run_id="run-safe",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot=(
            '{"rows":[{"notes":"token expired; cookie banner; '
            'password field absent; authorization page unavailable"}]}'
        ),
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert from_payload(to_payload(run)) == run


def test_from_payload_rejects_sensitive_result_url(sample_result) -> None:
    payload = to_payload(sample_result)
    payload["data"]["url"] = "https://example.test/product?authorization=secret"

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        from_payload(payload)


@pytest.mark.parametrize(
    "snapshot",
    [
        '{"password":"secret"',
        'prefix {"password":"secret"}',
    ],
)
def test_to_payload_rejects_malformed_snapshot_json(
    tmp_path: Path, snapshot: str
) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=index,
            modified_ns=index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    run = RunRecord(
        run_id="run-malformed",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot=snapshot,
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )

    with pytest.raises(PayloadError, match="malformed payload"):
        to_payload(run)


@pytest.mark.parametrize(
    "snapshot",
    [
        '{"password":"secret"',
        'prefix {"password":"secret"}',
    ],
)
def test_from_payload_rejects_malformed_snapshot_json(
    tmp_path: Path, snapshot: str
) -> None:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=index,
            modified_ns=index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    run = RunRecord(
        run_id="run-valid",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot='{"rows":[]}',
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    payload = to_payload(run)
    payload["data"]["associated_rows_snapshot"] = snapshot

    with pytest.raises(PayloadError, match="malformed payload"):
        from_payload(payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/access_token/secret/product",
        "https://example.test/product#refresh_token/secret",
    ],
)
def test_serializer_rejects_url_credentials_in_path_or_fragment(
    sample_result, url: str
) -> None:
    payload = to_payload(sample_result)
    payload["data"]["url"] = url

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        from_payload(payload)

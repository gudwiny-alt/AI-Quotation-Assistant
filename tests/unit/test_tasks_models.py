from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quote_app.evidence.models import EvidenceRecord, EvidenceState
from quote_app.tasks.models import BusinessOutcome, TaskState, WebsiteResult
from quote_app.tasks.serialization import from_payload, to_payload


def test_known_mac_visual_review_result_round_trips_without_becoming_default_validated(
    tmp_path: Path,
) -> None:
    """Catches reintroducing the old model-level rejection of the known beta code."""
    evidence = EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=tmp_path / "formal.png",
        sha256="a" * 64,
        pixel_width=800,
        pixel_height=600,
        captured_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        validation_code="CAPTURE_OK_MAC_VISUAL_REVIEW",
    )

    result = WebsiteResult(
        task_id="mac-beta",
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("4499"),
        url="https://example.test/product",
        evidence=evidence,
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )

    loaded = from_payload(to_payload(result))

    assert loaded == result
    assert loaded.evidence is not None
    assert loaded.evidence.validation_code == "CAPTURE_OK_MAC_VISUAL_REVIEW"
    assert not loaded.evidence.is_validated

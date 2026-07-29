from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceState,
    MacCapturePolicy,
    accepts_capture_validation,
)


def _record(validation_code: str) -> EvidenceRecord:
    return EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=Path("/tmp/evidence.png"),
        sha256="0" * 64,
        pixel_width=1,
        pixel_height=1,
        captured_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        validation_code=validation_code,
    )


@pytest.mark.parametrize(
    ("validation_code", "is_validated"),
    [
        ("CAPTURE_OK", True),
        ("CAPTURE_OK_MAC_VISUAL_REVIEW", False),
        ("CAPTURE_FAILED", False),
    ],
)
def test_record_validation_remains_strict(validation_code: str, is_validated: bool) -> None:
    assert _record(validation_code).is_validated is is_validated


@pytest.mark.parametrize(
    ("policy", "platform_name", "validation_code", "accepted"),
    [
        (MacCapturePolicy.STRICT, "Darwin", "CAPTURE_OK", True),
        (MacCapturePolicy.STRICT, "Darwin", "CAPTURE_OK_MAC_VISUAL_REVIEW", False),
        (MacCapturePolicy.MAC_VISUAL_REVIEW_BETA, "Darwin", "CAPTURE_OK", True),
        (
            MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
            "Darwin",
            "CAPTURE_OK_MAC_VISUAL_REVIEW",
            True,
        ),
        (
            MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
            "Windows",
            "CAPTURE_OK_MAC_VISUAL_REVIEW",
            False,
        ),
        (
            MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
            "Darwin",
            "CAPTURE_FAILED",
            False,
        ),
    ],
)
def test_explicit_policy_accepts_only_the_macos_visual_review_code(
    policy: MacCapturePolicy,
    platform_name: str,
    validation_code: str,
    accepted: bool,
) -> None:
    assert (
        accepts_capture_validation(
            validation_code,
            policy=policy,
            platform_name=platform_name,
        )
        is accepted
    )

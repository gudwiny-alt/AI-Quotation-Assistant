from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_MODULE = ROOT / "src/quote_app/sites/official.py"
HONOR_OVERRIDE_MODULE = ROOT / "src/quote_app/sites/official_overrides/honor.py"
OFFICIAL_SHA256 = "30d68f9a9a6c7cc194a428d8607700af3352b09c9277a503244da418072dd6b3"
HONOR_OVERRIDE_SHA256 = "842a7b106c36b93bf4a07df4d14e326308431be706735943211ba9e02113a1df"


def test_honor_official_baseline_excludes_unpublished_enter_fallback() -> None:
    """The acceptance baseline must not contain the unshipped search experiment."""
    payload = OFFICIAL_MODULE.read_text(encoding="utf-8")

    assert "_HONOR_CLICK_RESULT_POLLS" not in payload
    assert "did not render product results after Enter" not in payload


def test_honor_official_modules_match_the_frozen_baseline() -> None:
    """No future website work may silently alter the accepted HONOR module."""
    assert hashlib.sha256(OFFICIAL_MODULE.read_bytes()).hexdigest() == OFFICIAL_SHA256
    assert (
        hashlib.sha256(HONOR_OVERRIDE_MODULE.read_bytes()).hexdigest()
        == HONOR_OVERRIDE_SHA256
    )

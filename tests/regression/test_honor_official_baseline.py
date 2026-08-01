from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_MODULE = ROOT / "src/quote_app/sites/official.py"
HONOR_OVERRIDE_MODULE = ROOT / "src/quote_app/sites/official_overrides/honor.py"
HONOR_DETAIL_SUFFIX_SHA256 = "141253dd7627e6cd7f5ca492facc6ae84df17d77e4a1600bebec196c027a6fe6"
HONOR_OVERRIDE_DETAIL_SUFFIX_SHA256 = "2b2c733f27027ea563451d44d72b5b043edbd127dcf50b5101fbb4c577717f4e"


def test_honor_official_entry_uses_the_approved_single_enter_recovery() -> None:
    """Only the homepage entry may recover one unrendered search submission."""
    payload = OFFICIAL_MODULE.read_text(encoding="utf-8")

    assert 'search_input.press("Enter")' in payload
    assert "HONOR_SEARCH_RESULTS_MISSING" in payload


def test_honor_official_modules_keep_the_detail_chain_frozen() -> None:
    """Card matching may evolve; SKU, stock and price validation may not."""
    payload = OFFICIAL_MODULE.read_text(encoding="utf-8")
    _entry, separator, detail_and_beyond = payload.partition(
        "    def _observe_loaded_honor_detail("
    )
    assert separator
    assert (
        hashlib.sha256(detail_and_beyond.encode("utf-8")).hexdigest()
        == HONOR_DETAIL_SUFFIX_SHA256
    )
    override_payload = HONOR_OVERRIDE_MODULE.read_text(encoding="utf-8")
    _matching, separator, detail_and_beyond = override_payload.partition(
        "    @classmethod\n    def select_task_sku("
    )
    assert separator
    assert (
        hashlib.sha256(detail_and_beyond.encode("utf-8")).hexdigest()
        == HONOR_OVERRIDE_DETAIL_SUFFIX_SHA256
    )

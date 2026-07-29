from __future__ import annotations

from decimal import Decimal

import pytest

from quote_app.evidence.geometry import CssRect
from quote_app.evidence import platform
from quote_app.evidence.platform import BrowserWindowIdentity
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites import protocol
from quote_app.tasks.models import BusinessOutcome


class _Probe:
    def semantic_hash(self) -> str:
        return "stable"


_RECTANGLES = {
    BusinessOutcome.PRICE_FOUND: (),
    BusinessOutcome.NO_MODEL: (
        CssRect(1, 2, 3, 4, "search_keyword"),
        CssRect(5, 6, 7, 8, "result_region"),
    ),
    BusinessOutcome.CAPACITY_UNAVAILABLE: (
        CssRect(1, 2, 3, 4, "capacity"),
    ),
    BusinessOutcome.COLOR_UNAVAILABLE: (
        CssRect(1, 2, 3, 4, "color"),
    ),
    BusinessOutcome.SOLD_OUT: (
        CssRect(1, 2, 3, 4, "stock_status"),
    ),
}


def _semantic_state(
    outcome: BusinessOutcome = BusinessOutcome.PRICE_FOUND,
    *,
    price: Decimal | None = Decimal("3999.00"),
    url: str = "https://example.test/product",
    rectangles: tuple[CssRect, ...] = (),
) -> VerifiedSemanticState:
    is_not_applicable = outcome in {
        BusinessOutcome.NO_MODEL,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        BusinessOutcome.COLOR_UNAVAILABLE,
    }
    return VerifiedSemanticState(
        canonical_url=url,
        brand="fixture-brand",
        model_name="fixture-model",
        capacity="12GB+256GB",
        color="fixture-color",
        current_sku=(
            "not-applicable" if is_not_applicable else "fixture-sku"
        ),
        region=(
            "not-applicable" if is_not_applicable else "fixture-region"
        ),
        stock_state=(
            "not-applicable" if is_not_applicable else "fixture-stock"
        ),
        price=price,
        outcome=outcome,
        css_rectangles=rectangles,
    )


@pytest.mark.parametrize("outcome", tuple(BusinessOutcome))
def test_adapter_observation_accepts_only_the_exact_contract_for_each_outcome(
    outcome: BusinessOutcome,
) -> None:
    price = Decimal("3999.00") if outcome is BusinessOutcome.PRICE_FOUND else None
    observation = protocol.AdapterObservation(
        outcome=outcome,
        price=price,
        url=" https://example.test/product ",
        css_rectangles=_RECTANGLES[outcome],
        semantic_state=_semantic_state(
            outcome,
            price=price,
            rectangles=_RECTANGLES[outcome],
        ),
    )

    assert observation.outcome is outcome
    assert observation.url == "https://example.test/product"
    assert observation.css_rectangles == _RECTANGLES[outcome]


@pytest.mark.parametrize(
    ("outcome", "price", "rectangles"),
    [
        (BusinessOutcome.PRICE_FOUND, None, ()),
        (BusinessOutcome.PRICE_FOUND, Decimal("NaN"), ()),
        (BusinessOutcome.PRICE_FOUND, Decimal("-0.01"), ()),
        (BusinessOutcome.PRICE_FOUND, Decimal("1"), _RECTANGLES[BusinessOutcome.CAPACITY_UNAVAILABLE]),
        (BusinessOutcome.NO_MODEL, Decimal("1"), _RECTANGLES[BusinessOutcome.NO_MODEL]),
        (BusinessOutcome.NO_MODEL, None, tuple(reversed(_RECTANGLES[BusinessOutcome.NO_MODEL]))),
        (BusinessOutcome.CAPACITY_UNAVAILABLE, None, ()),
        (BusinessOutcome.COLOR_UNAVAILABLE, None, _RECTANGLES[BusinessOutcome.SOLD_OUT]),
        (BusinessOutcome.SOLD_OUT, None, _RECTANGLES[BusinessOutcome.SOLD_OUT] * 2),
    ],
)
def test_adapter_observation_rejects_invalid_business_or_frame_contracts(
    outcome: BusinessOutcome,
    price: Decimal | None,
    rectangles: tuple[CssRect, ...],
) -> None:
    with pytest.raises(ValueError):
        protocol.AdapterObservation(
            outcome=outcome,
            price=price,
            url="https://example.test/product",
            css_rectangles=rectangles,
            semantic_state=_semantic_state(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.test/product",
        "https://user:password@example.test/product",
        "https://user@example.test/product",
        "https:///missing-host",
    ],
)
def test_adapter_observation_rejects_non_http_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ValueError):
        protocol.AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=Decimal("1"),
            url=url,
            css_rectangles=(),
            semantic_state=_semantic_state(price=Decimal("1")),
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/item?access_token=secret",
        "https://example.test/token=secret/item",
        "https://example.test/item#password=secret",
    ],
)
def test_adapter_observation_rejects_credentials_outside_userinfo(url: str) -> None:
    with pytest.raises(ValueError, match="credential-free"):
        protocol.AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=Decimal("1"),
            url=url,
            css_rectangles=(),
            semantic_state=_semantic_state(price=Decimal("1")),
        )


def test_adapter_observation_keeps_ordinary_query_and_fragment() -> None:
    url = "https://example.test/item?color=black#specifications"

    observation = protocol.AdapterObservation(
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("1"),
        url=url,
        css_rectangles=(),
        semantic_state=_semantic_state(
            price=Decimal("1"),
            url=url,
        ),
    )

    assert observation.url == url


def test_capture_context_requires_real_window_identity_and_hash_probe() -> None:
    context = platform.CaptureContext(
        expected_window=BrowserWindowIdentity("fixture", 1, "window-1"),
        stability_probe=_Probe(),
    )

    assert context.expected_window.window_handle == "window-1"

    with pytest.raises(ValueError):
        platform.CaptureContext(  # type: ignore[arg-type]
            expected_window="window",
            stability_probe=_Probe(),
        )
    with pytest.raises(ValueError):
        platform.CaptureContext(
            expected_window=BrowserWindowIdentity("fixture", 1, "window-1"),
            stability_probe=object(),  # type: ignore[arg-type]
        )

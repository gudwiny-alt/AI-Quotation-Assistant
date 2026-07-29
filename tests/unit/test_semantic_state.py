from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError, wait_for_stable_hash
from quote_app.evidence.semantic_state import (
    VerifiedPageStateProbe,
    VerifiedSemanticState,
)
from quote_app.sites.protocol import AdapterObservation
from quote_app.tasks.models import BusinessOutcome
from quote_app.tasks.models import url_contains_credentials


def _state(**changes: object) -> VerifiedSemanticState:
    values: dict[str, object] = {
        "canonical_url": "https://www.honor.com/cn/shop/product/100.html",
        "brand": "HONOR",
        "model_name": "荣耀Magic8",
        "capacity": "12GB+256GB",
        "color": "绒黑色",
        "current_sku": "10086516771847",
        "region": "福建>福州>台江",
        "stock_state": "现货",
        "price": Decimal("4499.00"),
        "outcome": BusinessOutcome.PRICE_FOUND,
        "css_rectangles": (),
    }
    values.update(changes)
    return VerifiedSemanticState(**values)  # type: ignore[arg-type]


def test_semantic_state_rejects_credential_url() -> None:
    with pytest.raises(ValueError, match="credential"):
        _state(
            canonical_url="https://user:pass@example.com/product",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/product?client_secret=value",
        "https://example.test/product?api_key=value",
        "https://example.test/product?apikey=value",
        "https://example.test/product?session=value",
        "https://example.test/product?session_id=value",
        "https://example.test/product?credential=value",
        "https://example.test/product?CLIENT-SECRET=value",
        "https://example.test/product?api%5Fkey=value",
        "https://example.test/product?client%255Fsecret=value",
        "https://example.test/client-secret/value/product",
        "https://example.test/product#checkout_credential=value",
    ],
)
def test_semantic_state_rejects_common_credential_parameter_forms(
    url: str,
) -> None:
    assert url_contains_credentials(url)
    with pytest.raises(ValueError, match="credential"):
        _state(canonical_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/product?sku=token-case",
        "https://example.test/product?color=secret-red",
        "https://example.test/product?session_style=summer",
        "https://example.test/product?secretary_model=pro",
        "https://example.test/product-token-case?campaign=authorization-help",
    ],
)
def test_semantic_state_allows_normal_product_parameters(url: str) -> None:
    assert not url_contains_credentials(url)
    assert _state(canonical_url=url).canonical_url == url


@pytest.mark.parametrize(
    "field_name",
    ["model_name", "current_sku", "region", "stock_state"],
)
def test_semantic_state_rejects_blank_verified_fields(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        _state(**{field_name: " \t "})


@pytest.mark.parametrize(
    "price",
    [None, Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")],
)
def test_price_found_requires_non_negative_finite_price(
    price: Decimal | None,
) -> None:
    with pytest.raises(ValueError, match="price"):
        _state(price=price)


@pytest.mark.parametrize(
    "outcome",
    [
        BusinessOutcome.NO_MODEL,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        BusinessOutcome.COLOR_UNAVAILABLE,
        BusinessOutcome.SOLD_OUT,
    ],
)
def test_legal_no_requires_absent_price(outcome: BusinessOutcome) -> None:
    with pytest.raises(ValueError, match="price"):
        _state(
            outcome=outcome,
            price=Decimal("0"),
            css_rectangles={
                BusinessOutcome.NO_MODEL: (
                    CssRect(1, 1, 10, 10, "search_keyword"),
                    CssRect(2, 2, 10, 10, "result_region"),
                ),
                BusinessOutcome.CAPACITY_UNAVAILABLE: (
                    CssRect(1, 1, 10, 10, "capacity"),
                ),
                BusinessOutcome.COLOR_UNAVAILABLE: (
                    CssRect(1, 1, 10, 10, "color"),
                ),
                BusinessOutcome.SOLD_OUT: (
                    CssRect(1, 1, 10, 10, "stock_status"),
                ),
            }[outcome],
        )


@pytest.mark.parametrize(
    ("outcome", "rectangles"),
    [
        (
            BusinessOutcome.PRICE_FOUND,
            (CssRect(1, 1, 10, 10, "stock_status"),),
        ),
        (BusinessOutcome.NO_MODEL, ()),
        (
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            (CssRect(1, 1, 10, 10, "color"),),
        ),
        (
            BusinessOutcome.COLOR_UNAVAILABLE,
            (CssRect(1, 1, 10, 10, "capacity"),),
        ),
        (BusinessOutcome.SOLD_OUT, ()),
    ],
)
def test_semantic_state_rejects_rectangle_roles_not_matching_outcome(
    outcome: BusinessOutcome,
    rectangles: tuple[CssRect, ...],
) -> None:
    with pytest.raises(ValueError, match="role"):
        _state(
            outcome=outcome,
            price=(
                Decimal("4499")
                if outcome is BusinessOutcome.PRICE_FOUND
                else None
            ),
            css_rectangles=rectangles,
        )


def test_semantic_state_normalizes_values_and_is_immutable() -> None:
    rectangle = CssRect(1, 2, 3, 4, "stock_status")
    state = _state(
        canonical_url=" https://www.honor.com/product/100 ",
        brand=" HONOR ",
        model_name=" 荣耀Magic8 ",
        capacity=" 12GB+256GB ",
        color=" 绒黑色 ",
        current_sku=" 10086516771847 ",
        region=" 福建>福州>台江 ",
        stock_state=" 售罄 ",
        price=None,
        outcome=BusinessOutcome.SOLD_OUT,
        css_rectangles=[rectangle],
    )

    assert (
        state.canonical_url,
        state.brand,
        state.model_name,
        state.capacity,
        state.color,
        state.current_sku,
        state.region,
        state.stock_state,
        state.css_rectangles,
    ) == (
        "https://www.honor.com/product/100",
        "HONOR",
        "荣耀Magic8",
        "12GB+256GB",
        "绒黑色",
        "10086516771847",
        "福建>福州>台江",
        "售罄",
        (rectangle,),
    )
    with pytest.raises(FrozenInstanceError):
        state.region = "广东>深圳"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("current_sku", "unknown"),
        ("current_sku", "not-applicable"),
        ("region", "not-specified"),
        ("region", "not-evaluated"),
        ("stock_state", "unknown"),
        ("stock_state", "available"),
    ],
)
def test_price_found_rejects_unverified_sentinel_fields(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        _state(**{field_name: value})


def test_no_model_requires_strict_not_applicable_semantics() -> None:
    rectangles = (
        CssRect(1, 1, 10, 10, "search_keyword"),
        CssRect(2, 2, 10, 10, "result_region"),
    )

    state = _state(
        current_sku="not-applicable",
        region="not-applicable",
        stock_state="not-applicable",
        price=None,
        outcome=BusinessOutcome.NO_MODEL,
        css_rectangles=rectangles,
    )

    assert state.current_sku == "not-applicable"
    with pytest.raises(ValueError, match="stock_state"):
        _state(
            current_sku="not-applicable",
            region="not-applicable",
            stock_state="not-evaluated",
            price=None,
            outcome=BusinessOutcome.NO_MODEL,
            css_rectangles=rectangles,
        )


def test_probe_identical_fields_yield_lowercase_sha256() -> None:
    state = _state()

    first = VerifiedPageStateProbe(lambda: state).semantic_hash()
    second = VerifiedPageStateProbe(lambda: replace(state)).semantic_hash()

    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    assert set(first) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("current_sku", "10086516771848"),
        ("region", "广东>深圳>龙岗"),
        ("stock_state", "售罄"),
        ("price", Decimal("4599.00")),
        (
            "canonical_url",
            "https://www.honor.com/cn/shop/product/101.html",
        ),
        ("model_name", "荣耀Magic8 Pro"),
        ("capacity", "16GB+512GB"),
        ("color", "天青釉"),
    ],
)
def test_probe_hash_changes_for_each_verified_business_field(
    field_name: str,
    replacement: object,
) -> None:
    baseline = _state()
    changed = replace(baseline, **{field_name: replacement})

    assert (
        VerifiedPageStateProbe(lambda: baseline).semantic_hash()
        != VerifiedPageStateProbe(lambda: changed).semantic_hash()
    )


def test_probe_has_no_input_for_unrelated_rotating_ad_text() -> None:
    state = _state()
    rotating_ad = ["限时活动 A"]

    def reader() -> VerifiedSemanticState:
        _ = rotating_ad[0]
        return state

    probe = VerifiedPageStateProbe(reader)
    first = probe.semantic_hash()
    rotating_ad[0] = "限时活动 B"

    assert probe.semantic_hash() == first


def test_probe_reader_exception_becomes_capture_unstable() -> None:
    def broken_reader() -> VerifiedSemanticState:
        raise RuntimeError("external page read failed")

    with pytest.raises(CaptureQualityError) as captured:
        wait_for_stable_hash(
            VerifiedPageStateProbe(broken_reader),
            minimum_interval_seconds=0.001,
            sleeper=lambda _seconds: None,
        )

    assert captured.value.code == "CAPTURE_UNSTABLE"


def test_probe_digest_does_not_expose_plaintext_state_or_url() -> None:
    state = _state()

    digest = VerifiedPageStateProbe(lambda: state).semantic_hash()

    for plaintext in (
        state.canonical_url,
        state.brand,
        state.model_name,
        state.capacity,
        state.color,
        state.current_sku,
        state.region,
        state.stock_state,
        str(state.price),
    ):
        assert plaintext not in digest


@pytest.mark.parametrize("mismatch", ["outcome", "price", "url", "rectangles"])
def test_adapter_observation_rejects_state_boundary_mismatch(
    mismatch: str,
) -> None:
    state = _state()
    values: dict[str, object] = {
        "outcome": state.outcome,
        "price": state.price,
        "url": state.canonical_url,
        "css_rectangles": state.css_rectangles,
        "semantic_state": state,
    }
    if mismatch == "outcome":
        values["semantic_state"] = _state(
            outcome=BusinessOutcome.SOLD_OUT,
            price=None,
            stock_state="售罄",
            css_rectangles=(CssRect(1, 1, 10, 10, "stock_status"),),
        )
    elif mismatch == "price":
        values["semantic_state"] = replace(state, price=Decimal("4599"))
    elif mismatch == "url":
        values["semantic_state"] = replace(
            state,
            canonical_url=(
                "https://www.honor.com/cn/shop/product/101.html"
            ),
        )
    else:
        observed_rectangle = CssRect(1, 1, 10, 10, "stock_status")
        values.update(
            outcome=BusinessOutcome.SOLD_OUT,
            price=None,
            css_rectangles=(observed_rectangle,),
            semantic_state=_state(
                outcome=BusinessOutcome.SOLD_OUT,
                price=None,
                stock_state="售罄",
                css_rectangles=(
                    CssRect(2, 1, 10, 10, "stock_status"),
                ),
            ),
        )

    with pytest.raises(ValueError, match="semantic"):
        AdapterObservation(**values)  # type: ignore[arg-type]

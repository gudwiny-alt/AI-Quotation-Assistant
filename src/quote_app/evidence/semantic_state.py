from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.tasks.models import (
    BusinessOutcome,
    url_contains_credentials,
)

_OUTCOME_ROLES = {
    BusinessOutcome.PRICE_FOUND: frozenset({()}),
    BusinessOutcome.NO_MODEL: frozenset({("search_keyword", "result_region")}),
    BusinessOutcome.CAPACITY_UNAVAILABLE: frozenset(
        {("capacity",), ("title", "capacity_group")}
    ),
    BusinessOutcome.COLOR_UNAVAILABLE: frozenset(
        {("color",), ("title", "color_group")}
    ),
    BusinessOutcome.SOLD_OUT: frozenset({("stock_status",)}),
}
_NOT_APPLICABLE = "not-applicable"
_UNVERIFIED_MARKERS = frozenset(
    {
        "unknown",
        "not-evaluated",
        "not-specified",
        _NOT_APPLICABLE,
    }
)


@dataclass(frozen=True, slots=True)
class VerifiedSemanticState:
    canonical_url: str
    brand: str
    model_name: str
    capacity: str
    color: str
    current_sku: str
    region: str
    stock_state: str
    price: Decimal | None
    outcome: BusinessOutcome
    css_rectangles: tuple[CssRect, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "canonical_url",
            "brand",
            "model_name",
            "capacity",
            "color",
            "current_sku",
            "region",
            "stock_state",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value.strip())

        try:
            parsed_url = urlsplit(self.canonical_url)
            valid_url = (
                parsed_url.scheme.lower() in {"http", "https"}
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not url_contains_credentials(self.canonical_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(
                "canonical_url must be credential-free HTTP(S)"
            )
        if not isinstance(self.outcome, BusinessOutcome):
            raise ValueError("outcome must be a BusinessOutcome")
        if self.price is not None and not isinstance(self.price, Decimal):
            raise ValueError("price must be a Decimal")
        if not isinstance(self.css_rectangles, tuple | list) or not all(
            isinstance(rectangle, CssRect)
            for rectangle in self.css_rectangles
        ):
            raise ValueError("css_rectangles must contain CssRect values")

        rectangles = tuple(self.css_rectangles)
        actual_roles = tuple(rectangle.role for rectangle in rectangles)
        if actual_roles not in _OUTCOME_ROLES[self.outcome]:
            raise ValueError(
                "rectangle roles do not match the business outcome"
            )
        if self.outcome is BusinessOutcome.PRICE_FOUND:
            if (
                self.price is None
                or not self.price.is_finite()
                or self.price < 0
            ):
                raise ValueError(
                    "price-found state requires a non-negative finite price"
                )
        elif self.price is not None:
            raise ValueError("legal no state requires price to be None")
        self._validate_applicability()
        object.__setattr__(self, "css_rectangles", rectangles)

    def _validate_applicability(self) -> None:
        verified_fields = (
            ("current_sku", self.current_sku),
            ("region", self.region),
            ("stock_state", self.stock_state),
        )
        if self.outcome in {
            BusinessOutcome.PRICE_FOUND,
            BusinessOutcome.SOLD_OUT,
        }:
            for field_name, value in verified_fields:
                marker = _normalized_marker(value)
                if marker in _UNVERIFIED_MARKERS or (
                    field_name == "stock_state" and marker == "available"
                ):
                    raise ValueError(
                        f"{field_name} must contain an actual verified value"
                    )
            return
        if self.outcome in {
            BusinessOutcome.NO_MODEL,
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            BusinessOutcome.COLOR_UNAVAILABLE,
        }:
            for field_name, value in verified_fields:
                if _normalized_marker(value) != _NOT_APPLICABLE:
                    raise ValueError(
                        f"{field_name} must be not-applicable for "
                        f"{self.outcome.value}"
                    )
            return


SemanticStateReader = Callable[[], VerifiedSemanticState]


class VerifiedPageStateProbe:
    def __init__(self, reader: SemanticStateReader) -> None:
        if not callable(reader):
            raise ValueError("reader must be callable")
        self._reader = reader

    def semantic_hash(self) -> str:
        state = self._reader()
        if not isinstance(state, VerifiedSemanticState):
            raise ValueError(
                "reader must return a VerifiedSemanticState"
            )
        ordered_state = (
            state.canonical_url,
            state.brand,
            state.model_name,
            state.capacity,
            state.color,
            state.current_sku,
            state.region,
            state.stock_state,
            str(state.price) if state.price is not None else None,
            state.outcome.value,
            tuple(
                (
                    rectangle.x,
                    rectangle.y,
                    rectangle.width,
                    rectangle.height,
                    rectangle.role,
                )
                for rectangle in state.css_rectangles
            ),
        )
        return hashlib.sha256(
            json.dumps(
                ordered_state,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()


def _normalized_marker(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")

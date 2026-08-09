from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.tasks.models import BusinessOutcome, url_contains_credentials

_LEGAL_NO_ROLES = {
    BusinessOutcome.NO_MODEL: ("search_keyword", "result_region"),
    BusinessOutcome.CAPACITY_UNAVAILABLE: ("capacity",),
    BusinessOutcome.COLOR_UNAVAILABLE: ("color",),
}


@dataclass(frozen=True, slots=True)
class ApprovedHostFamily:
    root_domain: str
    allow_subdomains: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.root_domain, str):
            raise ValueError("root_domain must be a string")
        normalized = self.root_domain.strip().lower().rstrip(".")
        if not normalized or "." not in normalized:
            raise ValueError("root_domain must be a valid domain")
        if type(self.allow_subdomains) is not bool:
            raise ValueError("allow_subdomains must be a boolean")
        object.__setattr__(self, "root_domain", normalized)

    def contains(self, hostname: str) -> bool:
        normalized = hostname.strip().lower().rstrip(".")
        return normalized == self.root_domain or (
            self.allow_subdomains
            and normalized.endswith(f".{self.root_domain}")
        )


class OfficialManualAction(StrEnum):
    LOGIN = "login"
    SECURITY_VERIFICATION = "security_verification"


@dataclass(frozen=True, slots=True)
class OfficialDetailIdentity:
    canonical_url: str
    product_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_url",
            _validated_http_url(self.canonical_url),
        )
        if not isinstance(self.product_key, str) or not self.product_key.strip():
            raise ValueError("product_key must not be blank")
        object.__setattr__(self, "product_key", self.product_key.strip())


@dataclass(frozen=True, slots=True)
class OfficialOfferSnapshot:
    """Stable live-official state; inventory and region are intentionally absent."""

    identity: OfficialDetailIdentity
    brand: str
    model_name: str
    capacity: str
    color: str
    price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.identity, OfficialDetailIdentity):
            raise ValueError("identity must be an OfficialDetailIdentity")
        for field_name in ("brand", "model_name", "capacity", "color"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value.strip())
        if (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or self.price < 0
        ):
            raise ValueError("price must be a non-negative finite Decimal")


@dataclass(frozen=True, slots=True)
class OfficialCaptureView:
    css_rectangles: tuple[CssRect, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.css_rectangles, tuple | list) or not all(
            isinstance(rectangle, CssRect)
            for rectangle in self.css_rectangles
        ):
            raise ValueError("css_rectangles must contain CssRect values")
        object.__setattr__(self, "css_rectangles", tuple(self.css_rectangles))


@dataclass(frozen=True, slots=True)
class OfficialBusinessState:
    canonical_url: str
    brand: str
    model_name: str
    capacity: str
    color: str
    detail_identity: str | None
    price: Decimal | None
    outcome: BusinessOutcome
    capture_view: OfficialCaptureView

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_url",
            _validated_http_url(self.canonical_url),
        )
        for field_name in ("brand", "model_name", "capacity", "color"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value.strip())
        if self.detail_identity is not None:
            if (
                not isinstance(self.detail_identity, str)
                or not self.detail_identity.strip()
            ):
                raise ValueError("detail_identity must be nonblank when present")
            object.__setattr__(
                self,
                "detail_identity",
                self.detail_identity.strip(),
            )
        if not isinstance(self.outcome, BusinessOutcome):
            raise ValueError("outcome must be a BusinessOutcome")
        if not isinstance(self.capture_view, OfficialCaptureView):
            raise ValueError("capture_view must be an OfficialCaptureView")
        if self.outcome is BusinessOutcome.PRICE_FOUND:
            if self.detail_identity is None:
                raise ValueError("price-found state requires a detail identity")
            if (
                not isinstance(self.price, Decimal)
                or not self.price.is_finite()
                or self.price < 0
            ):
                raise ValueError("price-found state requires a valid price")
            if self.capture_view.css_rectangles:
                raise ValueError("price-found state does not use CSS rectangles")
            return
        if self.outcome not in {
            BusinessOutcome.NO_MODEL,
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            BusinessOutcome.COLOR_UNAVAILABLE,
        }:
            raise ValueError("unsupported live official business outcome")
        if self.price is not None:
            raise ValueError("legal no state cannot carry a price")
        actual_roles = tuple(
            rectangle.role for rectangle in self.capture_view.css_rectangles
        )
        if actual_roles != _LEGAL_NO_ROLES[self.outcome]:
            raise ValueError("rectangle roles do not match the business outcome")
        if self.outcome is BusinessOutcome.NO_MODEL:
            if self.detail_identity is not None:
                raise ValueError("no-model state cannot carry a detail identity")
        elif self.detail_identity is None:
            raise ValueError("configuration-unavailable state requires detail identity")

    @classmethod
    def price_found(
        cls,
        *,
        identity: OfficialDetailIdentity,
        brand: str,
        model_name: str,
        capacity: str,
        color: str,
        price: Decimal,
    ) -> OfficialBusinessState:
        return cls(
            canonical_url=identity.canonical_url,
            brand=brand,
            model_name=model_name,
            capacity=capacity,
            color=color,
            detail_identity=identity.product_key,
            price=price,
            outcome=BusinessOutcome.PRICE_FOUND,
            capture_view=OfficialCaptureView(()),
        )

    @classmethod
    def legal_no(
        cls,
        *,
        canonical_url: str,
        brand: str,
        model_name: str,
        capacity: str,
        color: str,
        outcome: BusinessOutcome,
        capture_view: OfficialCaptureView,
        detail_identity: str | None,
    ) -> OfficialBusinessState:
        return cls(
            canonical_url=canonical_url,
            brand=brand,
            model_name=model_name,
            capacity=capacity,
            color=color,
            detail_identity=detail_identity,
            price=None,
            outcome=outcome,
            capture_view=capture_view,
        )

    def offer_snapshot(self) -> OfficialOfferSnapshot:
        if self.outcome is not BusinessOutcome.PRICE_FOUND:
            raise ValueError("only price-found state has an offer snapshot")
        if self.detail_identity is None or self.price is None:
            raise AssertionError("validated price-found state must be complete")
        return OfficialOfferSnapshot(
            identity=OfficialDetailIdentity(
                self.canonical_url,
                self.detail_identity,
            ),
            brand=self.brand,
            model_name=self.model_name,
            capacity=self.capacity,
            color=self.color,
            price=self.price,
        )


def _validated_http_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL must not be blank")
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        valid = (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not url_contains_credentials(normalized)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("URL must be credential-free HTTP(S)")
    return normalized

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
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


@dataclass(frozen=True, slots=True)
class AdapterObservation:
    """Business and DOM observations before runner-owned evidence capture."""

    outcome: BusinessOutcome
    price: Decimal | None
    url: str
    css_rectangles: tuple[CssRect, ...]
    semantic_state: VerifiedSemanticState

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, BusinessOutcome):
            raise ValueError("outcome must be a BusinessOutcome")
        if self.price is not None and not isinstance(self.price, Decimal):
            raise ValueError("price must be a Decimal")
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must not be blank")
        if not isinstance(self.css_rectangles, tuple | list) or not all(
            isinstance(rectangle, CssRect)
            for rectangle in self.css_rectangles
        ):
            raise ValueError("css_rectangles must contain CssRect values")
        if not isinstance(self.semantic_state, VerifiedSemanticState):
            raise ValueError(
                "semantic_state must be a VerifiedSemanticState"
            )

        normalized_url = self.url.strip()
        try:
            parsed_url = urlsplit(normalized_url)
            valid_url = (
                parsed_url.scheme.lower() in {"http", "https"}
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not url_contains_credentials(normalized_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("url must be credential-free HTTP(S)")

        rectangles = tuple(self.css_rectangles)
        actual_roles = tuple(rectangle.role for rectangle in rectangles)
        if actual_roles not in _OUTCOME_ROLES[self.outcome]:
            raise ValueError("observation roles do not match the business outcome")
        if self.outcome is BusinessOutcome.PRICE_FOUND:
            if (
                self.price is None
                or not self.price.is_finite()
                or self.price < 0
            ):
                raise ValueError("price-found observation requires a price")
        elif self.price is not None:
            raise ValueError("legal no observation cannot carry a price")

        object.__setattr__(self, "url", normalized_url)
        object.__setattr__(self, "css_rectangles", rectangles)
        if (
            self.outcome is not self.semantic_state.outcome
            or self.price != self.semantic_state.price
            or normalized_url != self.semantic_state.canonical_url
            or rectangles != self.semantic_state.css_rectangles
        ):
            raise ValueError(
                "observation does not match its verified semantic state"
            )


@runtime_checkable
class BrowserPage(Protocol):
    """Minimal browser-page surface shared by site adapters."""

    @property
    def url(self) -> str: ...


@runtime_checkable
class SiteAdapter(Protocol):
    """Stable boundary between a website task and its completed result."""

    channel: WebsiteChannel

    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult: ...


@runtime_checkable
class SiteObservationAdapter(Protocol):
    """Production site boundary that returns no evidence artifacts."""

    channel: WebsiteChannel

    def observe(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation: ...


@runtime_checkable
class ResumableSiteObservationAdapter(SiteObservationAdapter, Protocol):
    """Adapter that can safely revalidate a durable detail-page checkpoint."""

    def resume(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation: ...

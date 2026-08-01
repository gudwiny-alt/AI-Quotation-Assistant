"""Local, non-formal diagnostics for HONOR search-card match failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quote_app.sites.locators import visible_locators
from quote_app.tasks.models import (
    WebsiteChannel,
    WebsiteTask,
    url_contains_credentials,
)
from quote_app.tasks.retry import NonRetryableTechnicalError

_MATCH_FAILURE_CODES = frozenset(
    {
        "HONOR_PRODUCT_MATCH_MISSING",
        "HONOR_PRODUCT_MATCH_AMBIGUOUS",
    }
)
_MAX_CARDS = 12
_MAX_TEXT_LENGTH = 240


def capture_honor_search_diagnostic(
    task: WebsiteTask,
    error: BaseException,
    screenshot_path: Path,
    page: Any,
) -> Path | None:
    """Persist the current public search page only for HONOR card failures."""
    if not _is_honor_match_failure(task, error):
        return None
    if not isinstance(screenshot_path, Path):
        raise ValueError("screenshot_path must be a Path")

    target = screenshot_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return None
    screenshot(path=str(target), full_page=False)
    if not target.is_file():
        return None
    try:
        cards = _visible_cards(page)
    except Exception:
        cards = []
    payload = {
        "schema_version": 1,
        "model_name": task.model_name,
        "search_url": _safe_url(getattr(page, "url", "")),
        "cards": cards,
        "screenshot_path": str(target),
    }
    target.with_suffix(f"{target.suffix}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _is_honor_match_failure(
    task: WebsiteTask,
    error: BaseException,
) -> bool:
    return (
        task.brand == "HONOR"
        and task.channel is WebsiteChannel.OFFICIAL
        and isinstance(error, NonRetryableTechnicalError)
        and error.code in _MATCH_FAILURE_CODES
    )


def _visible_cards(page: Any) -> list[dict[str, str | None]]:
    regions = visible_locators(page, ("#mainSaleList",))
    if len(regions) != 1:
        return []
    cards = visible_locators(regions[0], ("li.grid-items",))[:_MAX_CARDS]
    return [_card_record(card) for card in cards]


def _card_record(card: Any) -> dict[str, str | None]:
    text = _bounded_text(_safe_inner_text(card))
    href: str | None = None
    links = visible_locators(card, ("a.thumb",))
    if len(links) == 1:
        value = links[0].get_attribute("href")
        href = _safe_url(value) if isinstance(value, str) else None
    return {"text": text, "href": href}


def _safe_inner_text(locator: Any) -> str:
    try:
        value = locator.inner_text()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _bounded_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_TEXT_LENGTH]


def _safe_url(value: object) -> str:
    if not isinstance(value, str) or url_contains_credentials(value):
        return ""
    return _bounded_text(value)

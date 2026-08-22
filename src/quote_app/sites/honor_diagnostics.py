"""Local, non-formal diagnostics for HONOR search-card match failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quote_app.sites.locators import visible_locators
from quote_app.sites.official_overrides.honor import HonorOfficialOverride
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
        cards = _visible_cards(page, task)
    except Exception:
        cards = []
    payload = {
        "schema_version": 2,
        "model_name": task.model_name,
        "search_url": _safe_url(getattr(page, "url", "")),
        "cards": cards,
        "summary": _summary(cards),
        "attempt_trace": _attempt_trace(error),
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


def _visible_cards(
    page: Any,
    task: WebsiteTask,
) -> list[dict[str, object]]:
    regions = visible_locators(page, ("#mainSaleList",))
    cards = (
        visible_locators(regions[0], ("li.grid-items",))
        if len(regions) == 1
        else ()
    )
    if not cards:
        cards = visible_locators(
            page,
            HonorOfficialOverride.current_product_links,
        )
    cards = cards[:_MAX_CARDS]
    return [_card_record(task, card) for card in cards]


def _card_record(task: WebsiteTask, card: Any) -> dict[str, object]:
    text = _bounded_text(_safe_inner_text(card))
    href: str | None = None
    links = visible_locators(card, ("a.thumb",))
    if not links:
        value = card.get_attribute("href")
        if isinstance(value, str) and value.strip():
            links = (card,)
    model_matches = HonorOfficialOverride.card_matches_model(
        task.model_name,
        text,
    )
    if len(links) == 1:
        value = links[0].get_attribute("href")
        href = _safe_url(value) if isinstance(value, str) else None
    if not model_matches:
        candidate_status = "model_not_matched"
    elif not links:
        candidate_status = "thumbnail_link_missing"
    elif len(links) != 1:
        candidate_status = "thumbnail_link_ambiguous"
    elif not href:
        candidate_status = "thumbnail_href_missing"
    else:
        candidate_status = "ready_to_click"
    return {
        "text": text,
        "href": href,
        "model_matches": model_matches,
        "thumb_count": len(links),
        "candidate_status": candidate_status,
    }


def _summary(cards: list[dict[str, object]]) -> dict[str, int]:
    return {
        "visible_card_count": len(cards),
        "model_card_count": sum(
            record["model_matches"] is True
            for record in cards
        ),
        "ready_card_count": sum(
            record["candidate_status"] == "ready_to_click"
            for record in cards
        ),
    }


def _attempt_trace(error: BaseException) -> list[dict[str, object]]:
    value = getattr(error, "honor_search_trace", ())
    if not isinstance(value, list):
        return []
    return [record for record in value if isinstance(record, dict)]


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

"""Local, non-formal diagnostics for Huawei official search failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from quote_app.sites.locators import visible_locators
from quote_app.sites.official_brands.huawei import (
    _RESULT_CARD,
    _RESULT_LINK,
    _RESULT_REGION,
    _RESULT_TITLE,
    _SEARCH_INPUT,
    _approved_product_url,
    _title_matches,
)
from quote_app.tasks.models import WebsiteChannel, WebsiteTask, url_contains_credentials
from quote_app.tasks.retry import LayoutRecognitionError

_FALLBACK_PRODUCT_LINKS = (
    'a[href*="/product/"]',
    'a[href*="/product/comdetail/"]',
)
_MAX_CARDS = 12
_MAX_TEXT_LENGTH = 240


def capture_huawei_search_diagnostic(
    task: WebsiteTask,
    error: BaseException,
    screenshot_path: Path,
    page: Any,
) -> Path | None:
    """Capture public VMALL search evidence without changing task decisions."""
    if not _is_huawei_search_failure(task, error, page):
        return None
    target = screenshot_path.expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        screenshot = getattr(page, "screenshot", None)
        if not callable(screenshot):
            return None
        screenshot(path=str(target), full_page=False)
        if not target.is_file():
            return None
        payload = _payload(task, page)
        target.with_suffix(f"{target.suffix}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    return target


def _is_huawei_search_failure(
    task: WebsiteTask,
    error: BaseException,
    page: Any,
) -> bool:
    parsed = urlsplit(str(getattr(page, "url", "")))
    return (
        task.brand == "华为"
        and task.channel is WebsiteChannel.OFFICIAL
        and isinstance(error, LayoutRecognitionError)
        and (parsed.hostname or "").lower() == "www.vmall.com"
        and parsed.path == "/portal/search/index.html"
    )


def _payload(task: WebsiteTask, page: Any) -> dict[str, object]:
    regions = visible_locators(page, (_RESULT_REGION,))
    cards = (
        visible_locators(regions[0], (_RESULT_CARD,))[:_MAX_CARDS]
        if len(regions) == 1
        else ()
    )
    configured_cards = [_configured_card(task, card) for card in cards]
    fallback_links = [
        _link_record(link)
        for link in visible_locators(page, _FALLBACK_PRODUCT_LINKS)[:_MAX_CARDS]
    ]
    inputs = visible_locators(page, (_SEARCH_INPUT,))
    keyword = _safe_input_value(inputs[0]) if len(inputs) == 1 else ""
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "run_id": task.run_id,
        "model_name": _bounded_text(task.model_name),
        "page_url": _safe_url(getattr(page, "url", "")),
        "search_keyword": keyword,
        "result_region_count": len(regions),
        "configured_cards": configured_cards,
        "fallback_links": fallback_links,
    }


def _configured_card(task: WebsiteTask, card: Any) -> dict[str, object]:
    titles = visible_locators(card, (_RESULT_TITLE,))
    links = visible_locators(card, (_RESULT_LINK,))
    title = _safe_inner_text(titles[0]) if len(titles) == 1 else _safe_inner_text(card)
    href = _safe_url(links[0].get_attribute("href")) if len(links) == 1 else ""
    model_matches = _title_matches(task.model_name, title)
    approved = False
    if href:
        try:
            _approved_product_url(href)
            approved = True
        except ValueError:
            pass
    decision = (
        "model_not_matched"
        if not model_matches
        else "approved_detail_url_missing"
        if not approved
        else "accepted_exact_detail"
    )
    return {
        "title": _bounded_text(title),
        "href": href,
        "model_matches": model_matches,
        "decision": decision,
    }


def _link_record(link: Any) -> dict[str, str]:
    return {
        "text": _bounded_text(_safe_inner_text(link)),
        "href": _safe_url(link.get_attribute("href")),
    }


def _safe_input_value(locator: Any) -> str:
    try:
        return _bounded_text(locator.input_value())
    except Exception:
        return ""


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

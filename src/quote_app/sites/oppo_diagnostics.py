"""Local, non-formal diagnostics for OPPO search-card selection failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quote_app.sites.official_brands.oppo import (
    _EMPTY_RESULTS,
    _PRODUCT_LINKS,
    _PRODUCT_TITLES,
    _RESULT_REGIONS,
    _SEARCH_DIALOGS,
    _SEARCH_INPUTS,
    _approved_product_url,
    _input_value,
    _title_matches_model,
)
from quote_app.tasks.models import WebsiteChannel, WebsiteTask, url_contains_credentials

_MAX_CARDS = 12
_MAX_TEXT_LENGTH = 240
_OPPO_BRAND = "欧珀"


def capture_oppo_search_diagnostic(
    task: WebsiteTask,
    error: BaseException,
    screenshot_path: Path,
    page: Any,
) -> Path | None:
    """Capture a bounded OPPO search-card trace without changing task flow."""
    if not _is_oppo_search_card_failure(task, error):
        return None
    if not isinstance(screenshot_path, Path):
        raise ValueError("screenshot_path must be a Path")
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
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    return target


def _is_oppo_search_card_failure(task: WebsiteTask, error: BaseException) -> bool:
    return (
        task.brand == _OPPO_BRAND
        and task.channel is WebsiteChannel.OFFICIAL
        and getattr(error, "oppo_search_card_failure", False) is True
    )


def _payload(task: WebsiteTask, page: Any) -> dict[str, object]:
    scope = _first_visible(page, _SEARCH_DIALOGS)
    region = _first_visible(scope, _RESULT_REGIONS) if scope is not None else None
    cards_scope = scope if scope is not None else page
    links = _visible(cards_scope, _PRODUCT_LINKS)[:_MAX_CARDS]
    explicit_empty = _has_explicit_empty(scope)
    cards = [_card_record(task, link) for link in links]
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "run_id": task.run_id,
        "model_name": _bounded_text(task.model_name),
        "page_url": _safe_url(getattr(page, "url", "")),
        "search_keyword": _search_keyword(scope, task.model_name),
        "search_scope_found": scope is not None,
        "result_region_found": region is not None,
        "explicit_empty_result": explicit_empty,
        "failure_stage": "search_card_selection",
        "reason_code": _reason_code(scope, region, explicit_empty, cards),
        "cards": cards,
    }


def _card_record(task: WebsiteTask, link: Any) -> dict[str, object]:
    raw_href = _safe_url(link.get_attribute("href"))
    nested_titles = [
        _bounded_text(title.inner_text())
        for title in _visible(link, _PRODUCT_TITLES)
    ]
    inspected_title = _first_nonempty(
        _attribute_text(link, "aria-label"),
        _attribute_text(link, "title"),
        nested_titles[0] if nested_titles else "",
        _safe_inner_text(link),
    )
    model_matches = _title_matches_model(task.model_name, inspected_title)
    normalized_href = ""
    if raw_href:
        try:
            normalized_href = _approved_product_url(raw_href)
        except ValueError:
            normalized_href = ""
    decision = (
        "model_not_matched"
        if not model_matches
        else "approved_detail_url_missing"
        if not normalized_href
        else "accepted_exact_detail"
    )
    return {
        "aria_label": _attribute_text(link, "aria-label"),
        "title_attribute": _attribute_text(link, "title"),
        "visible_text": _bounded_text(_safe_inner_text(link)),
        "nested_titles": nested_titles,
        "raw_href": raw_href,
        "normalized_href": normalized_href,
        "model_matches": model_matches,
        "decision": decision,
    }


def _reason_code(
    scope: Any | None,
    region: Any | None,
    explicit_empty: bool,
    cards: list[dict[str, object]],
) -> str:
    if scope is None:
        return "SEARCH_SCOPE_MISSING"
    if region is None:
        return "RESULT_REGION_MISSING"
    if explicit_empty:
        return "EXPLICIT_EMPTY_RESULT"
    if any(card["decision"] == "approved_detail_url_missing" for card in cards):
        return "EXACT_MODEL_URL_REJECTED"
    if cards:
        return "NO_EXACT_MODEL_CARD"
    return "RESULT_CARDS_MISSING"


def _search_keyword(scope: Any | None, model_name: str) -> str:
    if scope is None:
        return ""
    for candidate in _visible(scope, _SEARCH_INPUTS):
        value = _input_value(candidate)
        if value == model_name:
            return _bounded_text(value)
    return ""


def _has_explicit_empty(scope: Any | None) -> bool:
    return scope is not None and any(
        "未找到相关商品" in _safe_inner_text(item)
        for item in _visible(scope, _EMPTY_RESULTS)
    )


def _visible(scope: Any, selectors: tuple[str, ...]) -> tuple[Any, ...]:
    found: list[Any] = []
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    found.append(item)
        except (AttributeError, RuntimeError):
            continue
        if found:
            break
    return tuple(found)


def _first_visible(scope: Any, selectors: tuple[str, ...]) -> Any | None:
    matches = _visible(scope, selectors)
    return matches[0] if matches else None


def _attribute_text(link: Any, name: str) -> str:
    return _bounded_text(link.get_attribute(name))


def _safe_inner_text(locator: Any) -> str:
    try:
        value = locator.inner_text()
    except (AttributeError, RuntimeError):
        return ""
    return value if isinstance(value, str) else ""


def _first_nonempty(*values: str) -> str:
    return next((value for value in values if value), "")


def _bounded_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_TEXT_LENGTH]


def _safe_url(value: object) -> str:
    if not isinstance(value, str) or url_contains_credentials(value):
        return ""
    return _bounded_text(value)

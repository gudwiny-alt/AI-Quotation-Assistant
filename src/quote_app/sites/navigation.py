"""Bounded, state-based navigation waits for dynamic store pages."""

from __future__ import annotations

from typing import Any

from quote_app.tasks.retry import LayoutRecognitionError


_NAVIGATION_POLL_MILLISECONDS = 100
_NAVIGATION_MAX_ATTEMPTS = 100


def click_and_wait_for_navigation(
    page: Any,
    action: Any,
    *,
    semantic_name: str,
    attempts: int = _NAVIGATION_MAX_ATTEMPTS,
) -> None:
    """Click one action and wait until it causes a new, loaded URL."""
    if not isinstance(semantic_name, str) or not semantic_name.strip():
        raise ValueError("semantic_name must not be blank")
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    if not callable(getattr(action, "click", None)):
        raise ValueError("action must provide click")
    if not callable(getattr(page, "wait_for_timeout", None)) or not callable(
        getattr(page, "wait_for_load_state", None)
    ):
        raise ValueError("page must provide synchronous navigation waits")
    before_url = getattr(page, "url", None)
    if not isinstance(before_url, str) or not before_url.strip():
        raise ValueError("page must expose a nonblank URL")

    action.click()
    for _ in range(attempts):
        current_url = getattr(page, "url", None)
        if isinstance(current_url, str) and current_url and current_url != before_url:
            page.wait_for_load_state("domcontentloaded")
            return
        page.wait_for_timeout(_NAVIGATION_POLL_MILLISECONDS)
    raise LayoutRecognitionError(
        f"{semantic_name.strip()}未在限定时间内跳转"
    )

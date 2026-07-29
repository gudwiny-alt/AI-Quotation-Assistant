from __future__ import annotations

import pytest

from quote_app.tasks.retry import LayoutRecognitionError


class _Action:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.clicked = False

    def click(self) -> None:
        self.clicked = True


class _Page:
    def __init__(self, *, changes_url_on_first_wait: bool) -> None:
        self.url = "https://store.example/"
        self.changes_url_on_first_wait = changes_url_on_first_wait
        self.waits: list[int] = []
        self.load_states: list[str] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)
        if self.changes_url_on_first_wait:
            self.url = "https://store.example/search?q=model"

    def wait_for_load_state(self, state: str) -> None:
        self.load_states.append(state)


def test_click_and_wait_for_navigation_waits_until_the_search_url_changes() -> None:
    """Break caught: post-click logic reads the still-loaded pre-search page."""
    from quote_app.sites.navigation import click_and_wait_for_navigation

    page = _Page(changes_url_on_first_wait=True)
    action = _Action(page)

    click_and_wait_for_navigation(page, action, semantic_name="店铺搜索")

    assert action.clicked is True
    assert page.waits == [100]
    assert page.load_states == ["domcontentloaded"]


def test_click_and_wait_for_navigation_fails_when_no_new_page_appears() -> None:
    """Break caught: a dead search click silently advances to the next task."""
    from quote_app.sites.navigation import click_and_wait_for_navigation

    page = _Page(changes_url_on_first_wait=False)

    with pytest.raises(LayoutRecognitionError, match="店铺搜索未在限定时间内跳转"):
        click_and_wait_for_navigation(
            page,
            _Action(page),
            semantic_name="店铺搜索",
            attempts=2,
        )


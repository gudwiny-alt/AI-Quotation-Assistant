"""Reproduce Chrome's empty popup URL before the VMALL navigation commits."""
from types import SimpleNamespace

import pytest

from quote_app.tasks.retry import LayoutRecognitionError
from tests.contract.test_official_huawei_live import DETAIL, SEARCH, _HuaweiPage, _adapter


class _OpeningPortalPage(_HuaweiPage):
    def __init__(self, destination=DETAIL):
        super().__init__()
        self._url = SEARCH
        self.active = "results"
        self.popup = SimpleNamespace(url="", close=self._close_popup)
        self.context = SimpleNamespace(pages=[self, self.popup])
        self.destination = destination
        self.popup_closed = False
        self.navigation_ticks = 0

    def _close_popup(self):
        self.popup_closed = True

    def wait_for_timeout(self, _milliseconds):
        self.navigation_ticks += 1
        if self.navigation_ticks == 4:
            self.popup.url = self.destination


def test_empty_popup_url_waits_for_committed_approved_detail():
    # Previous code rejects the first empty URL, before the destination commits.
    page = _OpeningPortalPage()
    _adapter()._wait_for_portal_detail(page, before_url=SEARCH, controlled_pages=(page,))
    assert page.url == DETAIL
    assert page.goto_calls == [DETAIL]
    assert page.popup_closed


def test_empty_popup_never_commits_times_out_without_following_it():
    page = _OpeningPortalPage(destination="")
    with pytest.raises(LayoutRecognitionError, match="bounded detail confirmation timed out"):
        _adapter()._wait_for_portal_detail(page, before_url=SEARCH, controlled_pages=(page,))
    assert page.goto_calls == []
    assert page.popup_closed
    assert page.navigation_ticks == 40


@pytest.mark.parametrize("destination", ["https://example.com/product/123.html", "https://www.vmall.com/"])
def test_popup_committed_to_non_detail_is_still_rejected(destination):
    page = _OpeningPortalPage(destination=destination)
    with pytest.raises(LayoutRecognitionError, match="did not reach approved detail"):
        _adapter()._wait_for_portal_detail(page, before_url=SEARCH, controlled_pages=(page,))
    assert not page.goto_calls
    assert page.popup_closed


def test_current_text_card_waits_for_popup_before_detail_validation(monkeypatch):
    from tests.contract.test_official_huawei_live import _task
    page = _OpeningPortalPage()
    page.context.pages = [page]
    adapter = _adapter()
    locator = SimpleNamespace(click=lambda: page.context.pages.append(page.popup))
    target = SimpleNamespace(source='current_text', prevalidated_url=None, locator=locator)
    monkeypatch.setattr(adapter, '_wait_for_exact_result', lambda *args: target)
    sentinel = object()
    monkeypatch.setattr(adapter, '_observe_detail', lambda *args, **kwargs: sentinel)
    assert adapter._observe_validated_mode(_task(), page, defer_price=True) is sentinel
    assert page.url == DETAIL and page.popup_closed

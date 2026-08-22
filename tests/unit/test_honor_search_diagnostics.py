from __future__ import annotations

import json
from pathlib import Path

from quote_app.tasks.models import WebsiteChannel, WebsiteTask
from quote_app.tasks.retry import NonRetryableTechnicalError


class _Locator:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> object:
        return self._items[index]


class _Link:
    def __init__(self, href: str) -> None:
        self._href = href

    def is_visible(self) -> bool:
        return True

    def get_attribute(self, name: str) -> str | None:
        return self._href if name == "href" else None


class _CurrentProductLink(_Link):
    def __init__(self, text: str, href: str) -> None:
        super().__init__(href)
        self._text = text

    def inner_text(self) -> str:
        return self._text

    def locator(self, selector: str) -> _Locator:
        assert selector == "a.thumb"
        return _Locator([])


class _Card:
    def __init__(self, text: str, href: str) -> None:
        self._text = text
        self._link = _Link(href)

    def is_visible(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self._text

    def locator(self, selector: str) -> _Locator:
        assert selector == "a.thumb"
        return _Locator([self._link])


class _Page:
    url = "https://www.honor.com/cn/shop/v/search?keyword=%E8%8D%A3%E8%80%80Power2"

    def __init__(self) -> None:
        self.cards = [
            _Card(
                "新品 荣耀Power2 12GB+256GB 幻夜黑 双卡 全网通版",
                "/cn/shop/product/10086147705665.html",
            )
        ]
        self.screenshot_paths: list[Path] = []

    def locator(self, selector: str) -> _Locator:
        if selector == "#mainSaleList":
            return _Locator([self])
        assert selector == "li.grid-items"
        return _Locator(self.cards)

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> _Page:
        assert index == 0
        return self

    def is_visible(self) -> bool:
        return True

    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is False
        screenshot_path = Path(path)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"png")
        self.screenshot_paths.append(screenshot_path)


def _task(*, brand: str = "HONOR") -> WebsiteTask:
    return WebsiteTask(
        task_id="honor-task",
        run_id="run-1",
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-1",
        brand=brand,
        model_name="荣耀Power2",
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
        channel=WebsiteChannel.OFFICIAL,
    )


def test_capture_honor_match_failure_writes_search_page_png_and_cards(
    tmp_path: Path,
) -> None:
    from quote_app.sites.honor_diagnostics import (
        capture_honor_search_diagnostic,
    )

    page = _Page()
    screenshot_path = tmp_path / "task.png"
    error = NonRetryableTechnicalError(
        "HONOR_PRODUCT_MATCH_MISSING",
        "荣耀官网搜索结果未唯一匹配基础机型",
    )
    error.honor_search_trace = [
        {
            "stage": "after_search_click",
            "search_url": "https://www.honor.com/cn/shop/v/search?keyword=Power2",
            "visible_card_count": 1,
            "model_card_count": 1,
            "ready_card_count": 1,
        }
    ]
    captured = capture_honor_search_diagnostic(
        _task(),
        error,
        screenshot_path,
        page,
    )

    assert captured == screenshot_path
    assert page.screenshot_paths == [screenshot_path]
    data = json.loads(screenshot_path.with_suffix(".png.json").read_text())
    assert data == {
        "schema_version": 2,
        "model_name": "荣耀Power2",
        "search_url": page.url,
        "cards": [
            {
                "text": "新品 荣耀Power2 12GB+256GB 幻夜黑 双卡 全网通版",
                "href": "/cn/shop/product/10086147705665.html",
                "model_matches": True,
                "thumb_count": 1,
                "candidate_status": "ready_to_click",
            }
        ],
        "summary": {
            "visible_card_count": 1,
            "model_card_count": 1,
            "ready_card_count": 1,
        },
        "attempt_trace": [
            {
                "stage": "after_search_click",
                "search_url": "https://www.honor.com/cn/shop/v/search?keyword=Power2",
                "visible_card_count": 1,
                "model_card_count": 1,
                "ready_card_count": 1,
            }
        ],
        "screenshot_path": str(screenshot_path),
    }


def test_capture_ignores_non_honor_match_failures(tmp_path: Path) -> None:
    from quote_app.sites.honor_diagnostics import (
        capture_honor_search_diagnostic,
    )

    captured = capture_honor_search_diagnostic(
        _task(brand="小米"),
        NonRetryableTechnicalError(
            "HONOR_PRODUCT_MATCH_MISSING",
            "荣耀官网搜索结果未唯一匹配基础机型",
        ),
        tmp_path / "task.png",
        _Page(),
    )

    assert captured is None
    assert tuple(tmp_path.iterdir()) == ()


def test_capture_reads_current_honor_product_links_without_legacy_grid(
    tmp_path: Path,
) -> None:
    from quote_app.sites.honor_diagnostics import (
        capture_honor_search_diagnostic,
    )

    class _CurrentProductLinkPage(_Page):
        def __init__(self) -> None:
            super().__init__()
            self.current_links = [
                _CurrentProductLink(
                    "荣耀Power2 预估到手价 ¥2699 起 ¥3299 多款可选",
                    "/cn/shop/product/10086147705665.html",
                )
            ]

        def locator(self, selector: str) -> _Locator:
            if selector == "#mainSaleList":
                return _Locator([])
            assert selector == 'a[href*="/cn/shop/product/"]'
            return _Locator(self.current_links)

    screenshot_path = tmp_path / "task.png"
    captured = capture_honor_search_diagnostic(
        _task(),
        NonRetryableTechnicalError(
            "HONOR_PRODUCT_MATCH_MISSING",
            "荣耀官网搜索结果未唯一匹配基础机型",
        ),
        screenshot_path,
        _CurrentProductLinkPage(),
    )

    assert captured == screenshot_path
    data = json.loads(screenshot_path.with_suffix(".png.json").read_text())
    assert data["summary"] == {
        "visible_card_count": 1,
        "model_card_count": 1,
        "ready_card_count": 1,
    }


def test_capture_excludes_credential_bearing_urls(tmp_path: Path) -> None:
    from quote_app.sites.honor_diagnostics import (
        capture_honor_search_diagnostic,
    )

    page = _Page()
    page.url = "https://user:secret@www.honor.com/cn/shop/v/search"
    screenshot_path = tmp_path / "task.png"
    capture_honor_search_diagnostic(
        _task(),
        NonRetryableTechnicalError(
            "HONOR_PRODUCT_MATCH_MISSING",
            "荣耀官网搜索结果未唯一匹配基础机型",
        ),
        screenshot_path,
        page,
    )

    data = json.loads(screenshot_path.with_suffix(".png.json").read_text())
    assert data["search_url"] == ""


def test_capture_keeps_search_screenshot_when_card_inspection_fails(
    tmp_path: Path,
) -> None:
    from quote_app.sites.honor_diagnostics import (
        capture_honor_search_diagnostic,
    )

    class _BrokenCardPage(_Page):
        def locator(self, _selector: str) -> _Locator:
            raise RuntimeError("result DOM is unavailable")

    screenshot_path = tmp_path / "task.png"
    captured = capture_honor_search_diagnostic(
        _task(),
        NonRetryableTechnicalError(
            "HONOR_PRODUCT_MATCH_MISSING",
            "荣耀官网搜索结果未唯一匹配基础机型",
        ),
        screenshot_path,
        _BrokenCardPage(),
    )

    assert captured == screenshot_path
    assert json.loads(screenshot_path.with_suffix(".png.json").read_text())["cards"] == []

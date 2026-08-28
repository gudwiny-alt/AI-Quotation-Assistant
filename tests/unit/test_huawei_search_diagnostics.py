from __future__ import annotations

import json
from pathlib import Path

from quote_app.services.web_run import _capture_search_diagnostic
from quote_app.sites.official_brands.huawei import (
    _RESULT_CARD,
    _RESULT_LINK,
    _RESULT_REGION,
    _RESULT_TITLE,
    _SEARCH_INPUT,
)
from quote_app.tasks.models import WebsiteChannel, WebsiteTask
from quote_app.tasks.retry import LayoutRecognitionError


class _Locator:
    def __init__(self, nodes: list[_Node]) -> None:
        self.nodes = nodes

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> _Node:
        return self.nodes[index]


class _Node:
    def __init__(self, *, text: str = "", attrs: dict[str, str] | None = None) -> None:
        self.text = text
        self.attrs = attrs or {}
        self.groups: dict[str, list[_Node]] = {}

    def locator(self, selector: str) -> _Locator:
        return _Locator(self.groups.get(selector, []))

    def is_visible(self) -> bool:
        return True

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def inner_text(self) -> str:
        return self.text

    def input_value(self) -> str:
        return self.attrs.get("value", "")


class _Page(_Node):
    url = (
        "https://www.vmall.com/portal/search/index.html?"
        "targetRoute=searchresult&searchWord=HUAWEI%20Mate%2080"
    )

    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is False
        Path(path).write_bytes(b"png")


def _task() -> WebsiteTask:
    return WebsiteTask(
        task_id="huawei-diagnostic-task",
        run_id="run-huawei-diagnostic",
        source_row_number=2,
        output_row_number=2,
        material_code="HUAWEI-DIAGNOSTIC",
        brand="华为",
        model_name="HUAWEI Mate 80",
        ram="16GB",
        storage="512GB",
        color="曜石黑",
        channel=WebsiteChannel.OFFICIAL,
    )


def test_huawei_search_failure_writes_card_and_fallback_link_diagnostic(
    tmp_path: Path,
) -> None:
    page = _Page()
    search = _Node(attrs={"value": "HUAWEI Mate 80"})
    region = _Node()
    card = _Node(text="HUAWEI Mate 80")
    title = _Node(text="HUAWEI Mate 80")
    link = _Node(
        text="HUAWEI Mate 80",
        attrs={"href": "https://www.vmall.com/product/100012345.html"},
    )
    page.groups[_SEARCH_INPUT] = [search]
    page.groups[_RESULT_REGION] = [region]
    region.groups[_RESULT_CARD] = [card]
    card.groups[_RESULT_TITLE] = [title]
    card.groups[_RESULT_LINK] = [link]
    page.groups['a[href*="/product/"]'] = [link]
    target = tmp_path / "huawei-search.png"

    captured = _capture_search_diagnostic(
        _task(),
        LayoutRecognitionError("VMALL no-model evidence is incomplete"),
        target,
        page,
    )

    assert captured == target.resolve()
    payload = json.loads(
        target.with_suffix(".png.json").read_text(encoding="utf-8")
    )
    assert payload["search_keyword"] == "HUAWEI Mate 80"
    assert payload["configured_cards"][0]["decision"] == "accepted_exact_detail"
    assert payload["fallback_links"][0]["href"].endswith("/product/100012345.html")

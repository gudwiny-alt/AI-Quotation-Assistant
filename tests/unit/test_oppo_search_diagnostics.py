from __future__ import annotations

import json
from pathlib import Path

from quote_app.sites.official_brands.oppo import (
    OppoSearchCardSelectionError,
    _EMPTY_RESULTS,
    _PRODUCT_LINKS,
    _PRODUCT_TITLES,
    _RESULT_REGIONS,
    _SEARCH_DIALOGS,
    _SEARCH_INPUTS,
)
from quote_app.sites.oppo_diagnostics import capture_oppo_search_diagnostic
from quote_app.services.web_run import _capture_search_diagnostic
from quote_app.tasks.models import WebsiteChannel, WebsiteTask


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
    url = "https://www.opposhop.cn/cn/web/"

    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is False
        Path(path).write_bytes(b"png")


def _task(*, brand: str = "欧珀") -> WebsiteTask:
    return WebsiteTask(
        task_id="oppo-diagnostic-task",
        run_id="run-oppo-diagnostic",
        source_row_number=2,
        output_row_number=2,
        material_code="OPPO-DIAGNOSTIC",
        brand=brand,
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
        channel=WebsiteChannel.OFFICIAL,
    )


def _page(*cards: _Node) -> _Page:
    page = _Page()
    scope = _Node()
    region = _Node()
    keyword = _Node(attrs={"value": "OPPO A5m 5G"})
    page.groups[_SEARCH_DIALOGS[0]] = [scope]
    scope.groups[_RESULT_REGIONS[0]] = [region]
    scope.groups[_SEARCH_INPUTS[0]] = [keyword]
    scope.groups[_PRODUCT_LINKS[0]] = list(cards)
    scope.groups[_EMPTY_RESULTS[0]] = []
    return page


def _card(
    *,
    link_text: str,
    title: str,
    href: str,
    aria_label: str = "",
) -> _Node:
    link = _Node(
        text=link_text,
        attrs={
            "href": href,
            **({"aria-label": aria_label} if aria_label else {}),
        },
    )
    link.groups[_PRODUCT_TITLES[0]] = [_Node(text=title)]
    return link


def _capture_payload(tmp_path: Path, page: _Page) -> dict[str, object]:
    target = tmp_path / "diagnostic.png"
    result = capture_oppo_search_diagnostic(
        _task(), OppoSearchCardSelectionError("search selection"), target, page
    )
    assert result == target.resolve()
    return json.loads(target.with_suffix(".png.json").read_text(encoding="utf-8"))


def test_oppo_diagnostic_records_generic_link_and_nested_exact_model(tmp_path: Path) -> None:
    payload = _capture_payload(
        tmp_path,
        _page(
            _card(
                link_text="查看商品",
                aria_label="查看商品",
                title="OPPO A5m 水晶粉 8GB+256GB 暂时缺货",
                href="/cn/web/products/38672.html",
            )
        ),
    )

    card = payload["cards"][0]
    assert card["aria_label"] == "查看商品"
    assert card["nested_titles"] == ["OPPO A5m 水晶粉 8GB+256GB 暂时缺货"]
    assert card["model_matches"] is False
    assert card["decision"] == "model_not_matched"


def test_oppo_diagnostic_accepts_sold_out_card_without_5g_in_its_title(tmp_path: Path) -> None:
    payload = _capture_payload(
        tmp_path,
        _page(
            _card(
                link_text="OPPO A5m 水晶粉 8GB+256GB 暂时缺货",
                title="OPPO A5m 水晶粉 8GB+256GB 暂时缺货",
                href="/cn/web/products/38672.html",
            )
        ),
    )

    card = payload["cards"][0]
    assert card["model_matches"] is True
    assert card["decision"] == "accepted_exact_detail"


def test_oppo_diagnostic_distinguishes_near_model_and_invalid_url(tmp_path: Path) -> None:
    payload = _capture_payload(
        tmp_path,
        _page(
            _card(
                link_text="OPPO A5m Pro 水晶粉",
                title="OPPO A5m Pro 水晶粉",
                href="/cn/web/products/38673.html",
            ),
            _card(
                link_text="OPPO A5m 水晶粉",
                title="OPPO A5m 水晶粉",
                href="javascript:void(0)",
            ),
        ),
    )

    assert [card["decision"] for card in payload["cards"]] == [
        "model_not_matched",
        "approved_detail_url_missing",
    ]
    assert payload["reason_code"] == "EXACT_MODEL_URL_REJECTED"


def test_oppo_diagnostic_returns_none_for_other_brand_or_write_failure(tmp_path: Path) -> None:
    assert (
        capture_oppo_search_diagnostic(
            _task(brand="小米"),
            OppoSearchCardSelectionError("search selection"),
            tmp_path / "other.png",
            _page(),
        )
        is None
    )


def test_website_run_dispatches_marked_oppo_failure_to_its_diagnostic_collector(
    tmp_path: Path,
) -> None:
    target = tmp_path / "runner.png"

    captured = _capture_search_diagnostic(
        _task(),
        OppoSearchCardSelectionError("search selection"),
        target,
        _page(
            _card(
                link_text="OPPO A5m 水晶粉",
                title="OPPO A5m 水晶粉",
                href="/cn/web/products/38672.html",
            )
        ),
    )

    assert captured == target.resolve()
    assert target.with_suffix(".png.json").is_file()

    class _BrokenPage(_Page):
        def screenshot(self, *, path: str, full_page: bool) -> None:
            raise RuntimeError("disk unavailable")

    assert (
        capture_oppo_search_diagnostic(
            _task(),
            OppoSearchCardSelectionError("search selection"),
            tmp_path / "broken.png",
            _BrokenPage(),
        )
        is None
    )

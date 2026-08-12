from __future__ import annotations

import importlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.evidence.quality import CaptureQualityError
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import LayoutRecognitionError, NonRetryableTechnicalError
from tests.conftest import (
    _OfficialDocumentParser,
    _OfficialFixturePage,
    _OfficialLocator,
    _OfficialNode,
    _official_select,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/vivo"
_ENTRY = "https://shop.vivo.com.cn/"
_SEARCH = "https://www.vivo.com.cn/search/searchResult?searchKeyword=vivo%20X200&page_src=1"
_CAPTURE_SELECTOR_SPECS = {
    "title": {"role": "title", "selector": "section.base-info h1.name"},
    "price": {"role": "price", "selector": "div.summary_price p.sale-price"},
    "capacity": {
        "role": "capacity",
        "selector": "dl.sku-module.specs li.sku-module_item--checked",
        "group_label": "版本",
    },
    "color": {
        "role": "color",
        "selector": "dl.sku-module.specs li.sku-module_item--checked",
        "group_label": "颜色",
    },
}


def _capture_selector_specs() -> dict[str, dict[str, str]]:
    """Return the four real detail-node selectors an adapter must pass to evaluate."""

    return {role: dict(spec) for role, spec in _CAPTURE_SELECTOR_SPECS.items()}


class _VivoLocator(_OfficialLocator):
    def nth(self, index: int) -> _VivoLocator:
        return _VivoLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _VivoLocator:
        return _VivoLocator(
            self.page,
            [
                match
                for node in self.nodes
                for match in _official_select(node.descendants(), selector)
            ],
        )

    def fill(self, value: str) -> None:
        if self.page.active != "home" or self.nodes[0].tag != "input":
            raise AssertionError("only the homepage vivo search input may be filled")
        super().fill(value)
        self.page.fill_calls.append(value)

    def press(self, key: str) -> None:
        if self.page.active != "home" or self.nodes[0].tag != "input" or key != "Enter":
            raise AssertionError("vivo search must submit Enter from the homepage input")
        self.page.presses.append(key)
        self.page.search_submissions += 1
        self.page._url = self.page.search_result_url_override or _SEARCH
        self.page.active = "results"

    def click(self) -> None:
        node = self.nodes[0]
        if node.tag == "a":
            if (
                self.page.active != "results"
                or node.parent is None
                or node.parent.attrs.get("data-skuid") is None
            ):
                raise AssertionError("detail navigation must start from a search result card href")
            self.page.goto(node.attrs["href"])
            return
        group = self.page.group_for(node)
        if group is None:
            return super().click()
        if self.page.active != "detail":
            raise AssertionError("vivo option is unavailable outside active detail state")
        if "spec_item--disabled" in node.attrs.get("class", ""):
            raise AssertionError("disabled vivo option must not be clicked")
        delayed = node.attrs.get("data-select-after-waits")
        if delayed is not None:
            self.page.pending_selection[group] = (node, int(delayed))
            self.page.option_clicks.append(group)
            self.page.events.append(f"clicked:{group}")
            return
        for option in self.page.options(group):
            option.attrs["class"] = option.attrs.get("class", "").replace(
                " sku-module_item--checked", ""
            )
        node.attrs["class"] = f"{node.attrs.get('class', '')} sku-module_item--checked"
        self.page.option_clicks.append(group)
        self.page.events.append(f"selected:{group}")

    def inner_text(self) -> str:
        node = self.nodes[0]
        if node.tag == "h1" and self.page.title_override:
            return self.page.title_override
        if node.tag == "p" and "sale-price" in node.attrs.get("class", ""):
            return self.page.next_sale_price(node)
        return super().inner_text()

    def bounding_box(self) -> dict[str, float] | None:
        return self.page.dom_rect(self.nodes[0])


class _VivoPage(_OfficialFixturePage):
    """Three separate DOMs prevent stale results/home nodes leaking into detail."""

    def __init__(self, detail: str = "detail_normal.html") -> None:
        super().__init__((_FIXTURES / "search_results.html").read_text(), entry_url=_ENTRY)
        self.home_root = self._parse(
            "<html><body><input placeholder='请输入搜索内容' value=''></body></html>"
        )
        self.results_root = self.root
        self.detail_root = self._parse((_FIXTURES / detail).read_text())
        self.blank_root = self._parse("<html><body></body></html>")
        self.active = "blank"
        self.fill_calls: list[str] = []
        self.search_submissions = 0
        self.search_result_url_override: str | None = None
        self.events: list[str] = []
        self.waits = 0
        self.late_after_waits: int | None = None
        self.empty_after_waits: int | None = None
        self.option_waits = {"capacity": 0, "color": 0}
        self.selection_waits = {"capacity": 0, "color": 0}
        self.pending_selection: dict[str, tuple[_OfficialNode, int]] = {}
        self.price_waits = 0
        self.price_visible_after_waits: int | None = None
        self.identity_drift_after_price_waits: int | None = None
        self.ambiguous_capacity_after_price_waits: int | None = None
        self.price_poll = 0
        self.unstable_prices = False
        self.redirect_url: str | None = None
        self.title_override: str | None = None
        self.capture_scale = 1.0
        self.capture_scale_original: float | None = None
        self.capture_scale_marker = False
        self.capture_scales: list[float] = []
        self.capture_selector_arguments: list[dict[str, object]] = []
        self.scroll_offset = 0.0
        self.light_scrolls: list[float] = []
        self.viewport_height = 800.0
        self.blocker: tuple[float, float, bool] | None = None
        self.remove_color_on_refresh = False

    @staticmethod
    def _parse(html: str) -> _OfficialNode:
        parser = _OfficialDocumentParser()
        parser.feed(html)
        return parser.root

    @property
    def root(self) -> _OfficialNode:  # type: ignore[override]
        return self._active_root

    @root.setter
    def root(self, value: _OfficialNode) -> None:
        self._active_root = value

    def active_root(self) -> _OfficialNode:
        return {
            "home": self.home_root,
            "results": self.results_root,
            "detail": self.detail_root,
        }.get(self.active, self.blank_root)

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)
        if url == _ENTRY:
            self._url, self.active = url, "home"
            return
        if url.startswith("https://shop.vivo.com.cn/product/"):
            self._url, self.active = self.redirect_url or url, "detail"
            return
        self._url, self.active = url, "results"

    def locator(self, selector: str) -> _VivoLocator:
        return _VivoLocator(self, _official_select(self.active_root().descendants(), selector))

    def wait_for_timeout(self, milliseconds: float) -> None:
        self.wait_timeout_milliseconds.append(milliseconds)
        if self.active == "detail":
            if self.pending_selection:
                group, (node, reveal_after) = next(iter(self.pending_selection.items()))
                self.selection_waits[group] += 1
                if self.selection_waits[group] >= reveal_after:
                    for option in self.options(group):
                        option.attrs["class"] = option.attrs.get("class", "").replace(
                            " sku-module_item--checked", ""
                        )
                    node.attrs["class"] += " sku-module_item--checked"
                    del self.pending_selection[group]
                return
            if self.selected("capacity") == "12GB+256GB" and self.selected("color") == "辰夜黑":
                self.price_waits += 1
                if (
                    self.price_visible_after_waits is not None
                    and self.price_waits >= self.price_visible_after_waits
                ):
                    next(
                        node
                        for node in self.detail_root.descendants()
                        if node.tag == "p" and "sale-price" in node.attrs.get("class", "")
                    ).attrs.pop("hidden", None)
                if self.identity_drift_after_price_waits == self.price_waits:
                    self._url = "https://shop.vivo.com.cn/product/999999?skuId=9"
                if self.ambiguous_capacity_after_price_waits == self.price_waits:
                    other = next(
                        node
                        for node in self.options("capacity")
                        if node.text != "12GB+256GB"
                    )
                    other.attrs["class"] += " sku-module_item--checked"
                return
            group = "capacity" if self.selected("capacity") != "12GB+256GB" else "color"
            self.option_waits[group] += 1
            for node in self.options(group):
                reveal_after = node.attrs.get("data-reveal-after-option-waits")
                if reveal_after is not None and self.option_waits[group] >= int(reveal_after):
                    node.attrs.pop("hidden", None)
                    node.attrs.pop("data-reveal-after-option-waits", None)
            return
        if self.active != "results":
            raise AssertionError("only result or detail settlement may wait")
        self.waits += 1
        if self.late_after_waits is not None and self.waits >= self.late_after_waits:
            next(
                node
                for node in self.results_root.descendants()
                if "late-result" in node.attrs.get("class", "").split()
            ).attrs.pop("hidden", None)
        if self.empty_after_waits is not None and self.waits >= self.empty_after_waits:
            next(
                node
                for node in self.results_root.descendants()
                if "no-goods" in node.attrs.get("class", "").split()
            ).attrs.pop("hidden", None)

    def group_for(self, node: _OfficialNode) -> str | None:
        if self.active != "detail":
            return None
        return (
            "capacity"
            if node in self.options("capacity")
            else "color"
            if node in self.options("color")
            else None
        )

    def options(self, group: str) -> list[_OfficialNode]:
        if self.active != "detail":
            raise AssertionError("configuration exists only in active detail DOM")
        label = "版本" if group == "capacity" else "颜色"
        module = next(
            node
            for node in self.detail_root.descendants()
            if node.tag == "dl" and "sku-module" in node.attrs.get("class", "")
        )
        start = next(
            index
            for index, child in enumerate(module.children)
            if child.tag == "dt" and child.text == label
        )
        return [
            node
            for node in module.children[start + 1].descendants()
            if node.tag == "li" and "spec_item" in node.attrs.get("class", "")
        ]

    def selected(self, group: str) -> str | None:
        return next(
            (
                node.text
                for node in self.options(group)
                if "sku-module_item--checked" in node.attrs.get("class", "")
            ),
            None,
        )

    def next_sale_price(self, node: _OfficialNode) -> str:
        self.events.append(f"price:{self.selected('capacity')}:{self.selected('color')}")
        if self.selected("capacity") != "12GB+256GB" or self.selected("color") != "辰夜黑":
            return ""
        values = ("4499", "4399", "4399")
        if self.unstable_prices:
            values = ("4499", "4399", "4599", "4299")
            value = values[self.price_poll % len(values)]
        else:
            value = values[min(self.price_poll, len(values) - 1)]
        self.price_poll += 1
        node.text_parts = [value]
        return f"¥{value}"

    def dom_rect(self, node: _OfficialNode) -> dict[str, float] | None:
        if self.active == "results":
            if node.tag == "input":
                return {"x": 30.0, "y": 80.0, "width": 360.0, "height": 42.0}
            if "page-search-result-content" in node.attrs.get("class", ""):
                return {"x": 20.0, "y": 140.0, "width": 900.0, "height": 540.0}
        if self.active != "detail" or (self.remove_color_on_refresh and node.text == "辰夜黑"):
            return None
        if node.tag == "dd" and "sku-module_content" in node.attrs.get("class", ""):
            group = "capacity" if node is self.options("capacity")[0].parent.parent else "color"
            y = 250.0 if group == "capacity" else 342.0
            return {"x": 30.0, "y": y, "width": 600.0, "height": 82.0}
        raw_y = (
            110.0
            if node.tag == "h1"
            else 174.0
            if "sale-price" in node.attrs.get("class", "")
            else 270.0
            if node.text == "12GB+256GB"
            else 362.0
        )
        return {
            "x": 30.0 * self.capture_scale,
            "y": raw_y * self.capture_scale - self.scroll_offset,
            "width": 520.0 * self.capture_scale,
            "height": 42.0 * self.capture_scale,
        }

    def evaluate(self, script: str, argument: object = None) -> object:
        if self.active != "detail":
            raise AssertionError("capture scripts require active detail DOM")
        if "removeAttribute(attribute)" in script:
            if self.capture_scale_marker:
                assert self.capture_scale_original is not None
                self.capture_scale = self.capture_scale_original
                self.capture_scale_original = None
                self.capture_scale_marker = False
            return True
        if "data-quotation-capture-scale-original" in script:
            if argument is not None and self.capture_scale != float(argument):
                if not self.capture_scale_marker:
                    self.capture_scale_original = self.capture_scale
                    self.capture_scale_marker = True
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "inlineZoom" in script and "computedZoom" in script:
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "window.scrollBy" in script:
            if (
                not isinstance(argument, dict)
                or "delta" not in argument
                or "proofs" not in argument
            ):
                raise AssertionError("geometry scroll requires delta plus proof selectors")
            delta = float(argument["delta"])
            geometry = self.evaluate("getBoundingClientRect", argument["proofs"])
            expected = (
                geometry["unionBottom"] - geometry["viewportHeight"] + 8.0
                if geometry["unionBottom"] > geometry["viewportHeight"] - 8.0
                else geometry["unionTop"] - 8.0
                if geometry["unionTop"] < 8.0
                else 0.0
            )
            assert self.capture_scale == 0.8 and delta == expected
            self.light_scrolls.append(delta)
            self.scroll_offset += delta
            return True
        if not isinstance(argument, dict) or tuple(argument) != (
            "title",
            "price",
            "capacity",
            "color",
        ):
            raise AssertionError("capture adapter must provide four explicit proof selectors")
        self.capture_selector_arguments.append(argument)
        proofs = self.proofs_from_selectors(argument)
        if "elementFromPoint" in script:
            return all(self.visible_and_unobscured(node) for node in proofs.values())
        if "getBoundingClientRect" in script:
            boxes = [self.dom_rect(node) for node in proofs.values()]
            if any(box is None for box in boxes):
                return {"missing": True}
            return {
                "unionTop": min(box["y"] for box in boxes if box),
                "unionBottom": max(box["y"] + box["height"] for box in boxes if box),
                "viewportHeight": self.viewport_height,
                "scrollY": self.scroll_offset,
            }
        raise AssertionError(f"unexpected vivo evaluation: {script[:90]}")

    def proofs_from_selectors(self, selectors: object) -> dict[str, _OfficialNode]:
        """Resolve proof nodes only from adapter-supplied real CSS selectors.

        ``group_label`` is deliberately not a test-only locator: it scopes the
        otherwise shared checked-option selector to the real ``dt``/``dd``
        section that represents 版本 or 颜色.  The node itself still comes from
        parsing the selector passed through ``evaluate``.
        """

        if not isinstance(selectors, dict) or tuple(selectors) != (
            "title",
            "price",
            "capacity",
            "color",
        ):
            raise AssertionError("capture adapter must pass four named CSS selectors")
        proofs: dict[str, _OfficialNode] = {}
        for role, expected_tag, expected_class in (
            ("title", "h1", "name"),
            ("price", "p", "sale-price"),
            ("capacity", "li", "sku-module_item--checked"),
            ("color", "li", "sku-module_item--checked"),
        ):
            declaration = selectors[role]
            if not isinstance(declaration, dict) or declaration.get("role") != role:
                raise AssertionError(f"{role} proof selector lacks its role")
            selector = declaration.get("selector")
            if not isinstance(selector, str):
                raise AssertionError(f"{role} proof selector is not CSS")
            matches = _official_select(self.active_root().descendants(), selector)
            if role in {"capacity", "color"}:
                group_label = declaration.get("group_label")
                expected_group = "版本" if role == "capacity" else "颜色"
                if group_label != expected_group:
                    raise AssertionError(f"{role} proof has the wrong sku group")
                if set(declaration) != {"role", "selector", "group_label"}:
                    raise AssertionError(f"{role} proof has unsupported selector metadata")
                group = "capacity" if role == "capacity" else "color"
                matches = [node for node in matches if node in self.options(group)]
            elif set(declaration) != {"role", "selector"}:
                raise AssertionError(f"{role} proof has unsupported selector metadata")
            if len(matches) != 1:
                raise AssertionError(f"{role} proof selector must resolve exactly one live node")
            node = matches[0]
            if node.tag != expected_tag or expected_class not in node.attrs.get("class", ""):
                raise AssertionError(f"{role} proof selector resolved the wrong node")
            proofs[role] = node
        if proofs["price"].text.replace("¥", "") != "4399":
            raise AssertionError("adopted price proof selector did not resolve ¥4399")
        if proofs["capacity"].text != "12GB+256GB" or proofs["color"].text != "辰夜黑":
            raise AssertionError("selected configuration proof selector did not resolve targets")
        return proofs

    def visible_and_unobscured(self, node: _OfficialNode) -> bool:
        box = self.dom_rect(node)
        if box is None or box["y"] < 0 or box["y"] + box["height"] > self.viewport_height:
            return False
        if self.blocker is None:
            return True
        blocker_top, blocker_bottom, fixed = self.blocker
        center_y = box["y"] + box["height"] / 2
        if not fixed:
            blocker_top -= self.scroll_offset
            blocker_bottom -= self.scroll_offset
        return not blocker_top <= center_y <= blocker_bottom


def _task(model: str = "vivo X200") -> WebsiteTask:
    return WebsiteTask(
        task_id="vivo",
        run_id="vivo-contract",
        source_row_number=2,
        output_row_number=2,
        material_code="VIVO",
        brand="维沃",
        model_name=model,
        ram="12GB",
        storage="256GB",
        color="辰夜黑",
        channel=WebsiteChannel.OFFICIAL,
    )


def _adapter() -> Any:
    spec = next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "维沃" and spec.channel is WebsiteChannel.OFFICIAL
    )
    return importlib.import_module("quote_app.sites.official_brands.vivo").VivoOfficialAdapter(spec)


def _set_cards(
    page: _VivoPage,
    cards: tuple[tuple[str, str], ...],
    *,
    retain_late: bool = False,
) -> None:
    container = next(
        node
        for node in page.results_root.descendants()
        if "page-search-result-content" in node.attrs.get("class", "")
    )
    late = next(node for node in container.children if "late-result" in node.attrs.get("class", ""))
    terminal_empty = next(
        node for node in container.children if "no-goods" in node.attrs.get("class", "")
    )
    keyword_input = next(
        node
        for node in container.children
        if node.tag == "input" and node.attrs.get("placeholder") == "请输入搜索内容"
    )
    container.children = [
        keyword_input,
        *([late] if retain_late else []),
        terminal_empty,
    ]
    for index, (title, href) in enumerate(cards):
        card = _OfficialNode(
            "div", {"data-position": str(index), "data-skuid": str(200000 + index)}, container
        )
        image = _OfficialNode("img", {"class": "result-pic"}, card)
        label = _OfficialNode("p", {"class": "result-title"}, card)
        label.text_parts = [title]
        price = _OfficialNode("p", {"class": "result-price"}, card)
        new = _OfficialNode("span", {"class": "result-price-new"}, price)
        new.text_parts = ["4499"]
        old = _OfficialNode("span", {"class": "result-price-old"}, price)
        old.text_parts = ["4699"]
        link = _OfficialNode("a", {"target": "_blank", "href": href}, card)
        price.children.extend((new, old))
        card.children.extend((image, label, price, link))
        container.children.append(card)


def _detail_page_with_adopted_capture_nodes() -> _VivoPage:
    """Prepare actual selected fixture nodes without a production adapter."""

    page = _VivoPage()
    page.goto("https://shop.vivo.com.cn/product/10010284?skuId=135003")
    for group, target in (("capacity", "12GB+256GB"), ("color", "辰夜黑")):
        _VivoLocator(page, [next(node for node in page.options(group) if node.text == target)]).click()
    next(
        node
        for node in page.detail_root.descendants()
        if node.tag == "p" and "sale-price" in node.attrs.get("class", "")
    ).text_parts = ["4399"]
    return page


def _delay_detail_option(page: _VivoPage, group: str, target: str, waits: int) -> None:
    page.active = "detail"
    option = next(node for node in page.options(group) if node.text == target)
    option.attrs["hidden"] = ""
    option.attrs["data-reveal-after-option-waits"] = str(waits)
    page.active = "blank"


def _remove_detail_option(page: _VivoPage, group: str, target: str) -> None:
    page.active = "detail"
    option = next(node for node in page.options(group) if node.text == target)
    assert option.parent is not None
    option.parent.children.remove(option)
    page.active = "blank"


def _delay_detail_selection(page: _VivoPage, group: str, target: str, waits: int) -> None:
    page.active = "detail"
    option = next(node for node in page.options(group) if node.text == target)
    option.attrs["data-select-after-waits"] = str(waits)
    page.active = "blank"


def test_vivo_fixture_uses_real_result_data_attributes_and_isolated_state_dom() -> None:
    page = _VivoPage()
    search = (_FIXTURES / "search_results.html").read_text()
    assert "page-search-result-content" in search and "result-card" not in search
    page.goto(_ENTRY)
    assert (
        page.locator("input[placeholder='请输入搜索内容']").count() == 1
        and page.locator("p.sale-price").count() == 0
    )
    home_input = page.locator("input[placeholder='请输入搜索内容']")
    home_input.fill("vivo X200")
    home_input.press("Enter")
    assert (
        page.locator("div[data-position][data-skuid] a[target=_blank]").count() > 0
        and page.locator("p.sale-price").count() == 0
    )
    page.goto("https://shop.vivo.com.cn/product/10010284?skuId=135003")
    assert (
        page.locator("p.sale-price").count() == 1
        and page.locator("div[data-position][data-skuid]").count() == 0
    )
    fresh = _VivoPage()
    assert fresh.locator("input").count() == 0
    assert fresh.locator("div[data-skuid]").count() == 0
    assert fresh.locator("p.sale-price").count() == 0
    fresh.active = "unknown"
    assert fresh.locator("input").count() == 0
    assert fresh.locator("div[data-skuid]").count() == 0
    assert fresh.locator("p.sale-price").count() == 0
    sku_info = next(
        node
        for node in fresh.detail_root.descendants()
        if "sku-info" in node.attrs.get("class", "")
    )
    primary = next(node for node in sku_info.children if "primary" in node.attrs.get("class", ""))
    assert next(
        node
        for node in primary.children
        if node.tag == "h1" and "name" in node.attrs.get("class", "")
    )
    assert next(node for node in primary.children if "summary" in node.attrs.get("class", ""))
    assert next(
        node
        for node in sku_info.children
        if node.tag == "dl" and "sku-module" in node.attrs.get("class", "")
    )


def test_vivo_capture_harness_requires_four_real_selector_resolutions() -> None:
    page = _detail_page_with_adopted_capture_nodes()

    proofs = page.proofs_from_selectors(_capture_selector_specs())

    assert tuple(proofs) == ("title", "price", "capacity", "color")
    assert proofs["title"].text == "vivo X200"
    assert proofs["price"].text.replace("¥", "") == "4399"
    assert proofs["capacity"].text == "12GB+256GB"
    assert proofs["color"].text == "辰夜黑"


def test_vivo_capture_harness_rejects_a_wrong_real_css_selector() -> None:
    page = _detail_page_with_adopted_capture_nodes()
    wrong = _capture_selector_specs()
    wrong["price"]["selector"] = "div.summary_price p.market-price"

    with pytest.raises(AssertionError, match="price proof selector resolved the wrong node"):
        page.proofs_from_selectors(wrong)


def test_vivo_searches_once_then_quotes_final_stable_selected_offer() -> None:
    page = _VivoPage()
    observation = _adapter().observe(_task(), page)
    assert (
        page.goto_calls[0] == _ENTRY
        and page.fill_calls == ["vivo X200"]
        and page.presses == ["Enter"]
        and page.search_submissions == 1
    )
    assert (
        observation.outcome is BusinessOutcome.PRICE_FOUND
        and observation.price == Decimal("4399")
        and observation.url == page.url
        and page.option_clicks == ["capacity", "color"]
    )


def test_vivo_waits_multiple_rounds_for_late_exact_card_instead_of_no_model() -> None:
    page = _VivoPage()
    _set_cards(
        page,
        (("vivo X200 Pro", "https://shop.vivo.com.cn/product/10010281?skuId=135001"),),
        retain_late=True,
    )
    page.late_after_waits = 3
    result = _adapter().observe(_task(), page)
    assert (
        result.outcome is BusinessOutcome.PRICE_FOUND
        and page.waits == 3
        and result.url.endswith("/product/10010286?skuId=135005")
    )


@pytest.mark.parametrize("reveal_after", [18, 39])
def test_vivo_waits_full_search_budget_for_a_late_exact_card(reveal_after: int) -> None:
    page = _VivoPage()
    _set_cards(
        page,
        (("vivo X200 Pro", "https://shop.vivo.com.cn/product/10010281?skuId=135001"),),
        retain_late=True,
    )
    page.late_after_waits = reveal_after

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.waits == reveal_after


def test_vivo_nonterminal_search_without_exact_card_exhausts_budget_as_technical() -> None:
    page = _VivoPage()
    _set_cards(
        page,
        (("vivo X200 Pro", "https://shop.vivo.com.cn/product/10010281?skuId=135001"),),
    )

    with pytest.raises(LayoutRecognitionError, match="search results|stabilize|terminal"):
        _adapter().observe(_task(), page)

    assert page.waits == 40


def test_vivo_visible_terminal_no_goods_after_bounded_wait_is_legal_no() -> None:
    page = _VivoPage()
    _set_cards(
        page, (("vivo X200 Ultra", "https://shop.vivo.com.cn/product/10010281?skuId=135001"),)
    )
    page.empty_after_waits = 3
    result = _adapter().observe(_task(), page)
    assert (
        result.outcome is BusinessOutcome.NO_MODEL
        and 3 <= page.waits <= 8
        and page.locator("div.no-goods").is_visible()
    )


@pytest.mark.parametrize("model", ["iQOO 15", "iqoo 15", "i QOO 15", "I QOO 15"])
def test_vivo_rejects_iqoo_before_any_visit(model: str) -> None:
    page = _VivoPage()
    adapter = _adapter()
    with pytest.raises(NonRetryableTechnicalError, match="iQOO|不支持|unsupported") as caught:
        adapter.observe(_task(model), page)
    assert caught.value.code == "UNSUPPORTED_VIVO_MODEL_FAMILY"
    assert page.goto_calls == [] and page.fill_calls == [] and page.presses == []
    assert page.search_submissions == 0


@pytest.mark.parametrize(
    "name",
    [
        "vivo X200 Pro",
        "vivo X200 Plus",
        "vivo X200 Ultra",
        "vivo X200 Max",
        "vivo X200S",
        "vivo X200T",
    ],
)
def test_vivo_excludes_derived_models(name: str) -> None:
    page = _VivoPage()
    _set_cards(page, ((name, "https://shop.vivo.com.cn/product/10010281?skuId=135001"),))
    page.empty_after_waits = 3
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.NO_MODEL


def test_vivo_skips_invalid_first_exact_url_but_all_invalid_exact_urls_are_technical() -> None:
    page = _VivoPage()
    _set_cards(
        page,
        (
            ("vivo X200 12GB+256GB", "https://shop.vivo.com.cn/product/not-a-number?skuId=1"),
            ("vivo X200 12GB+512GB", "https://shop.vivo.com.cn/product/10010284?skuId=135003"),
        ),
    )
    assert _adapter().observe(_task(), page).url.endswith("/product/10010284?skuId=135003")
    bad = _VivoPage()
    _set_cards(bad, (("vivo X200", "https://shop.vivo.com.cn/product/not-a-number?skuId=1"),))
    with pytest.raises(LayoutRecognitionError):
        _adapter().observe(_task(), bad)


@pytest.mark.parametrize(
    ("fixture", "outcome", "role"),
    [
        ("detail_missing_capacity.html", BusinessOutcome.CAPACITY_UNAVAILABLE, "capacity"),
        ("detail_missing_color.html", BusinessOutcome.COLOR_UNAVAILABLE, "color"),
    ],
)
def test_vivo_disabled_exact_option_is_legal_no(
    fixture: str, outcome: BusinessOutcome, role: str
) -> None:
    result = _adapter().observe(_task(), _VivoPage(fixture))
    assert result.outcome is outcome and tuple(rect.role for rect in result.css_rectangles) == (
        role,
    )


@pytest.mark.parametrize("group", ["capacity", "color"])
def test_vivo_waits_for_target_option_that_appears_on_tick_eighteen(group: str) -> None:
    page = _VivoPage()
    target = "12GB+256GB" if group == "capacity" else "辰夜黑"
    _delay_detail_option(page, group, target, 18)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_waits[group] == 18


@pytest.mark.parametrize(
    ("group", "outcome"),
    [
        ("capacity", BusinessOutcome.CAPACITY_UNAVAILABLE),
        ("color", BusinessOutcome.COLOR_UNAVAILABLE),
    ],
)
def test_vivo_missing_target_option_waits_full_five_second_budget(
    group: str, outcome: BusinessOutcome
) -> None:
    page = _VivoPage()
    target = "12GB+256GB" if group == "capacity" else "辰夜黑"
    _remove_detail_option(page, group, target)

    result = _adapter().observe(_task(), page)

    assert result.outcome is outcome
    assert page.option_waits[group] == 20


@pytest.mark.parametrize("group", ["capacity", "color"])
def test_vivo_waits_for_clicked_option_to_become_uniquely_selected(group: str) -> None:
    page = _VivoPage()
    target = "12GB+256GB" if group == "capacity" else "辰夜黑"
    _delay_detail_selection(page, group, target, 4)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.selection_waits[group] == 4


def test_vivo_price_waits_three_seconds_stable_within_five_second_total_budget() -> None:
    page = _VivoPage()

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4399")
    assert page.price_waits == 13


def test_vivo_price_may_appear_late_but_still_needs_three_seconds_stability() -> None:
    page = _VivoPage()
    price = next(
        node
        for node in page.detail_root.descendants()
        if node.tag == "p" and "sale-price" in node.attrs.get("class", "")
    )
    price.attrs["hidden"] = ""
    page.price_visible_after_waits = 6

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4399")
    assert page.price_waits == 19


@pytest.mark.parametrize("drift", ["identity", "ambiguity"])
def test_vivo_price_wait_does_not_swallow_identity_or_configuration_drift(drift: str) -> None:
    page = _VivoPage()
    if drift == "identity":
        page.identity_drift_after_price_waits = 2
    else:
        page.ambiguous_capacity_after_price_waits = 2

    with pytest.raises((LayoutRecognitionError, CaptureQualityError)):
        _adapter().observe(_task(), page)

    assert page.price_waits == 2


def test_vivo_does_not_reclick_preselected_targets_and_revalidates_final_detail() -> None:
    page = _VivoPage()
    page.goto("https://shop.vivo.com.cn/product/10010284?skuId=135003")
    for group, target in (("capacity", "12GB+256GB"), ("color", "辰夜黑")):
        next(node for node in page.options(group) if node.text == target).attrs["class"] += (
            " sku-module_item--checked"
        )
    page.active = "blank"
    assert (
        _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
        and page.option_clicks == []
    )
    changed = _VivoPage()
    changed.redirect_url = "https://shop.vivo.com.cn/product/999999?skuId=9"
    with pytest.raises((LayoutRecognitionError, NonRetryableTechnicalError)):
        _adapter().observe(_task(), changed)


def test_vivo_title_drift_and_unstable_offer_are_technical_failures() -> None:
    title = _VivoPage()
    title.title_override = "vivo X200 Pro"
    with pytest.raises((LayoutRecognitionError, NonRetryableTechnicalError)):
        _adapter().observe(_task(), title)
    unstable = _VivoPage()
    unstable.unstable_prices = True
    with pytest.raises((LayoutRecognitionError, CaptureQualityError)):
        _adapter().observe(_task(), unstable)


@pytest.mark.parametrize(
    "near_match",
    ["vivo X200 青春版", "vivo X200 手机壳", "vivo X200 保护壳", "vivo X200 钢化膜"],
)
def test_vivo_rejects_nonphone_or_edition_suffixes(near_match: str) -> None:
    page = _VivoPage()
    _set_cards(
        page,
        ((near_match, "https://shop.vivo.com.cn/product/10010281?skuId=135001"),),
    )
    page.empty_after_waits = 3

    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.NO_MODEL


def test_vivo_requires_exact_search_route_after_enter() -> None:
    page = _VivoPage()
    page.search_result_url_override = "https://www.vivo.com.cn/other"
    with pytest.raises(LayoutRecognitionError, match="searchResult"):
        _adapter().observe(_task(), page)


@pytest.mark.parametrize(
    ("outcome", "url"),
    [
        (BusinessOutcome.NO_MODEL, "https://shop.vivo.com.cn/product/10010284?skuId=135003"),
        (BusinessOutcome.PRICE_FOUND, "https://www.vivo.com.cn/search/searchResult?searchKeyword=x"),
        (BusinessOutcome.PRICE_FOUND, "https://shop.vivo.com.cn/other"),
    ],
)
def test_vivo_resume_rejects_checkpoint_path_for_the_wrong_outcome(
    outcome: BusinessOutcome, url: str
) -> None:
    checkpoint = WebsiteObservationCheckpoint(
        task_id="vivo",
        outcome=outcome,
        price=Decimal("4399") if outcome is BusinessOutcome.PRICE_FOUND else None,
        url=url,
        observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )

    with pytest.raises(LayoutRecognitionError):
        _adapter().resume(_task(), _VivoPage(), checkpoint)


def test_vivo_sale_price_scope_chooses_lower_current_value_only() -> None:
    page = _VivoPage()
    price = next(
        node
        for node in page.detail_root.descendants()
        if node.tag == "p" and "sale-price" in node.attrs.get("class", "")
    )
    price.children.clear()
    price.text_parts = ["¥4499 ¥4399"]

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4399")


@pytest.mark.parametrize(
    ("viewport", "scale", "needs_scroll"),
    [(800.0, [], False), (340.0, [0.8], False), (280.0, [0.8], True)],
)
def test_vivo_capture_reads_real_four_proofs_from_scaled_domrects(
    viewport: float, scale: list[float], needs_scroll: bool
) -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = viewport
    adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    expected_delta = max(0.0, (362.0 + 42.0) * 0.8 - viewport + 8.0)
    assert (
        page.proofs_from_selectors(_capture_selector_specs())["price"]
        .text.replace("¥", "")
        .endswith("4399")
        and page.capture_scales == scale
    )
    assert page.capture_selector_arguments and all(
        selectors == _capture_selector_specs() for selectors in page.capture_selector_arguments
    )
    assert page.light_scrolls == ([expected_delta] if needs_scroll else [])


def test_vivo_capture_fixed_overlay_or_disappearing_proof_fails_closed_after_one_attempt() -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = 280
    page.blocker = (200, 400, True)
    with pytest.raises((LayoutRecognitionError, CaptureQualityError)):
        adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert page.capture_scales == [0.8] and len(page.light_scrolls) == 1
    gone = _VivoPage()
    second = _adapter()
    state = second.observe(_task(), gone)
    gone.remove_color_on_refresh = True
    with pytest.raises((LayoutRecognitionError, CaptureQualityError)):
        second.prepare_capture_view(_task(), gone, state.semantic_state)


def test_vivo_capture_can_scroll_up_once_from_real_union_geometry() -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = 340
    page.scroll_offset = 120

    adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.capture_scales == [0.8]
    assert page.light_scrolls == [-40.0]


def test_vivo_capture_rejects_a_proof_union_taller_than_safe_viewport() -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = 200

    with pytest.raises(LayoutRecognitionError, match="cannot fit"):
        adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.light_scrolls == []


def test_vivo_capture_does_not_rewrite_an_existing_eighty_percent_scale() -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = 340
    page.capture_scale = 0.8

    adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.capture_scale == 0.8
    assert page.capture_scales == []

    adapter.restore_capture_view(_task(), page, observation.semantic_state)

    assert page.capture_scale == 0.8

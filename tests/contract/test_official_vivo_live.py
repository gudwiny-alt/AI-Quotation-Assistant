from __future__ import annotations

import importlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from tests.conftest import _OfficialFixturePage, _OfficialLocator, _OfficialNode, _official_select

_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/vivo"
_ENTRY = "https://shop.vivo.com.cn/"
_SEARCH = "https://www.vivo.com.cn/search/searchResult?searchKeyword=vivo%20X200&page_src=1"


class _VivoLocator(_OfficialLocator):
    """Browser seam; production-facing selectors see only public live-shaped DOM."""

    def nth(self, index: int) -> _VivoLocator:
        return _VivoLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _VivoLocator:
        self.page.locator_calls.append(selector)
        return _VivoLocator(
            self.page,
            [
                match
                for node in self.nodes
                for match in _official_select(node.descendants(), selector)
            ],
        )

    def fill(self, value: str) -> None:
        super().fill(value)
        self.page.fill_calls.append(value)

    def press(self, key: str) -> None:
        self.page.presses.append(key)
        if key == "Enter":
            self.page.search_submissions += 1
            self.page._url = _SEARCH
            self.page.active = "results"

    def click(self) -> None:
        node = self.nodes[0]
        if node.tag == "a" and "/product/" in node.attrs.get("href", ""):
            self.page.goto(node.attrs["href"])
            return
        group = self.page.group_for(node)
        if group is not None:
            if "spec_item--disabled" in node.attrs.get("class", ""):
                raise AssertionError("disabled vivo option must not be clicked")
            for option in self.page.options(group):
                option.attrs["class"] = option.attrs.get("class", "").replace(
                    " sku-module_item--checked", ""
                )
            node.attrs["class"] = f"{node.attrs.get('class', '')} sku-module_item--checked"
            self.page.option_clicks.append(group)
            self.page.events.append(f"selected:{group}")
            return
        super().click()

    def inner_text(self) -> str:
        node = self.nodes[0]
        if (
            node.tag == "h1"
            and "name" in node.attrs.get("class", "")
            and self.page.title_override is not None
        ):
            return self.page.title_override
        if node.tag == "p" and "sale-price" in node.attrs.get("class", ""):
            return self.page.next_sale_price(node)
        return super().inner_text()

    def bounding_box(self) -> dict[str, float] | None:
        return self.page.dom_rect(self.nodes[0])


class _VivoPage(_OfficialFixturePage):
    """State changes model browser events; fixture HTML contains no test hooks."""

    def __init__(self, detail: str = "detail_normal.html") -> None:
        super().__init__(
            (_FIXTURES / "search_results.html").read_text() + (_FIXTURES / detail).read_text(),
            entry_url=_ENTRY,
        )
        self.active = "blank"
        self.fill_calls: list[str] = []
        self.search_submissions = 0
        self.events: list[str] = []
        self.locator_calls: list[str] = []
        self.waits = 0
        self.late_after_waits: int | None = None
        self.empty_after_waits: int | None = None
        self.price_poll = 0
        self.unstable_prices = False
        self.redirect_url: str | None = None
        self.title_override: str | None = None
        self.capture_scale = 1.0
        self.capture_scales: list[float] = []
        self.scroll_offset = 0.0
        self.light_scrolls: list[float] = []
        self.viewport_height = 800.0
        self.blocker: tuple[float, float] | None = None
        self.remove_color_on_refresh = False

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
        self.locator_calls.append(selector)
        return _VivoLocator(self, _official_select(self.root.descendants(), selector))

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        self.waits += 1
        if self.late_after_waits is not None and self.waits >= self.late_after_waits:
            next(
                node
                for node in self.root.descendants()
                if "late-result" in node.attrs.get("class", "").split()
            ).attrs.pop("hidden", None)
        if self.empty_after_waits is not None and self.waits >= self.empty_after_waits:
            next(
                node
                for node in self.root.descendants()
                if "no-goods" in node.attrs.get("class", "").split()
            ).attrs.pop("hidden", None)

    def group_for(self, node: _OfficialNode) -> str | None:
        for group, label in (("capacity", "版本"), ("color", "颜色")):
            if node in self.options(group) and label:
                return group
        return None

    def options(self, group: str) -> list[_OfficialNode]:
        label = "版本" if group == "capacity" else "颜色"
        module = next(
            node
            for node in self.root.descendants()
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
        values = (
            ("4499", "4399", "4399")
            if not self.unstable_prices
            else ("4499", "4399", "4599", "4299")
        )
        value = values[self.price_poll % len(values)]
        self.price_poll += 1
        node.text_parts = [value]
        return f"¥{value}"

    def dom_rect(self, node: _OfficialNode) -> dict[str, float] | None:
        if self.remove_color_on_refresh and node.text == "辰夜黑":
            return None
        role_y = (
            110.0
            if node.tag == "h1"
            else 174.0
            if "sale-price" in node.attrs.get("class", "")
            else 270.0
            if node.text == "12GB+256GB"
            else 362.0
        )
        return {
            "x": 30.0,
            "y": role_y * self.capture_scale - self.scroll_offset,
            "width": 520.0,
            "height": 42.0,
        }

    def evaluate(self, script: str, argument: object = None) -> object:
        if "data-quotation-capture-scale-original" in script:
            if argument is not None and self.capture_scale != float(argument):
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "elementFromPoint" in script:
            proofs = self.capture_proofs()
            return all(self.visible_and_unobscured(node) for node in proofs.values())
        if "getBoundingClientRect" in script:
            boxes = [self.dom_rect(node) for node in self.capture_proofs().values()]
            assert all(box is not None for box in boxes)
            return {
                "unionTop": min(box["y"] for box in boxes if box),
                "unionBottom": max(box["y"] + box["height"] for box in boxes if box),
                "viewportHeight": self.viewport_height,
                "scrollY": self.scroll_offset,
            }
        if "window.scrollBy" in script:
            delta = float(argument)
            assert self.capture_scale == 0.8 and 70.0 <= delta <= 120.0
            self.light_scrolls.append(delta)
            self.scroll_offset += delta
            return True
        raise AssertionError(f"unexpected vivo evaluation: {script[:90]}")

    def capture_proofs(self) -> dict[str, _OfficialNode]:
        title = next(
            node
            for node in self.root.descendants()
            if node.tag == "h1" and "name" in node.attrs.get("class", "")
        )
        price = next(
            node
            for node in self.root.descendants()
            if node.tag == "p" and "sale-price" in node.attrs.get("class", "")
        )
        capacity = next(
            node
            for node in self.options("capacity")
            if "sku-module_item--checked" in node.attrs.get("class", "")
        )
        color = next(
            node
            for node in self.options("color")
            if "sku-module_item--checked" in node.attrs.get("class", "")
        )
        return {"title": title, "price": price, "capacity": capacity, "color": color}

    def visible_and_unobscured(self, node: _OfficialNode) -> bool:
        box = self.dom_rect(node)
        if box is None or box["y"] < 0 or box["y"] + box["height"] > self.viewport_height:
            return False
        return self.blocker is None or not (
            self.blocker[0] <= box["y"] + box["height"] / 2 <= self.blocker[1]
        )


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


def _set_cards(page: _VivoPage, cards: tuple[tuple[str, str], ...]) -> None:
    late = next(
        node
        for node in page.root.descendants()
        if "late-result" in node.attrs.get("class", "").split()
    )
    late.parent.children = [late]
    for index, (title, href) in enumerate(cards):
        card = _OfficialNode(
            "div",
            {
                "class": "result-card",
                "data-position": str(index),
                "data-skuid": str(200000 + index),
            },
            late.parent,
        )
        image = _OfficialNode("img", {"class": "result-pic"}, card)
        title_node = _OfficialNode("p", {"class": "result-title"}, card)
        title_node.text_parts = [title]
        price = _OfficialNode("p", {"class": "result-price"}, card)
        new = _OfficialNode("span", {"class": "result-price-new"}, price)
        new.text_parts = ["4499"]
        old = _OfficialNode("span", {"class": "result-price-old"}, price)
        old.text_parts = ["4699"]
        link = _OfficialNode("a", {"target": "_blank", "href": href}, card)
        price.children.extend((new, old))
        card.children.extend((image, title_node, price, link))
        late.parent.children.append(card)


def test_vivo_fixture_matches_2026_08_12_readonly_live_search_and_detail_evidence() -> None:
    search = (_FIXTURES / "search_results.html").read_text()
    detail = (_FIXTURES / "detail_normal.html").read_text()
    assert _SEARCH in search and "data-position" in search and "data-skuid" in search
    assert (
        'img class="result-pic"' in search
        and 'p class="result-title"' in search
        and "result-price-new" in search
    )
    assert (
        "product.d7d3a960.js" in detail
        and 'section class="base-info section_wrapper"' in detail
        and 'h1 class="name"' in detail
    )
    page = _VivoPage()
    assert (
        page.locator("div.result-card a[target=_blank]").count() > 0
        and page.locator("p.sale-price").count() == 1
    )


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
    )
    assert page.option_clicks == ["capacity", "color"] and page.events.index(
        "selected:capacity"
    ) < page.events.index("selected:color")


def test_vivo_waits_multiple_rounds_for_late_exact_card_instead_of_no_model() -> None:
    page = _VivoPage()
    _set_cards(page, (("vivo X200 Pro", "https://shop.vivo.com.cn/product/10010281?skuId=135001"),))
    page.late_after_waits = 3
    result = _adapter().observe(_task(), page)
    assert (
        result.outcome is BusinessOutcome.PRICE_FOUND
        and page.waits >= 3
        and result.url.endswith("/product/10010286?skuId=135005")
    )


def test_vivo_visible_terminal_no_goods_after_bounded_wait_is_legal_no() -> None:
    page = _VivoPage()
    _set_cards(
        page, (("vivo X200 Ultra", "https://shop.vivo.com.cn/product/10010281?skuId=135001"),)
    )
    page.empty_after_waits = 3
    result = _adapter().observe(_task(), page)
    assert (
        result.outcome is BusinessOutcome.NO_MODEL
        and page.waits >= 3
        and page.locator("div.no-goods").is_visible()
    )


@pytest.mark.parametrize("model", ["iQOO 15", "i QOO 15"])
def test_vivo_rejects_iqoo_before_any_visit(model: str) -> None:
    page = _VivoPage()
    adapter = _adapter()
    with pytest.raises(Exception, match="iQOO|不支持|unsupported"):
        adapter.observe(_task(model), page)
    assert page.goto_calls == [] and page.fill_calls == []


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
    page.empty_after_waits = 1
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
    with pytest.raises(Exception):
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


def test_vivo_does_not_reclick_uniquely_selected_targets_and_revalidates_final_detail() -> None:
    page = _VivoPage()
    for group, target in (("capacity", "12GB+256GB"), ("color", "辰夜黑")):
        next(node for node in page.options(group) if node.text == target).attrs["class"] += (
            " sku-module_item--checked"
        )
    assert (
        _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
        and page.option_clicks == []
    )
    for drift in ("https://shop.vivo.com.cn/product/999999?skuId=9",):
        changed = _VivoPage()
        changed.redirect_url = drift
        with pytest.raises(Exception):
            _adapter().observe(_task(), changed)


def test_vivo_title_drift_and_unstable_offer_are_technical_failures() -> None:
    title = _VivoPage()
    title.title_override = "vivo X200 Pro"
    with pytest.raises(Exception):
        _adapter().observe(_task(), title)
    unstable = _VivoPage()
    unstable.unstable_prices = True
    with pytest.raises(Exception):
        _adapter().observe(_task(), unstable)


@pytest.mark.parametrize(
    ("viewport", "blocker", "scale", "scroll"),
    [(800.0, None, [], []), (330.0, None, [0.8], []), (300.0, None, [0.8], [70.0])],
)
def test_vivo_capture_reads_real_four_proofs_from_domrects(
    viewport: float, blocker: tuple[float, float] | None, scale: list[float], scroll: list[float]
) -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = viewport
    page.blocker = blocker
    adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert (
        page.capture_proofs()["price"].text.endswith("4399")
        and page.capture_scales == scale
        and page.light_scrolls == scroll
    )


def test_vivo_capture_overlay_or_disappearing_proof_fails_closed_after_one_attempt() -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.viewport_height = 300
    page.blocker = (340, 390)
    with pytest.raises(Exception):
        adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert page.capture_scales == [0.8] and len(page.light_scrolls) == 1
    gone = _VivoPage()
    second = _adapter()
    state = second.observe(_task(), gone)
    gone.remove_color_on_refresh = True
    with pytest.raises(Exception):
        second.prepare_capture_view(_task(), gone, state.semantic_state)

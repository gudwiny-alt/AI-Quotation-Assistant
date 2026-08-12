from __future__ import annotations

import importlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.evidence.quality import CaptureQualityError
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
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
        self.page._url = _SEARCH
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
        self.events: list[str] = []
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
        if self.active != "results":
            raise AssertionError("only result settlement may wait")
        self.wait_timeout_milliseconds.append(milliseconds)
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
        if self.active != "detail" or (self.remove_color_on_refresh and node.text == "辰夜黑"):
            return None
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
        if "data-quotation-capture-scale-original" in script:
            if argument is not None and self.capture_scale != float(argument):
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "window.scrollBy" in script:
            if (
                not isinstance(argument, dict)
                or "delta" not in argument
                or "proofs" not in argument
            ):
                raise AssertionError("geometry scroll requires delta plus proof identities")
            delta = float(argument["delta"])
            geometry = self.evaluate("getBoundingClientRect", argument["proofs"])
            expected = max(0.0, geometry["unionBottom"] - geometry["viewportHeight"] + 8.0)
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
            raise AssertionError("capture adapter must provide four explicit proof identities")
        proofs = self.proofs_for_identities(argument)
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

    def capture_proofs(self) -> dict[str, _OfficialNode]:
        title = next(
            node
            for node in self.detail_root.descendants()
            if node.tag == "h1" and "name" in node.attrs.get("class", "")
        )
        price = next(
            node
            for node in self.detail_root.descendants()
            if node.tag == "p" and "sale-price" in node.attrs.get("class", "")
        )
        return {
            "title": title,
            "price": price,
            "capacity": next(
                node
                for node in self.options("capacity")
                if "sku-module_item--checked" in node.attrs.get("class", "")
            ),
            "color": next(
                node
                for node in self.options("color")
                if "sku-module_item--checked" in node.attrs.get("class", "")
            ),
        }

    def proofs_for_identities(self, identities: dict[str, object]) -> dict[str, _OfficialNode]:
        proofs = self.capture_proofs()
        expected = {
            "title": "h1.name",
            "price": "p.sale-price:¥4399",
            "capacity": "li.spec_item.checked:12GB+256GB",
            "color": "li.spec_item.checked:辰夜黑",
        }
        if identities != expected:
            raise AssertionError("capture proofs must bind exact live fixture nodes")
        if proofs["title"].tag != "h1" or "name" not in proofs["title"].attrs.get("class", ""):
            raise AssertionError("title proof mismatch")
        if proofs["price"].text != "4399":
            raise AssertionError("adopted price proof mismatch")
        if proofs["capacity"].text != "12GB+256GB" or proofs["color"].text != "辰夜黑":
            raise AssertionError("selected configuration proof mismatch")
        return proofs

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
    container.children = [late] if retain_late else []
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
        and 3 <= page.waits <= 8
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
    assert page.capture_proofs()["price"].text.endswith("4399") and page.capture_scales == scale
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

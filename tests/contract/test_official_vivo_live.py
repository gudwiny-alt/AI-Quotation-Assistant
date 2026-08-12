from __future__ import annotations

import importlib
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from tests.conftest import _OfficialFixturePage, _OfficialLocator, _OfficialNode, _official_select

_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/vivo"


class _VivoLocator(_OfficialLocator):
    """Test seam; the persisted fixture has only public-vivo shaped DOM."""

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
        super().fill(value)
        self.page.fill_calls.append(value)

    def click(self) -> None:
        node = self.nodes[0]
        if node.tag == "a" and "/product/" in node.attrs.get("href", ""):
            self.page.entered_href = node.attrs["href"]
            self.page.goto(self.page.absolute_url(node.attrs["href"]))
            return
        group = self.page.group_for(node)
        if group is not None:
            if "spec_item--disabled" in node.attrs.get("class", ""):
                raise AssertionError("disabled vivo configuration was clicked")
            for candidate in self.page.options(group):
                candidate.attrs["class"] = candidate.attrs.get("class", "").replace(
                    " sku-module_item--checked", ""
                )
            node.attrs["class"] = f"{node.attrs.get('class', '')} sku-module_item--checked".strip()
            self.page.option_clicks.append(group)
            self.page.events.append(f"selected:{group}")
            return
        super().click()

    def inner_text(self) -> str:
        node = self.nodes[0]
        if "sale-price-value" in node.attrs.get("class", ""):
            return self.page.price_for_current_poll(node)
        return super().inner_text()

    def bounding_box(self) -> dict[str, float] | None:
        box = super().bounding_box()
        if box is None:
            return None
        return self.page.scaled_box(self.nodes[0], box)


class _VivoPage(_OfficialFixturePage):
    """DOM-backed state: synthetic dynamics are deliberately outside HTML."""

    def __init__(self, detail: str = "detail_normal.html") -> None:
        super().__init__(
            (_FIXTURES / "search_results.html").read_text() + (_FIXTURES / detail).read_text(),
            entry_url="https://shop.vivo.com.cn/",
        )
        self.active = "results"
        self.fill_calls: list[str] = []
        self.events: list[str] = []
        self.entered_href: str | None = None
        self.results_settled = False
        self.reveal_late_on_wait = False
        self.price_poll = 0
        self.unstable_price = False
        self.capture_scale = 1.0
        self.capture_scales: list[float] = []
        self.light_scrolls: list[float] = []
        self.capture_failure = False
        self.proof_reads: list[tuple[str, str]] = []
        self.fit_mode = "fit"  # fit | scale | scroll | blocked
        self.overlay_role: str | None = None

    def absolute_url(self, href: str) -> str:
        return href if href.startswith("https://") else f"https://shop.vivo.com.cn{href}"

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)
        self._url = url
        self.active = (
            "detail"
            if re.fullmatch(r"https://shop\.vivo\.com\.cn/product/\d+(?:\?.*)?", url)
            else "results"
        )

    def locator(self, selector: str) -> _VivoLocator:
        return _VivoLocator(self, _official_select(self.root.descendants(), selector))

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        if self.reveal_late_on_wait and not self.results_settled:
            late = next(
                node
                for node in self.root.descendants()
                if "late-result" in node.attrs.get("class", "").split()
            )
            late.attrs.pop("hidden", None)
            self.results_settled = True

    def group_for(self, node: _OfficialNode) -> str | None:
        return (
            "capacity"
            if self.under_labeled(node, "版本")
            else "color"
            if self.under_labeled(node, "颜色")
            else None
        )

    def under_labeled(self, node: _OfficialNode, label: str) -> bool:
        # True vivo shape: dl > dt.spec_title + dd.sku-module_content > ul > li.
        parent = node.parent
        while parent is not None and parent.tag != "dl":
            parent = parent.parent
        if parent is None:
            return False
        return any(child.tag == "dt" and child.text == label for child in parent.children)

    def options(self, group: str) -> list[_OfficialNode]:
        label = "版本" if group == "capacity" else "颜色"
        module = next(
            node
            for node in self.root.descendants()
            if node.tag == "dl" and "sku-module" in node.attrs.get("class", "")
        )
        children = module.children
        start = next(
            index for index, node in enumerate(children) if node.tag == "dt" and node.text == label
        )
        content = children[start + 1]
        return [
            node
            for node in content.descendants()
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

    def price_for_current_poll(self, node: _OfficialNode) -> str:
        self.events.append(f"price:{self.selected('capacity')}:{self.selected('color')}")
        if self.selected("capacity") != "12GB+256GB" or self.selected("color") != "辰夜黑":
            return ""
        # One complete offer snapshot per poll: later implementation must wait.
        values = ("4499", "4399", "4399")
        value = (
            values[min(self.price_poll, len(values) - 1)]
            if not self.unstable_price
            else ("4499", "4399", "4599", "4299")[self.price_poll % 4]
        )
        self.price_poll += 1
        return value if node.text == "4499" else ""

    def scaled_box(self, node: _OfficialNode, box: dict[str, float]) -> dict[str, float]:
        return {
            key: value * self.capture_scale if key in {"x", "y", "width", "height"} else value
            for key, value in box.items()
        }

    def evaluate(self, script: str, argument: object = None) -> object:
        if "data-quotation-capture-scale-original" in script:
            if argument is not None and self.capture_scale != float(argument):
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "elementFromPoint" in script and "__vivoProof" in script:
            roles = ("title", "price", "capacity", "color")
            self.proof_reads.extend((role, self.proof_text(role)) for role in roles)
            if self.overlay_role is not None:
                return False
            if self.fit_mode == "fit":
                return True
            if self.fit_mode == "scale":
                return self.capture_scale == 0.8
            if self.fit_mode == "scroll":
                return bool(self.light_scrolls)
            return False
        if "__vivoProofGeometry" in script:
            return {
                "unionTop": 88.0,
                "unionBottom": 870.0,
                "viewportHeight": 800.0,
                "scrollY": 120.0,
                "blockerTop": 650.0 if self.overlay_role else None,
            }
        if "__vivoApplyLightScroll" in script:
            delta = float(argument)
            assert self.capture_scale == 0.8 and 70 <= delta <= 120
            self.light_scrolls.append(delta)
            self.overlay_role = None
            return True
        if "window.scrollTo" in script:
            raise AssertionError("vivo capture must use one bounded geometry scroll")
        raise AssertionError(f"unexpected vivo script: {script[:100]}")

    def proof_text(self, role: str) -> str:
        if role == "title":
            return "vivo X200"
        if role == "price":
            return "¥4399"
        return self.selected("capacity" if role == "capacity" else "color") or ""


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
    listing = next(
        node
        for node in page.root.descendants()
        if node.tag == "ul" and "spu-item-list" in node.attrs.get("class", "")
    )
    late = next(node for node in listing.children if "late-result" in node.attrs.get("class", ""))
    listing.children = [late]
    for title, href in cards:
        item = _OfficialNode("li", {"class": "spu-item"}, listing)
        link = _OfficialNode("a", {"target": "_blank", "href": href, "title": title}, item)
        figure = _OfficialNode("div", {"class": "figure"}, link)
        info = _OfficialNode("div", {"class": "spu-info"}, link)
        name = _OfficialNode("p", {"class": "name"}, info)
        name.text_parts = [title]
        info.children.append(name)
        link.children.extend((figure, info))
        item.children.append(link)
        listing.children.append(item)


def test_vivo_fixture_is_provenanced_real_public_chunk_shape_and_collects_without_adapter() -> None:
    html = (_FIXTURES / "search_results.html").read_text()
    assert "productlist.1495b354.js" in html and "2026-08-12" in html
    page = _VivoPage()
    assert page.locator("ul.spu-item-list li.spu-item a[target=_blank]").count() > 0
    assert page.locator("div.summary div.summary_price p.sale-price").count() == 1
    assert page.locator("dl.sku-module.specs dt.sku-module_title.spec_title").count() == 2


def test_vivo_quotes_stable_lower_selected_offer_and_enters_observed_numeric_detail_url() -> None:
    page = _VivoPage()
    observation = _adapter().observe(_task(), page)
    assert observation.outcome is BusinessOutcome.PRICE_FOUND and observation.price == Decimal(
        "4399"
    )
    assert observation.url.endswith("/product/1000200?skuId=100200") and page.url == observation.url
    assert page.option_clicks == ["capacity", "color"]
    assert (
        page.events.index("selected:capacity")
        < page.events.index("selected:color")
        < next(i for i, value in enumerate(page.events) if value == "price:12GB+256GB:辰夜黑")
    )


def test_vivo_late_real_card_is_not_no_model_before_results_settle() -> None:
    page = _VivoPage()
    _set_cards(page, (("vivo X200 Pro", "/product/1000199?skuId=100199"),))
    page.reveal_late_on_wait = True
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
    assert page.results_settled and page.url.endswith("/product/1000203?skuId=100203")


def test_vivo_settled_no_goods_is_legal_no_with_real_empty_result_wording() -> None:
    page = _VivoPage()
    _set_cards(page, (("vivo X200 Ultra", "/product/1000199?skuId=100199"),))
    page.results_settled = True
    assert "没有找到符合条件的商品，试试其他筛选条件吧" in page.locator("div.no-goods").inner_text()
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in result.css_rectangles) == ("search_keyword", "result_region")


@pytest.mark.parametrize("model", ["iQOO 15", "iqoo 15", "i QOO 15"])
def test_vivo_marks_iqoo_variants_unsupported_without_assuming_exception_type(model: str) -> None:
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
def test_vivo_excludes_every_derived_base_model(name: str) -> None:
    page = _VivoPage()
    _set_cards(page, ((name, "/product/1000199?skuId=100199"),))
    page.results_settled = True
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.NO_MODEL


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


def test_vivo_unstable_whole_offer_polls_fail_closed() -> None:
    page = _VivoPage()
    page.unstable_price = True
    adapter = _adapter()
    with pytest.raises(Exception, match="price|stable|settle|报价"):
        adapter.observe(_task(), page)
    assert page.price_poll >= 3


@pytest.mark.parametrize(
    ("mode", "scale", "scroll"), [("fit", [], []), ("scale", [0.8], []), ("scroll", [0.8], [70.0])]
)
def test_vivo_capture_uses_adopted_four_proofs_and_at_most_one_geometry_scroll(
    mode: str, scale: list[float], scroll: list[float]
) -> None:
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.fit_mode = mode
    adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert page.proof_reads[-4:] == [
        ("title", "vivo X200"),
        ("price", "¥4399"),
        ("capacity", "12GB+256GB"),
        ("color", "辰夜黑"),
    ]
    assert page.capture_scales == scale and page.light_scrolls == scroll


def test_vivo_capture_overlay_or_vanished_proof_fails_closed_and_capture_failure_keeps_price() -> (
    None
):
    page = _VivoPage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.fit_mode = "blocked"
    page.overlay_role = "color"
    with pytest.raises(Exception):
        adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert len(page.light_scrolls) <= 1 and observation.price == Decimal("4399")

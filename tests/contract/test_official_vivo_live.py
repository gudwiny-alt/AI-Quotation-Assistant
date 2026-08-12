from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.sites.official_brands.factory import create_official_adapter
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from quote_app.tasks.retry import LayoutRecognitionError, NonRetryableTechnicalError
from tests.conftest import _OfficialFixturePage, _OfficialLocator, _OfficialNode, _official_select

# The independent vivo module is intentionally absent while this contract is RED.
from quote_app.sites.official_brands.vivo import (  # type: ignore[import-not-found]
    VivoOfficialAdapter,
    VivoSearchCardSelectionError,
)


_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/vivo"


class _VivoLocator(_OfficialLocator):
    def nth(self, index: int) -> _VivoLocator:
        return _VivoLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _VivoLocator:
        matches: list[_OfficialNode] = []
        for node in self.nodes:
            matches.extend(_official_select(node.descendants(), selector))
        return _VivoLocator(self.page, matches)

    def fill(self, value: str) -> None:
        super().fill(value)
        self.page.fill_calls.append(value)
        for node in self.page.root.descendants():
            if node.attrs.get("data-vivo-role") == "search-input":
                node.attrs["value"] = value

    def click(self) -> None:
        node = self.nodes[0]
        if node.attrs.get("data-action") == "open-search":
            self.page.activate("search")
            self.page.search_waiting_for_enter = True
            return
        super().click()

    def evaluate(self, script: str) -> object:
        if "__vivoExplicitSelection" in script:
            node = self.nodes[0]
            return node.attrs.get("aria-selected") == "true"
        return super().evaluate(script)


class _VivoFixturePage(_OfficialFixturePage):
    def __init__(self, detail_fixture: str = "detail_normal.html") -> None:
        html = (
            (_FIXTURES / "search_results.html").read_text(encoding="utf-8")
            + (_FIXTURES / detail_fixture).read_text(encoding="utf-8")
        )
        super().__init__(html, entry_url="https://shop.vivo.com.cn/")
        self.fill_calls: list[str] = []
        self.capture_scale = 1.0
        self.capture_scales: list[float] = []
        self.proofs_fit = True
        self.proofs_fit_after_scale = True
        self.proofs_fit_after_position = True
        self.proof_occluded = False
        self.proof_occluded_after_position = False
        self.position_attempts = 0
        self.position_deltas: list[float] = []
        self.proof_union_top = 110.0
        self.proof_union_bottom = 438.0
        self.viewport_height = 800.0
        self.scroll_y = 0.0

    def locator(self, selector: str) -> _VivoLocator:
        screens = [
            node
            for node in self.root.descendants()
            if node.attrs.get("data-screen") == self._active
        ]
        scope = screens[0].descendants() + screens if screens else self.root.descendants()
        return _VivoLocator(self, _official_select(scope, selector))

    def evaluate(self, script: str, argument: object = None) -> object:
        if "data-quotation-capture-scale-original" in script:
            if argument is not None:
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "__vivoProofsFitCurrentViewport" in script:
            if "__vivoProofsUnoccluded" in script and self.proof_occluded:
                return False
            return self.proofs_fit if self.capture_scale == 1.0 else self.proofs_fit_after_scale
        if "__vivoProofPositionState" in script:
            return {
                "unionTop": self.proof_union_top,
                "unionBottom": self.proof_union_bottom,
                "viewportHeight": self.viewport_height,
                "scrollY": self.scroll_y,
                "occlusions": [] if not self.proof_occluded else [{"proofBottom": 438.0, "blockerTop": 400.0}],
            }
        if "__vivoApplyBoundedProofPosition" in script:
            self.position_attempts += 1
            self.position_deltas.append(float(argument))
            self.proofs_fit = self.proofs_fit_after_position
            self.proofs_fit_after_scale = self.proofs_fit_after_position
            self.proof_occluded = self.proof_occluded_after_position
            return True
        if "window.scrollTo" in script:
            raise AssertionError("vivo capture may only use the bounded proof-position operation")
        raise AssertionError(f"unexpected vivo fixture evaluate: {script[:80]}")


def _spec() -> Any:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "维沃" and spec.channel is WebsiteChannel.OFFICIAL
    )


def _task(*, model_name: str = "vivo X200", color: str = "辰夜黑") -> WebsiteTask:
    return WebsiteTask(
        task_id=f"vivo-{model_name}-{color}",
        run_id="run-vivo-live-contract",
        source_row_number=2,
        output_row_number=2,
        material_code="VIVO-LIVE-CONTRACT",
        brand="维沃",
        model_name=model_name,
        ram="12GB",
        storage="256GB",
        color=color,
        channel=WebsiteChannel.OFFICIAL,
    )


def _adapter() -> VivoOfficialAdapter:
    adapter = create_official_adapter(_spec())
    assert isinstance(adapter, VivoOfficialAdapter)
    return adapter


def _result_region(page: _VivoFixturePage) -> _OfficialNode:
    return next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-vivo-role") == "result-region"
    )


def _set_result_cards(page: _VivoFixturePage, cards: tuple[tuple[str, str, str], ...]) -> None:
    region = _result_region(page)
    region.children.clear()
    page._product_urls.clear()
    for title, href, note in cards:
        card = _OfficialNode("article", {"data-vivo-role": "result-card", "class": "product-card"}, region)
        link = _OfficialNode("a", {"data-vivo-role": "product-link", "href": href}, card)
        label = _OfficialNode("span", {"data-vivo-role": "product-title"}, link)
        label.text_parts = [title]
        link.children.append(label)
        if note:
            stock = _OfficialNode("span", {"data-vivo-role": "stock-note"}, link)
            stock.text_parts = [note]
            link.children.append(stock)
        card.children.append(link)
        region.children.append(card)
        if href.startswith("/product/"):
            page._product_urls.add(f"https://shop.vivo.com.cn{href}")


def test_vivo_exact_base_model_quotes_lowest_main_price_after_capacity_then_color() -> None:
    page = _VivoFixturePage()

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499")
    assert observation.url == "https://shop.vivo.com.cn/product/1000200?skuId=100020011"
    assert page.option_clicks == ["capacity", "color"]


def test_vivo_rejects_iqoo_before_any_official_navigation() -> None:
    page = _VivoFixturePage()

    with pytest.raises(NonRetryableTechnicalError, match="iQOO"):
        _adapter().observe(_task(model_name="iQOO 15"), page)

    assert page.goto_calls == []
    assert page.fill_calls == []


def test_vivo_excludes_derived_models_and_enters_card_despite_card_color_difference() -> None:
    page = _VivoFixturePage()
    _set_result_cards(
        page,
        (
            ("vivo X200 Pro 12GB+256GB 辰夜黑", "/product/1000199", ""),
            ("vivo X200 16GB+512GB 宝石蓝", "/product/1000200?skuId=100020011", ""),
            ("vivo X200S 12GB+256GB 辰夜黑", "/product/1000202", ""),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1] == "https://shop.vivo.com.cn/product/1000200?skuId=100020011"


def test_vivo_enters_a_sold_out_exact_card_to_select_available_configuration() -> None:
    page = _VivoFixturePage()
    _set_result_cards(
        page,
        (("vivo X200 16GB+512GB 宝石蓝", "/product/1000200?skuId=100020011", "暂时缺货"),),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/product/1000200?skuId=100020011")


def test_vivo_skips_first_invalid_exact_url_and_uses_later_numeric_product_url() -> None:
    page = _VivoFixturePage()
    _set_result_cards(
        page,
        (
            ("vivo X200 12GB+256GB 辰夜黑", "/product/x200-topic", ""),
            ("vivo X200 16GB+512GB 宝石蓝", "/product/1000200?skuId=100020011", ""),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/product/1000200?skuId=100020011")


def test_vivo_fails_technically_when_every_exact_card_url_is_invalid() -> None:
    page = _VivoFixturePage()
    _set_result_cards(
        page,
        (
            ("vivo X200 12GB+256GB 辰夜黑", "/product/x200-topic", ""),
            ("vivo X200 16GB+512GB 宝石蓝", "https://evil.example/product/1000200", ""),
        ),
    )

    with pytest.raises(VivoSearchCardSelectionError, match="approved numeric"):
        _adapter().observe(_task(), page)


def test_vivo_no_model_is_only_legal_after_search_wait_and_keeps_keyword_and_region_evidence() -> None:
    page = _VivoFixturePage()
    _set_result_cards(page, (("vivo X200 Pro 12GB+256GB 辰夜黑", "/product/1000199", ""),))

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == ("search_keyword", "result_region")
    assert page.wait_timeout_milliseconds


def test_vivo_missing_capacity_is_legal_no_with_capacity_evidence() -> None:
    observation = _adapter().observe(_task(), _VivoFixturePage("detail_missing_capacity.html"))

    assert observation.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE
    assert tuple(rect.role for rect in observation.css_rectangles) == ("capacity",)


def test_vivo_missing_color_after_capacity_is_legal_no_with_color_evidence() -> None:
    page = _VivoFixturePage("detail_missing_color.html")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.COLOR_UNAVAILABLE
    assert tuple(rect.role for rect in observation.css_rectangles) == ("color",)
    assert page.option_clicks == ["capacity"]


def test_vivo_capture_keeps_four_business_proofs_in_place_when_already_visible() -> None:
    adapter = _adapter()
    page = _VivoFixturePage()
    observation = adapter.observe(_task(), page)

    adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.capture_scales == []
    assert page.position_attempts == 0


def test_vivo_capture_switches_to_eighty_percent_without_scroll_when_that_makes_four_proofs_fit() -> None:
    adapter = _adapter()
    page = _VivoFixturePage()
    observation = adapter.observe(_task(), page)
    page.proofs_fit = False
    page.proofs_fit_after_scale = True

    adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.capture_scales == [0.8]
    assert page.position_attempts == 0


def test_vivo_capture_positions_once_when_a_floating_layer_occludes_one_proof() -> None:
    adapter = _adapter()
    page = _VivoFixturePage()
    observation = adapter.observe(_task(), page)
    page.proof_occluded = True
    page.proof_occluded_after_position = False

    adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.position_attempts == 1
    assert len(page.position_deltas) == 1


def test_vivo_capture_fails_closed_after_its_single_bounded_position_attempt() -> None:
    adapter = _adapter()
    page = _VivoFixturePage()
    observation = adapter.observe(_task(), page)
    page.proof_occluded = True
    page.proof_occluded_after_position = True

    with pytest.raises(LayoutRecognitionError, match="title, price, capacity and color must fit"):
        adapter.prepare_capture_view(_task(), page, observation.semantic_state)

    assert page.position_attempts == 1

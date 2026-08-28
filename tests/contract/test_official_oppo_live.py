from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.sites.official_brands.factory import create_official_adapter
from quote_app.sites.official_brands.models import OfficialCaptureView
from quote_app.sites.official_brands.oppo import (
    OppoOfficialAdapter,
    OppoSearchCardSelectionError,
)
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import LayoutRecognitionError
from tests.conftest import (
    _OfficialFixturePage,
    _OfficialLocator,
    _OfficialNode,
    _official_select,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/oppo"


class _OppoLocator(_OfficialLocator):
    def nth(self, index: int) -> _OppoLocator:
        return _OppoLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _OppoLocator:
        matches = []
        for node in self.nodes:
            matches.extend(_official_select(node.descendants(), selector))
        return _OppoLocator(self.page, matches)

    def element_handle(self) -> _OppoLocator:
        return self

    def inner_text(self) -> str:
        node = self.nodes[0]
        if (
            self.page._active == "product"
            and (
                node.attrs.get("data-oppo-role")
                in {"detail-title", "price"}
                or node.attrs.get("data-option-kind") in {"capacity", "color"}
            )
        ):
            self.page.detail_read_scales.append(self.page.capture_scale)
        return super().inner_text()

    def evaluate(self, script: str) -> object:
        if "__oppoExplicitSelection" in script:
            node = self.nodes[0]
            classes = set(node.attrs.get("class", "").lower().split())
            return "active-btn" in classes or node.attrs.get("aria-selected") == "true"
        return super().evaluate(script)

    def fill(self, value: str) -> None:
        super().fill(value)
        if self.nodes[0].attrs.get("data-oppo-role") != "search-input":
            return
        results = next(
            node
            for node in self.page.root.descendants()
            if node.attrs.get("data-screen") == "results"
        )
        for node in results.descendants():
            if node.attrs.get("data-oppo-role") == "search-input":
                node.attrs["value"] = value

    def click(self) -> None:
        node = self.nodes[0]
        option_kind = node.attrs.get("data-option-kind")
        if option_kind in self.page.blocked_option_click_kinds:
            raise RuntimeError(f"OPPO {option_kind} click is covered by QR overlay")
        if node.attrs.get("data-action") == "open-search":
            self.page.activate("search")
            self.page.search_waiting_for_enter = True
            return
        super().click()


class _OppoFixturePage(_OfficialFixturePage):
    def __init__(self, fixture: str) -> None:
        super().__init__((_FIXTURES / fixture).read_text(encoding="utf-8"), entry_url="https://www.opposhop.cn/cn/web/")
        self.capture_scale = 1.0
        self.capture_scale_original: float | None = None
        self.capture_scales: list[float] = []
        self.detail_read_scales: list[float] = []
        self.proofs_fit = True
        self.proofs_fit_after_scale = True
        self.proofs_fit_after_position = True
        self.capacity_occluded = False
        self.capacity_occluded_after_position = False
        self.occluded_proof_index: int | None = None
        self.occluded_proof_index_after_position: int | None = None
        self.position_attempts = 0
        self.position_deltas: list[float] = []
        self.proof_union_top = 180.0
        self.proof_union_bottom = 720.0
        self.capacity_bottom = 660.0
        self.blocker_top = 580.0
        self.viewport_height = 800.0
        self.scroll_y = 100.0
        self.blocked_option_click_kinds: set[str] = set()
        self._product_urls = {
            node.attrs["href"]
            for node in self.root.descendants()
            if "/cn/web/products/" in node.attrs.get("href", "")
        }

    def goto(self, url: str, **kwargs: object) -> None:
        if url in self._product_urls:
            self.goto_calls.append(url)
            self._url = url
            self.activate("product")
            return
        super().goto(url, **kwargs)

    def locator(self, selector: str) -> _OppoLocator:
        screens = [
            node
            for node in self.root.descendants()
            if node.attrs.get("data-screen") == self._active
        ]
        scope = screens[0].descendants() + screens if screens else self.root.descendants()
        return _OppoLocator(self, _official_select(scope, selector))

    def evaluate(self, script: str, argument: object = None) -> object:
        if (
            "data-quotation-capture-scale-original" in script
            or ("inlineZoom" in script and "computedZoom" in script)
        ):
            if "removeAttribute" in script:
                if self.capture_scale_original is not None:
                    self.capture_scale = self.capture_scale_original
                    self.capture_scale_original = None
                return True
            if argument is not None:
                if self.capture_scale_original is None:
                    self.capture_scale_original = self.capture_scale
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "__oppoProofsFitCurrentViewport" in script:
            fits = (
                self.proofs_fit
                if self.capture_scale == 1.0
                else self.proofs_fit_after_scale
            )
            if "__oppoProofsUnoccluded" in script and (
                self.capacity_occluded or self.occluded_proof_index is not None
            ):
                return False
            return fits
        if "__oppoProofPositionState" in script:
            state: dict[str, object] = {
                "unionTop": self.proof_union_top,
                "unionBottom": self.proof_union_bottom,
                "capacityBottom": self.capacity_bottom,
                "blockerTop": self.blocker_top if self.capacity_occluded else None,
                "capacityOccluded": self.capacity_occluded,
                "viewportHeight": self.viewport_height,
                "scrollY": self.scroll_y,
            }
            if "__oppoAllProofOcclusions" in script:
                proof_bottoms = (300.0, 360.0, self.capacity_bottom, 620.0)
                occluded_index = (
                    2 if self.capacity_occluded else self.occluded_proof_index
                )
                state["occlusions"] = (
                    [] if occluded_index is None else [{
                        "proofBottom": proof_bottoms[occluded_index],
                        "blockerTop": self.blocker_top,
                    }]
                )
            return state
        if "__oppoApplyBoundedProofPosition" in script:
            self.position_attempts += 1
            self.position_deltas.append(float(argument))
            self.proofs_fit = self.proofs_fit_after_position
            self.proofs_fit_after_scale = self.proofs_fit_after_position
            self.capacity_occluded = self.capacity_occluded_after_position
            self.occluded_proof_index = self.occluded_proof_index_after_position
            return True
        if "window.scrollTo" in script:
            raise AssertionError("legacy broad OPPO proof positioning is forbidden")
        raise AssertionError(f"unexpected OPPO fixture evaluate: {script[:80]}")


class _OppoResultPageWithClearedSearchInput(_OppoFixturePage):
    def press(self, key: str) -> None:
        super().press(key)
        if key != "Enter":
            return
        for node in self.root.descendants():
            if node.attrs.get("data-oppo-role") == "search-input":
                node.attrs["value"] = ""


class _OppoNoModelPageWithVisibleQuery(
    _OppoResultPageWithClearedSearchInput
):
    def __init__(self) -> None:
        super().__init__("no_model.html")
        results = next(
            node
            for node in self.root.descendants()
            if node.attrs.get("data-screen") == "results"
        )
        dialog = next(
            node
            for node in results.descendants()
            if node.attrs.get("data-oppo-role") == "search-dialog"
        )
        query = _OfficialNode(
            "div",
            {
                "data-oppo-role": "search-query",
                "style": "left:20px;top:20px;width:280px;height:36px",
            },
            dialog,
        )
        query.text_parts = ["OPPO A6 5G"]
        dialog.children.insert(1, query)
        empty_result = next(
            node
            for node in results.descendants()
            if node.attrs.get("data-oppo-role") == "empty-results"
        )
        empty_result.text_parts = ["没有更多了"]


def _spec() -> Any:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "欧珀" and spec.channel is WebsiteChannel.OFFICIAL
    )


def _oppo_task(
    *,
    model_name: str,
    ram: str,
    storage: str,
    color: str,
) -> WebsiteTask:
    return WebsiteTask(
        task_id=f"oppo-{model_name}-{ram}-{storage}-{color}",
        run_id="run-oppo-model-only",
        source_row_number=2,
        output_row_number=2,
        material_code="OPPO-MODEL-ONLY",
        brand="欧珀",
        model_name=model_name,
        ram=ram,
        storage=storage,
        color=color,
        channel=WebsiteChannel.OFFICIAL,
    )


def _task() -> WebsiteTask:
    return _oppo_task(
        model_name="OPPO A6 5G",
        ram="12GB",
        storage="256GB",
        color="蓝海浮光",
    )


def _set_result_cards(
    page: _OppoFixturePage,
    cards: tuple[tuple[str, str], ...],
) -> None:
    region = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "results"
    )
    region.children.clear()
    page._product_urls.clear()
    for title, href in cards:
        article = _OfficialNode("article", {"class": "five-item goods-card"}, region)
        link = _OfficialNode(
            "a",
            {"data-oppo-role": "product-link", "class": "goods-card app-card-hover", "href": href},
            article,
        )
        label = _OfficialNode("span", {"data-oppo-role": "product-title"}, link)
        label.text_parts = [title]
        link.children.append(label)
        article.children.append(link)
        region.children.append(article)
        page._product_urls.add(f"https://www.opposhop.cn{href}")


def _set_detail_product(
    page: _OppoFixturePage,
    *,
    model_name: str,
    capacity: str,
    color: str,
) -> None:
    detail_title = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "detail-title"
    )
    detail_title.text_parts = [f"{model_name} {color} {capacity} 官方标配"]
    capacity_options = [
        node for node in page.root.descendants()
        if node.attrs.get("data-option-kind") == "capacity"
    ]
    color_options = [
        node for node in page.root.descendants()
        if node.attrs.get("data-option-kind") == "color"
    ]
    capacity_options[0].text_parts = [capacity]
    color_options[0].text_parts = [color]


def _adapter() -> OppoOfficialAdapter:
    adapter = create_official_adapter(_spec())
    assert isinstance(adapter, OppoOfficialAdapter)
    return adapter


def test_oppo_real_semantic_page_selects_capacity_then_color_and_quotes_lowest_valid_price() -> None:
    page = _OppoFixturePage("normal.html")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("1899")
    assert observation.url == "https://www.opposhop.cn/cn/web/products/32740.html?us=search"
    assert page.option_clicks == ["capacity", "color"]
    assert page.detail_read_scales[0] == 0.8
    assert page.capture_scale == 1.0


def test_oppo_enters_first_exact_model_card_before_selecting_capacity_and_color() -> None:
    page = _OppoFixturePage("normal.html")
    result_titles = [
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "product-title"
    ]
    result_titles[0].text_parts = [
        "OPPO A6 5G 丝绒灰 8GB+256GB 官方标配"
    ]
    result_titles[1].text_parts = [
        "OPPO A6 5G 蓝海浮光 12GB+256GB 官方标配"
    ]

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1] == (
        "https://www.opposhop.cn/cn/web/products/32740.html?us=search"
    )


def test_oppo_a5m_enters_first_exact_model_card_before_selecting_target_color() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
    )
    _set_result_cards(
        page,
        (
            ("OPPO A5m 水晶粉 6GB+128GB", "/cn/web/products/38672.html?us=search"),
            ("OPPO A5m 水晶粉 8GB+256GB", "/cn/web/products/38675.html?us=search"),
        ),
    )
    _set_detail_product(page, model_name="OPPO A5m", capacity="8GB+256GB", color="钻石白")

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/38672.html?us=search")
    assert page.option_clicks == []


def test_oppo_a5m_does_not_reclick_an_already_selected_target_covered_by_qr() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
    )
    _set_result_cards(
        page,
        (("OPPO A5m 水晶粉 8GB+256GB", "/cn/web/products/38675.html?us=search"),),
    )
    _set_detail_product(
        page,
        model_name="OPPO A5m",
        capacity="8GB+256GB",
        color="钻石白",
    )
    page.blocked_option_click_kinds = {"capacity", "color"}

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_clicks == []


def test_oppo_a6t_enters_exact_model_card_and_rejects_neighbor_variants() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A6t",
        ram="6GB",
        storage="128GB",
        color="墨竹黑",
    )
    _set_result_cards(
        page,
        (
            ("OPPO A6 Pro 墨竹黑 6GB+128GB", "/cn/web/products/41950.html?us=search"),
            ("OPPO A6t 青出于蓝 6GB+128GB 官方标配", "/cn/web/products/41956.html?us=search"),
            ("OPPO A6i 墨竹黑 6GB+128GB", "/cn/web/products/41960.html?us=search"),
        ),
    )
    _set_detail_product(page, model_name="OPPO A6t", capacity="6GB+128GB", color="墨竹黑")

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/41956.html?us=search")


def test_oppo_a5m_scales_before_first_detail_business_read() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
    )
    _set_result_cards(
        page,
        (("OPPO A5m 水晶粉 8GB+256GB", "/cn/web/products/38675.html?us=search"),),
    )
    _set_detail_product(
        page,
        model_name="OPPO A5m",
        capacity="8GB+256GB",
        color="钻石白",
    )

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.detail_read_scales[0] == 0.8
    assert page.capture_scales == [0.8]
    assert page.capture_scale == 1.0


def test_oppo_enters_exact_card_when_result_search_input_is_cleared() -> None:
    page = _OppoResultPageWithClearedSearchInput("normal.html")
    task = _oppo_task(
        model_name="OPPO A6t",
        ram="6GB",
        storage="128GB",
        color="墨竹黑",
    )
    _set_result_cards(
        page,
        (("OPPO A6t 青出于蓝 6GB+128GB 官方标配", "/cn/web/products/41956.html?us=search"),),
    )
    _set_detail_product(page, model_name="OPPO A6t", capacity="6GB+128GB", color="墨竹黑")

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/41956.html?us=search")


def test_oppo_skips_invalid_exact_model_url_and_enters_first_later_approved_card() -> None:
    page = _OppoFixturePage("normal.html")
    _set_result_cards(
        page,
        (
            ("OPPO A6 5G 蓝海浮光 12GB+256GB", "/cn/web/topic/32739.html"),
            ("OPPO A6 5G 丝绒灰 8GB+256GB", "/cn/web/products/32740.html?us=search"),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/32740.html?us=search")


def test_oppo_marks_search_selection_failure_when_all_exact_model_urls_are_unapproved() -> None:
    page = _OppoFixturePage("normal.html")
    _set_result_cards(
        page,
        (
            ("OPPO A6 5G 蓝海浮光 12GB+256GB", "/cn/web/topic/32739.html"),
            ("OPPO A6 5G 丝绒灰 8GB+256GB", "https://evil.example/32740.html"),
        ),
    )

    with pytest.raises(
        OppoSearchCardSelectionError,
        match="no approved product URL",
    ):
        _adapter().observe(_task(), page)


def test_oppo_exact_model_sold_out_card_still_enters_detail() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
    )
    _set_result_cards(
        page,
        (("OPPO A5m 水晶粉 8GB+256GB 暂时缺货", "/cn/web/products/38672.html"),),
    )
    _set_detail_product(
        page,
        model_name="OPPO A5m",
        capacity="8GB+256GB",
        color="钻石白",
    )

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/38672.html")


def test_oppo_capture_accepts_exactly_title_price_capacity_and_color_in_one_view() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.detail_read_scales.clear()

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    current = adapter.verified_state_reader(task, page, observation.semantic_state)()

    assert current == observation.semantic_state
    assert page.capture_scales == [0.8, 0.8]
    assert page.capture_scale == 0.8
    assert page.detail_read_scales
    assert set(page.detail_read_scales) == {0.8}
    assert page.position_attempts == 0


def test_oppo_a5m_capture_reapplies_eighty_percent_without_scrolling() -> None:
    adapter = _adapter()
    task = _oppo_task(
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
    )
    page = _OppoFixturePage("normal.html")
    _set_result_cards(
        page,
        (("OPPO A5m 水晶粉 8GB+256GB", "/cn/web/products/38675.html?us=search"),),
    )
    _set_detail_product(
        page,
        model_name="OPPO A5m",
        capacity="8GB+256GB",
        color="钻石白",
    )
    observation = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scales == [0.8, 0.8]
    assert page.capture_scale == 0.8
    assert page.position_attempts == 0


def test_oppo_capture_positions_once_only_when_four_proofs_do_not_fit() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.proofs_fit = False
    page.proofs_fit_after_scale = False
    page.proof_union_bottom = 830.0

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scales == [0.8, 0.8]
    assert page.position_attempts == 1


def test_oppo_capture_fails_after_one_position_when_four_proofs_still_do_not_fit() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.proofs_fit = False
    page.proofs_fit_after_scale = False
    page.proofs_fit_after_position = False
    page.proof_union_bottom = 830.0

    with pytest.raises(
        LayoutRecognitionError,
        match="title, price, capacity and color must fit",
    ):
        adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.position_attempts == 1
    assert page.capture_scale == 1.0


def test_oppo_capture_positions_once_when_qr_initially_occludes_capacity() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.capacity_occluded = True
    page.capacity_occluded_after_position = False

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.position_attempts == 1
    assert page.position_deltas == [104.0]
    assert page.capture_scale == 0.8


def test_oppo_capture_fails_closed_after_one_position_when_qr_still_occludes_capacity() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.capacity_occluded = True
    page.capacity_occluded_after_position = True

    with pytest.raises(
        LayoutRecognitionError,
        match="title, price, capacity and color must fit",
    ):
        adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.position_attempts == 1
    assert page.position_deltas == [104.0]
    assert page.capture_scale == 1.0


def test_oppo_capture_positions_from_the_actual_non_capacity_proof_occluded_by_qr() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.occluded_proof_index = 3
    page.occluded_proof_index_after_position = None

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.position_attempts == 1
    assert page.position_deltas == [64.0]


def test_oppo_capture_does_not_repeat_zoom_when_detail_is_already_at_80_percent() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.proofs_fit = False
    page.proofs_fit_after_scale = True

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scales == [0.8, 0.8]

    second_adapter = _adapter()
    second_adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scales == [0.8, 0.8]
    assert page.position_attempts == 0


def test_oppo_emits_formal_no_model_evidence_after_stable_search_results() -> None:
    observation = _adapter().observe(_task(), _OppoFixturePage("no_model.html"))

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


def test_oppo_accepts_visible_search_query_when_result_input_value_is_cleared() -> None:
    page = _OppoNoModelPageWithVisibleQuery()
    _set_result_cards(
        page,
        (
            ("OPPO A6i+ 8GB+256GB 冰川蓝", "/cn/web/products/40101.html"),
            ("OPPO A6 Pro 12GB+256GB 流光白", "/cn/web/products/40102.html"),
            ("OPPO A6x 8GB+256GB 冰川蓝", "/cn/web/products/40103.html"),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )
    assert len(page.wait_timeout_milliseconds) <= 4


def test_oppo_accepts_visible_vuetify_field_when_result_input_value_is_cleared() -> None:
    page = _OppoNoModelPageWithVisibleQuery()
    query = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "search-query"
    )
    query.attrs.pop("data-oppo-role")
    query.attrs["class"] = "v-field v-field--active"
    _set_result_cards(
        page,
        (
            ("OPPO A6i+ 8GB+256GB 冰川蓝", "/cn/web/products/40101.html"),
            ("OPPO A6 Pro 12GB+256GB 流光白", "/cn/web/products/40102.html"),
            ("OPPO A6x 8GB+256GB 冰川蓝", "/cn/web/products/40103.html"),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


def test_oppo_reads_query_from_vuetify_field_input_value_not_container_text() -> None:
    page = _OppoNoModelPageWithVisibleQuery()
    for node in page.root.descendants():
        if node.attrs.get("data-oppo-role") == "search-input":
            node.attrs.pop("data-oppo-role")
    query = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "search-query"
    )
    query.attrs.pop("data-oppo-role")
    query.attrs["class"] = "v-field v-field--active"
    query.text_parts = []
    nested_input = _OfficialNode(
        "input",
        {
            "class": "v-field__input",
            "value": "OPPO A6 5G",
            "style": "left:20px;top:20px;width:280px;height:36px",
        },
        query,
    )
    query.children.append(nested_input)
    _set_result_cards(
        page,
        (
            ("OPPO A6i+ 8GB+256GB 冰川蓝", "/cn/web/products/40101.html"),
            ("OPPO A6 Pro 12GB+256GB 流光白", "/cn/web/products/40102.html"),
            ("OPPO A6x 8GB+256GB 冰川蓝", "/cn/web/products/40103.html"),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


def test_oppo_reads_query_from_current_generic_dialog_input() -> None:
    page = _OppoNoModelPageWithVisibleQuery()
    for node in page.root.descendants():
        if node.attrs.get("data-oppo-role") == "search-input":
            node.attrs.pop("data-oppo-role")
            node.attrs["class"] = "v-input__control-current"
        if node.attrs.get("data-oppo-role") == "search-query":
            node.attrs.pop("data-oppo-role")
            node.attrs["class"] = "v-field v-field--active"
            node.text_parts = []
    _set_result_cards(
        page,
        (
            ("OPPO A6i+ 8GB+256GB 冰川蓝", "/cn/web/products/40101.html"),
            ("OPPO A6 Pro 12GB+256GB 流光白", "/cn/web/products/40102.html"),
            ("OPPO A6x 8GB+256GB 冰川蓝", "/cn/web/products/40103.html"),
        ),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


def test_oppo_no_model_capture_uses_80_percent_and_restores_after_capture() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoNoModelPageWithVisibleQuery()
    observation = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 0.8
    assert tuple(
        rect.role
        for rect in adapter.capture_rectangles_for_capture(
            task, page, observation.semantic_state
        )
    ) == ("search_keyword", "result_region")

    adapter.restore_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 1.0


def test_oppo_no_model_capture_positions_query_and_complete_results_after_scale() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoNoModelPageWithVisibleQuery()
    observation = adapter.observe(task, page)
    page.proofs_fit = False
    page.proofs_fit_after_scale = False
    page.proofs_fit_after_position = True
    page.proof_union_bottom = 830.0

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 0.8
    assert page.position_attempts == 1
    assert tuple(
        rect.role
        for rect in adapter.capture_rectangles_for_capture(
            task, page, observation.semantic_state
        )
    ) == ("search_keyword", "result_region")


def test_oppo_homepage_product_links_do_not_bypass_the_real_search_submission() -> None:
    page = _OppoFixturePage("normal.html")
    store = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-screen") == "store"
    )
    promo = _OfficialNode(
        "a",
        {
            "data-oppo-role": "product-link",
            "href": "https://www.opposhop.cn/cn/web/products/99999.html",
        },
        store,
    )
    promo.text_parts = ["OPPO Reno16 首页推荐"]
    store.children.append(promo)
    search_opener = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "search-opener"
    )
    assert search_opener.attrs["data-action"] == "open-search"

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.presses == ["Enter"]


def test_oppo_never_writes_no_model_when_search_did_not_leave_the_homepage() -> None:
    page = _OppoFixturePage("normal.html")
    store = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-screen") == "store"
    )
    promo = _OfficialNode(
        "a",
        {
            "data-oppo-role": "product-link",
            "href": "https://www.opposhop.cn/cn/web/products/99999.html",
        },
        store,
    )
    promo.text_parts = ["OPPO Reno16 首页推荐"]
    store.children.append(promo)
    search_opener = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "search-opener"
    )
    search_opener.attrs["data-action"] = "noop"

    with pytest.raises(LayoutRecognitionError, match="search"):
        _adapter().observe(_task(), page)


def test_oppo_allows_official_title_to_omit_the_input_network_marker() -> None:
    from quote_app.sites.official_brands.oppo import _title_matches_model

    assert _title_matches_model(
        "OPPO A5m 5G",
        "OPPO A5m 水晶粉 8GB+256GB",
    )
    assert not _title_matches_model(
        "OPPO A5m 5G",
        "OPPO A5m Pro 水晶粉 8GB+256GB",
    )


def test_oppo_emits_capacity_unavailable_only_after_complete_options_settles() -> None:
    page = _OppoFixturePage("normal.html")
    target = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-option-kind") == "capacity"
        and node.text == "12GB+256GB"
    )
    target.text_parts = ["16GB+512GB"]

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == ("capacity",)
    assert len(page.wait_timeout_milliseconds) >= 20
    assert page.capture_scale == 1.0


def test_oppo_emits_color_unavailable_after_capacity_is_selected() -> None:
    page = _OppoFixturePage("normal.html")
    target = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-option-kind") == "color"
        and node.text == "蓝海浮光"
    )
    target.text_parts = ["晨曦金"]

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.COLOR_UNAVAILABLE
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == ("color",)
    assert page.option_clicks == ["capacity"]
    assert page.capture_scale == 1.0


def test_oppo_resume_uses_saved_detail_without_repeating_search() -> None:
    adapter = _adapter()
    task = _task()
    original = adapter.observe(task, _OppoFixturePage("normal.html"))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=original.price,
        url=original.url,
        observed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    resumed_page = _OppoFixturePage("normal.html")

    resumed = adapter.resume(task, resumed_page, checkpoint)

    assert resumed == original
    assert resumed_page.goto_calls == [checkpoint.url]


def test_oppo_resume_scales_before_first_detail_business_read() -> None:
    adapter = _adapter()
    task = _task()
    original = adapter.observe(task, _OppoFixturePage("normal.html"))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=original.price,
        url=original.url,
        observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    resumed_page = _OppoFixturePage("normal.html")

    resumed = adapter.resume(task, resumed_page, checkpoint)

    assert resumed == original
    assert resumed_page.detail_read_scales[0] == 0.8
    assert resumed_page.capture_scales == [0.8]
    assert resumed_page.capture_scale == 1.0


def test_oppo_resume_does_not_rewrite_an_existing_eighty_percent_scale() -> None:
    adapter = _adapter()
    task = _task()
    original = adapter.observe(task, _OppoFixturePage("normal.html"))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=original.price,
        url=original.url,
        observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    resumed_page = _OppoFixturePage("normal.html")
    resumed_page.capture_scale = 0.8

    resumed = adapter.resume(task, resumed_page, checkpoint)

    assert resumed == original
    assert resumed_page.detail_read_scales[0] == 0.8
    assert resumed_page.capture_scales == []


@pytest.mark.parametrize(
    "url",
    [
        "http://www.opposhop.cn/cn/web/products/32740.html",
        "https://evil.example/cn/web/products/32740.html",
        "https://www.opposhop.cn/cn/web/products/not-a-number.html",
        "https://www.opposhop.cn/cn/web/topic/32740.html",
    ],
)
def test_oppo_rejects_unapproved_detail_urls(url: str) -> None:
    from quote_app.sites.official_brands.oppo import _approved_product_url

    with pytest.raises(ValueError, match="approved numeric"):
        _approved_product_url(url)


def test_oppo_business_state_has_no_extra_stock_or_region_requirement() -> None:
    page = _OppoFixturePage("normal.html")
    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.css_rectangles == OfficialCaptureView(()).css_rectangles


def test_oppo_capture_fails_closed_when_one_of_four_proofs_disappears() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    title = next(node for node in page.root.descendants() if node.attrs.get("data-oppo-role") == "detail-title")
    title.attrs["hidden"] = ""

    with pytest.raises(LayoutRecognitionError):
        adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 1.0

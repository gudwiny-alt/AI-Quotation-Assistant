from __future__ import annotations

import importlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.sites.catalog import load_site_catalog
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from quote_app.tasks.retry import LayoutRecognitionError, NonRetryableTechnicalError
from tests.conftest import _OfficialFixturePage, _OfficialLocator, _OfficialNode, _official_select

_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/vivo"
_POLL_COUNT = 4


class _VivoLocator(_OfficialLocator):
    """Fixture-only browser seam; fixture DOM itself has no vivo test hooks."""

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
        for node in self.page.root.descendants():
            if node.tag == "input":
                node.attrs["value"] = value

    def click(self) -> None:
        node = self.nodes[0]
        classes = set(node.attrs.get("class", "").split())
        if "nav-search-button" in classes:
            self.page.active = "search"
            self.page.search_waiting_for_enter = True
            return
        if node.tag == "a" and "/product/" in node.attrs.get("href", ""):
            self.page.card_clicks.append(node.attrs["href"])
            self.page.goto(self.page.absolute_product_url(node.attrs["href"]))
            return
        group = self.page.option_group(node)
        if group:
            if node.attrs.get("disabled") is not None or node.attrs.get("aria-disabled") == "true":
                raise AssertionError("disabled target option must never be clicked")
            for candidate in self.page.option_nodes(group):
                candidate.attrs["aria-checked"] = "false"
            node.attrs["aria-checked"] = "true"
            self.page.option_clicks.append(group)
            self.page.events.append(f"select:{group}")
            return
        super().click()

    def inner_text(self) -> str:
        node = self.nodes[0]
        if (
            "product-name" in node.attrs.get("class", "").split()
            and self.page.detail_title_override is not None
        ):
            return self.page.detail_title_override
        if "price-current" in node.attrs.get("class", "").split():
            return self.page.read_current_price(node)
        return super().inner_text()

    def evaluate(self, script: str) -> object:
        node = self.nodes[0]
        if "__vivoExplicitSelection" in script:
            return node.attrs.get("aria-checked") == "true"
        return super().evaluate(script)


class _VivoFixturePage(_OfficialFixturePage):
    """A DOM-backed vivo page model. State lives here, not in fixture attributes."""

    def __init__(self, detail: str = "detail_normal.html") -> None:
        super().__init__(
            (_FIXTURES / "search_results.html").read_text(encoding="utf-8")
            + (_FIXTURES / detail).read_text(encoding="utf-8"),
            entry_url="https://shop.vivo.com.cn/",
        )
        self.active = "store"
        self.fill_calls: list[str] = []
        self.card_clicks: list[str] = []
        self.events: list[str] = []
        self.capture_scale = 1.0
        self.capture_scales: list[float] = []
        self.position_deltas: list[float] = []
        self.price_reads = 0
        self.late_card_after_wait: int | None = None
        self.unsettled_price = False
        self.detail_title_override: str | None = None
        self.detail_url_override: str | None = None
        self.remove_proof_after_prepare = False
        self.proof_layout = "fit"  # fit | scale | position | impossible
        self.overlay_role: str | None = None

    def absolute_product_url(self, href: str) -> str:
        return href if href.startswith("https://") else f"https://shop.vivo.com.cn{href}"

    def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append(url)
        if "/product/" in url and urlsplit_path_is_numeric(url):
            self._url = self.detail_url_override or url
            self.active = "product"
            return
        self._url = url
        self.active = "store"

    def press(self, key: str) -> None:
        self.presses.append(key)
        if key == "Enter" and self.search_waiting_for_enter:
            self.search_waiting_for_enter = False
            self.active = "results"
            self._url = "https://shop.vivo.com.cn/search?keyword=vivo%20X200"

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        if self.late_card_after_wait == len(self.wait_timeout_milliseconds):
            late = next(
                node
                for node in self.root.descendants()
                if "late-result" in node.attrs.get("class", "").split()
            )
            late.attrs.pop("hidden", None)

    def locator(self, selector: str) -> _VivoLocator:
        return _VivoLocator(self, _official_select(self.scope_nodes(), selector))

    def scope_nodes(self) -> list[_OfficialNode]:
        all_nodes = self.root.descendants()
        if self.active == "product":
            return [
                node
                for node in all_nodes
                if node is not None
                and (node.tag == "main" or self.is_under(node, "vivo-product-detail"))
            ]
        if self.active == "results":
            return [node for node in all_nodes if self.is_under(node, "search-result-page")]
        if self.active == "search":
            return [node for node in all_nodes if self.is_under(node, "search-drawer")]
        return [node for node in all_nodes if self.is_under(node, "vivo-storefront")]

    @staticmethod
    def is_under(node: _OfficialNode, class_name: str) -> bool:
        while node is not None:
            if class_name in node.attrs.get("class", "").split():
                return True
            node = node.parent  # type: ignore[assignment]
        return False

    def option_group(self, node: _OfficialNode) -> str | None:
        return (
            "capacity"
            if self.is_under(node, "capacity-item")
            else "color"
            if self.is_under(node, "color-item")
            else None
        )

    def option_nodes(self, group: str) -> list[_OfficialNode]:
        parent_class = "capacity-item" if group == "capacity" else "color-item"
        return [
            node
            for node in self.root.descendants()
            if "sku-option" in node.attrs.get("class", "").split()
            and self.is_under(node, parent_class)
        ]

    def selected(self, group: str) -> str | None:
        return next(
            (
                node.text
                for node in self.option_nodes(group)
                if node.attrs.get("aria-checked") == "true"
            ),
            None,
        )

    def read_current_price(self, node: _OfficialNode) -> str:
        self.price_reads += 1
        self.events.append(f"price:{self.selected('capacity')}:{self.selected('color')}")
        if self.selected("capacity") != "12GB+256GB" or self.selected("color") != "辰夜黑":
            return ""
        if self.unsettled_price:
            return "¥4,499" if self.price_reads % 2 else "¥4,699"
        return "" if self.price_reads < 3 else node.text

    def proof_nodes(self) -> dict[str, _OfficialNode]:
        wanted = {
            "title": "product-name",
            "price": "price-current",
            "capacity": "capacity-item",
            "color": "color-item",
        }
        result: dict[str, _OfficialNode] = {}
        for role, class_name in wanted.items():
            result[role] = next(
                node
                for node in self.root.descendants()
                if class_name in node.attrs.get("class", "").split()
            )
        if self.remove_proof_after_prepare:
            result["color"].attrs["hidden"] = ""
        return result

    def evaluate(self, script: str, argument: object = None) -> object:
        # The adapter's scripts must identify the four concrete DOM proof nodes.
        if "data-quotation-capture-scale-original" in script:
            if argument is not None and self.capture_scale != float(argument):
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "__vivoProofsFitCurrentViewport" in script:
            proofs = self.proof_nodes()
            if any(not node.visible for node in proofs.values()):
                return False
            if "__vivoProofsUnoccluded" in script and self.overlay_role:
                return False
            if self.proof_layout == "fit":
                return True
            if self.proof_layout == "scale":
                return self.capture_scale == 0.8
            if self.proof_layout == "position":
                return bool(self.position_deltas)
            return False
        if "__vivoProofPositionState" in script:
            return {
                "unionTop": 110.0,
                "unionBottom": 870.0,
                "viewportHeight": 800.0,
                "scrollY": 100.0,
                "occlusions": []
                if not self.overlay_role
                else [{"proofBottom": 438.0, "blockerTop": 400.0, "role": self.overlay_role}],
            }
        if "__vivoApplyBoundedProofPosition" in script:
            delta = float(argument)
            assert 0 < delta <= 160.0, "positioning must be positive, directed, and bounded"
            assert self.capture_scale == 0.8, (
                "80% must be applied before one directed positioning attempt"
            )
            self.position_deltas.append(delta)
            self.overlay_role = None
            return True
        if "window.scrollTo" in script:
            raise AssertionError("unbounded broad scroll is not an approved vivo capture operation")
        raise AssertionError(f"unexpected vivo fixture evaluate: {script[:100]}")


def urlsplit_path_is_numeric(url: str) -> bool:
    import re
    from urllib.parse import urlsplit

    return re.fullmatch(r"/product/\d+", urlsplit(url).path) is not None


def _spec() -> Any:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "维沃" and spec.channel is WebsiteChannel.OFFICIAL
    )


def _task(model_name: str = "vivo X200") -> WebsiteTask:
    return WebsiteTask(
        task_id=f"vivo-{model_name}",
        run_id="vivo-contract",
        source_row_number=2,
        output_row_number=2,
        material_code="VIVO",
        brand="维沃",
        model_name=model_name,
        ram="12GB",
        storage="256GB",
        color="辰夜黑",
        channel=WebsiteChannel.OFFICIAL,
    )


def _adapter() -> Any:
    # Import inside the test seam: the test suite itself collects before the RED.
    module = importlib.import_module("quote_app.sites.official_brands.vivo")
    adapter = module.VivoOfficialAdapter(_spec())
    assert adapter.__class__.__name__ == "VivoOfficialAdapter"
    return adapter


def _cards(page: _VivoFixturePage, items: tuple[tuple[str, str], ...]) -> None:
    region = next(
        node
        for node in page.root.descendants()
        if "goods-list" in node.attrs.get("class", "").split()
    )
    region.children.clear()
    for title, href in items:
        card = _OfficialNode("article", {"class": "goods-item"}, region)
        link = _OfficialNode("a", {"href": href}, card)
        name = _OfficialNode("p", {"class": "goods-name"}, link)
        name.text_parts = [title]
        link.children.append(name)
        card.children.append(link)
        region.children.append(card)


def test_vivo_fixture_contract_collects_before_missing_adapter_is_executed() -> None:
    page = _VivoFixturePage()
    fixture_attributes = {name for node in page.root.descendants() for name in node.attrs}
    assert not any(
        name.startswith("data-vivo-") or name == "data-screen" for name in fixture_attributes
    )
    with pytest.raises(ModuleNotFoundError, match="official_brands.vivo"):
        _adapter()


def test_vivo_exact_card_click_reaches_numeric_detail_then_selects_and_stabilizes_current_price() -> (
    None
):
    page = _VivoFixturePage()
    observation = _adapter().observe(_task(), page)
    assert observation.outcome is BusinessOutcome.PRICE_FOUND and observation.price == Decimal(
        "4499"
    )
    assert page.card_clicks == ["/product/1000200?skuId=100020011"]
    assert page.url.endswith("/product/1000200?skuId=100020011") and page.active == "product"
    assert page.option_clicks == ["capacity", "color"] and page.price_reads >= 3
    assert (
        page.events.index("select:capacity")
        < page.events.index("select:color")
        < next(
            i for i, event in enumerate(page.events) if event.startswith("price:12GB+256GB:辰夜黑")
        )
    )
    assert page.selected("capacity") == "12GB+256GB" and page.selected("color") == "辰夜黑"


def test_vivo_waits_for_late_exact_card_instead_of_turning_loading_into_no_model() -> None:
    page = _VivoFixturePage()
    _cards(page, (("vivo X200 Pro 12GB+256GB 辰夜黑", "/product/1000199"),))
    page.late_card_after_wait = _POLL_COUNT
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
    assert len(page.wait_timeout_milliseconds) >= _POLL_COUNT and page.card_clicks == [
        "/product/1000203"
    ]


def test_vivo_stable_empty_results_waits_the_full_window_before_legal_no_model() -> None:
    page = _VivoFixturePage()
    _cards(page, (("vivo X200 Ultra 12GB+256GB 辰夜黑", "/product/1000199"),))
    observation = _adapter().observe(_task(), page)
    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )
    assert len(page.wait_timeout_milliseconds) >= _POLL_COUNT


@pytest.mark.parametrize("model", ["iQOO 15", "iqoo 15", " IQOO   15 "])
def test_vivo_rejects_every_iqoo_spelling_before_navigation(model: str) -> None:
    page = _VivoFixturePage()
    with pytest.raises(NonRetryableTechnicalError, match="iQOO"):
        _adapter().observe(_task(model), page)
    assert page.goto_calls == [] and page.fill_calls == []


@pytest.mark.parametrize("suffix", ["Pro", "Plus", "Ultra", "Max", "T", "S"])
def test_vivo_never_enters_derived_model_card(suffix: str) -> None:
    page = _VivoFixturePage()
    _cards(page, ((f"vivo X200 {suffix} 12GB+256GB 辰夜黑", "/product/1000199"),))
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.NO_MODEL
    assert page.card_clicks == []


def test_vivo_skips_invalid_exact_url_and_clicks_later_approved_card() -> None:
    page = _VivoFixturePage()
    _cards(
        page,
        (
            ("vivo X200 12GB+256GB 辰夜黑", "/product/x200-topic"),
            ("vivo X200 16GB+512GB 宝石蓝", "/product/1000200?skuId=100020011"),
        ),
    )
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
    assert page.card_clicks == ["/product/1000200?skuId=100020011"]


def test_vivo_all_invalid_exact_urls_raise_a_named_technical_selection_error() -> None:
    page = _VivoFixturePage()
    _cards(page, (("vivo X200 12GB+256GB 辰夜黑", "/product/topic"),))
    adapter = _adapter()
    with pytest.raises(Exception, match="approved numeric") as caught:
        adapter.observe(_task(), page)
    assert caught.type.__name__ == "VivoSearchCardSelectionError"


@pytest.mark.parametrize(
    ("detail", "outcome", "role"),
    [
        ("detail_missing_capacity.html", BusinessOutcome.CAPACITY_UNAVAILABLE, "capacity"),
        ("detail_missing_color.html", BusinessOutcome.COLOR_UNAVAILABLE, "color"),
    ],
)
def test_vivo_disabled_exact_configuration_is_legal_no(
    detail: str, outcome: BusinessOutcome, role: str
) -> None:
    page = _VivoFixturePage(detail)
    observation = _adapter().observe(_task(), page)
    assert observation.outcome is outcome and tuple(
        rect.role for rect in observation.css_rectangles
    ) == (role,)
    assert page.option_clicks == (
        [] if outcome is BusinessOutcome.CAPACITY_UNAVAILABLE else ["capacity"]
    )


def test_vivo_does_not_reclick_uniquely_preselected_target_options() -> None:
    page = _VivoFixturePage()
    for group in ("capacity", "color"):
        next(
            node for node in page.option_nodes(group) if node.text in {"12GB+256GB", "辰夜黑"}
        ).attrs["aria-checked"] = "true"
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_clicks == []


@pytest.mark.parametrize("drift", ["title", "identity"])
def test_vivo_revalidates_detail_title_and_numeric_identity_after_card_entry(drift: str) -> None:
    page = _VivoFixturePage()
    if drift == "title":
        page.detail_title_override = "vivo X200 Pro"
    else:
        page.detail_url_override = "https://shop.vivo.com.cn/product/999999"
    with pytest.raises(LayoutRecognitionError):
        _adapter().observe(_task(), page)


def test_vivo_unsettled_selected_offer_never_quotes_a_price() -> None:
    page = _VivoFixturePage()
    page.unsettled_price = True
    with pytest.raises(LayoutRecognitionError, match="price|stable|settle"):
        _adapter().observe(_task(), page)
    assert page.price_reads >= 2


def test_vivo_capture_uses_only_four_actual_proofs_without_mutating_view_when_already_fit() -> None:
    page = _VivoFixturePage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert set(page.proof_nodes()) == {"title", "price", "capacity", "color"}
    assert page.capture_scales == [] and page.position_deltas == []


def test_vivo_capture_tries_eighty_percent_before_directed_single_position_when_not_fit_without_overlay() -> (
    None
):
    page = _VivoFixturePage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.proof_layout = "position"
    adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert (
        page.capture_scales == [0.8]
        and len(page.position_deltas) == 1
        and 0 < page.position_deltas[0] <= 160
    )


def test_vivo_capture_overlay_uses_eighty_percent_then_one_bounded_directed_position() -> None:
    page = _VivoFixturePage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.proof_layout = "position"
    page.overlay_role = "color"
    adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert page.capture_scales == [0.8] and len(page.position_deltas) == 1


def test_vivo_capture_fails_closed_after_one_position_or_disappearing_proof() -> None:
    page = _VivoFixturePage()
    adapter = _adapter()
    observation = adapter.observe(_task(), page)
    page.proof_layout = "impossible"
    with pytest.raises(LayoutRecognitionError, match="title, price, capacity and color must fit"):
        adapter.prepare_capture_view(_task(), page, observation.semantic_state)
    assert len(page.position_deltas) == 1
    vanished = _VivoFixturePage()
    second = _adapter()
    state = second.observe(_task(), vanished)
    vanished.remove_proof_after_prepare = True
    with pytest.raises(LayoutRecognitionError):
        second.prepare_capture_view(_task(), vanished, state.semantic_state)

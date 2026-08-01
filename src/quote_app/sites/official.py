from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.catalog import SUPPORTED_BRANDS, SiteSpec, site_session_family
from quote_app.sites.locators import visible_locators
from quote_app.sites.matching import (
    capacity_matches,
    color_matches,
    model_matches,
    normalize_product_text,
)
from quote_app.sites.official_overrides import (
    AppleOfficialOverride,
    HonorOfficialOverride,
    XiaomiOfficialOverride,
)
from quote_app.sites.prices import (
    PriceCandidate,
    PricePolicy,
    SellingPriceEvidence,
    choose_price,
)
from quote_app.sites.protocol import AdapterObservation, BrowserPage
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
    url_contains_credentials,
)
from quote_app.tasks.retry import (
    LayoutRecognitionError,
    LoginRequired,
    NonRetryableTechnicalError,
    SecurityVerificationRequired,
)

OFFICIAL_POLICIES = {
    "小米": PricePolicy.RED_SELLING,
    "HONOR": PricePolicy.LOWEST,
    "华为": PricePolicy.LOWEST,
    "维沃": PricePolicy.LOWEST,
    "欧珀": PricePolicy.LOWEST,
    "苹果": PricePolicy.HIGHEST,
    "ZTE中兴": PricePolicy.LOWEST,
}

OFFICIAL_CAPACITY_STRATEGIES = {
    "小米": "ram_plus_storage",
    "HONOR": "ram_plus_storage",
    "华为": "ram_plus_storage_or_storage",
    "维沃": "ram_plus_storage",
    "欧珀": "ram_plus_storage",
    "苹果": "storage_only",
    "ZTE中兴": "ram_plus_storage",
}

_BRAND_SLUGS = {
    "小米": "xiaomi",
    "HONOR": "honor",
    "华为": "huawei",
    "维沃": "vivo",
    "欧珀": "oppo",
    "苹果": "apple",
    "ZTE中兴": "zte",
}
_DISABLED_CLASSES = frozenset(
    {"disabled", "disable", "sold-out", "unavailable"}
)
_SELECTED_CLASSES = frozenset({"selected", "checked", "active"})
_UNAVAILABLE_STOCK_MARKERS = (
    "无货",
    "暂时缺货",
    "暂时无货",
    "售罄",
    "已抢光",
    "不可购买",
)
_AVAILABLE_STOCK_MARKERS = frozenset({"有货", "现货", "可购买", "库存充足"})
_SKU = re.compile(r"^[A-Za-z0-9_-]+$")
_PRODUCT_TOKEN_PART = re.compile(r"[A-Z]+[0-9]*|[0-9]+")
_PRODUCT_TOKEN_PREFIXES = {
    "小米": "xiaomi",
    "HONOR": "honor",
    "华为": "huawei",
    "维沃": "vivo",
    "欧珀": "oppo",
    "苹果": "iphone",
    "ZTE中兴": "zte",
}
_PRODUCT_MODEL_PREFIXES = {
    "小米": ("XIAOMI", "小米"),
    "HONOR": ("HONOR", "荣耀"),
    "华为": ("HUAWEI", "华为"),
    "维沃": ("VIVO", "维沃"),
    "欧珀": ("OPPO", "欧珀"),
    "苹果": ("IPHONE", "APPLE", "苹果"),
    "ZTE中兴": ("ZTE中兴", "ZTE", "中兴"),
}
_MAX_SELECTION_POLLS = 5
_MAX_PRICE_POLLS = 5
_HONOR_ADDRESS_HYDRATION_POLLS = 12
_HONOR_ADDRESS_HYDRATION_INTERVAL_MS = 500
_HONOR_RENDER_POLLS = 12
_HONOR_RENDER_INTERVAL_MS = 500
_MAX_PRICE_CONTEXT_LENGTH = 1000
_POLL_INTERVAL_MS = 100
_PRICE_STYLE_SCRIPT = """
(element) => {
  let current = element;
  let effectiveLineThrough = false;
  let color = "";
  let contextText = null;
  while (current) {
    const style = window.getComputedStyle(current);
    if (!color) color = style.color;
    if ((style.textDecorationLine || style.textDecoration || "")
        .includes("line-through")) {
      effectiveLineThrough = true;
    }
    if (contextText === null
        && current.hasAttribute
        && current.hasAttribute("data-official-price-context")) {
      contextText = current.getAttribute("data-official-price-context");
    }
    current = current.parentElement;
  }
  return {color, effectiveLineThrough, contextText};
}
"""


@dataclass(frozen=True, slots=True)
class OfficialLocatorSet:
    store_markers: tuple[str, ...]
    search_containers: tuple[str, ...]
    search_inputs: tuple[str, ...]
    search_actions: tuple[str, ...]
    model_keywords: tuple[str, ...]
    result_regions: tuple[str, ...]
    product_cards: tuple[str, ...]
    product_titles: tuple[str, ...]
    product_links: tuple[str, ...]
    empty_results: tuple[str, ...]
    detail_sellers: tuple[str, ...]
    detail_titles: tuple[str, ...]
    current_sku_markers: tuple[str, ...]
    capacity_groups: tuple[str, ...]
    capacity_options: tuple[str, ...]
    color_groups: tuple[str, ...]
    color_options: tuple[str, ...]
    region_states: tuple[str, ...]
    stock_states: tuple[str, ...]
    selling_prices: tuple[str, ...]
    login_markers: tuple[str, ...]
    risk_markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HonorStableOffer:
    current_sku: str
    region: str
    stock_state: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class OfficialStockSample:
    region: str
    state: str
    unavailable: bool


def _locator_set(slug: str, *, apple: bool = False) -> OfficialLocatorSet:
    prefix = f".official-{slug}"
    return OfficialLocatorSet(
        store_markers=(f"{prefix}-store",),
        search_containers=() if apple else (f"{prefix}-search",),
        search_inputs=() if apple else (f"{prefix}-search-input",),
        search_actions=() if apple else (f"{prefix}-search-action",),
        model_keywords=(f"{prefix}-model-keyword",) if apple else (),
        result_regions=(f"{prefix}-results",),
        product_cards=(f"{prefix}-card",),
        product_titles=(f"{prefix}-product-title",),
        product_links=(f"{prefix}-product-link",),
        empty_results=(f"{prefix}-empty",),
        detail_sellers=(f"{prefix}-seller",),
        detail_titles=(f"{prefix}-detail-title",),
        current_sku_markers=(f"{prefix}-sku",),
        capacity_groups=(f"{prefix}-capacities",),
        capacity_options=(
            f"{prefix}-capacities {prefix}-option",
        ),
        color_groups=(f"{prefix}-colors",),
        color_options=(
            f"{prefix}-colors {prefix}-option",
        ),
        region_states=(f"{prefix}-region",),
        stock_states=(f"{prefix}-stock",),
        selling_prices=(f"{prefix}-price",),
        login_markers=('[data-official-role="login"]',),
        risk_markers=('[data-official-role="risk-control"]',),
    )


OFFICIAL_LOCATORS = {
    brand: _locator_set(slug, apple=brand == "苹果")
    for brand, slug in _BRAND_SLUGS.items()
}


class OfficialSiteAdapter:
    """Fail-closed data-driven observer for one approved official store."""

    channel = WebsiteChannel.OFFICIAL

    def __init__(self, spec: SiteSpec) -> None:
        if not isinstance(spec, SiteSpec):
            raise TypeError("spec must be a SiteSpec")
        spec.validate_approved()
        if spec.channel is not WebsiteChannel.OFFICIAL:
            raise ValueError("spec channel must be OFFICIAL")
        if spec.brand not in SUPPORTED_BRANDS:
            raise ValueError("spec brand must be supported")
        if spec.price_policy is not OFFICIAL_POLICIES[spec.brand]:
            raise ValueError("spec price policy does not match the official policy")
        self.spec = spec
        self._locators = OFFICIAL_LOCATORS[spec.brand]
        self._capacity_strategy = OFFICIAL_CAPACITY_STRATEGIES[spec.brand]
        self._xiaomi_override = (
            XiaomiOfficialOverride() if spec.brand == "小米" else None
        )
        self._apple_override = (
            AppleOfficialOverride() if spec.brand == "苹果" else None
        )
        self._honor_override = (
            HonorOfficialOverride() if spec.brand == "HONOR" else None
        )

    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult:
        del task, page, capture
        raise NonRetryableTechnicalError(
            "ADAPTER_DIRECT_EXECUTION_UNSUPPORTED",
            "站点适配器必须通过任务执行器生成正式截图",
        )

    def observe(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        self._validate_task(task)
        browser_page = _playwright_page(page)
        browser_page.goto(self.spec.entry_url, wait_until="domcontentloaded")
        self._raise_if_blocked(browser_page)
        if self._honor_override is not None:
            if self._honor_override.uses_live_contract(
                browser_page
            ) or self._wait_for_honor_live_contract(browser_page):
                return self._observe_honor_live(task, browser_page)
        self._require_approved_store(browser_page)

        result_region, keyword_locator = self._open_model_results(
            browser_page,
            task,
        )
        product_cards = visible_locators(
            result_region,
            self._locators.product_cards,
        )
        empty_states = visible_locators(
            result_region,
            self._locators.empty_results,
        )
        if not product_cards:
            if len(empty_states) != 1:
                raise LayoutRecognitionError(
                    "Official result cards are missing and no explicit empty state "
                    "is visible"
                )
            return self._no_model_observation(
                task,
                browser_page,
                keyword_locator,
                result_region,
            )
        if empty_states:
            raise LayoutRecognitionError(
                "Official result cards conflict with an explicit empty state"
            )

        exact_cards = self._exact_product_cards(
            product_cards,
            task.model_name,
        )
        if not exact_cards:
            return self._no_model_observation(
                task,
                browser_page,
                keyword_locator,
                result_region,
            )
        if len(exact_cards) != 1:
            raise LayoutRecognitionError(
                "Official exact product result is ambiguous"
            )

        product_link = _unique_visible_locator(
            exact_cards[0],
            self._locators.product_links,
            semantic_name="exact product purchase link",
        )
        detail_url = _approved_product_url(
            product_link.get_attribute("href"),
            base_url=browser_page.url,
            expected_host=_required_entry_host(self.spec.entry_url),
            brand=self.spec.brand,
            expected_model=task.model_name,
        )
        if self._apple_override is not None:
            product_link.click()
            browser_page.wait_for_load_state("domcontentloaded")
            if not _url_is_exact_product(
                browser_page.url,
                detail_url=detail_url,
                expected_host=_required_entry_host(self.spec.entry_url),
                brand=self.spec.brand,
                expected_model=task.model_name,
            ):
                browser_page.goto(detail_url, wait_until="domcontentloaded")
        else:
            browser_page.goto(detail_url, wait_until="domcontentloaded")
        return self._observe_loaded_detail(task, browser_page, detail_url)

    def _observe_loaded_detail(
        self,
        task: WebsiteTask,
        browser_page: Any,
        detail_url: str,
    ) -> AdapterObservation:
        browser_page.wait_for_load_state("domcontentloaded")
        self._raise_if_blocked(browser_page)
        self._require_exact_detail_url(
            browser_page,
            detail_url,
            task.model_name,
        )
        self._require_approved_detail_seller(browser_page)

        detail_title = _unique_visible_locator(
            browser_page,
            self._locators.detail_titles,
            semantic_name="product detail title",
        )
        if not model_matches(task.model_name, detail_title.inner_text()):
            raise LayoutRecognitionError(
                "Official product detail model does not match"
            )

        selection_order = (
            self._apple_override.selection_order
            if self._apple_override is not None
            else ("capacity", "color")
        )
        outcomes = {
            "capacity": BusinessOutcome.CAPACITY_UNAVAILABLE,
            "color": BusinessOutcome.COLOR_UNAVAILABLE,
        }
        for option_kind in selection_order:
            option, unavailable_evidence = self._resolve_option(
                browser_page,
                task,
                option_kind,
            )
            if unavailable_evidence is not None:
                self._require_exact_detail_url(
                    browser_page,
                    detail_url,
                    task.model_name,
                )
                return self._legal_no(
                    task,
                    outcomes[option_kind],
                    browser_page,
                    (_css_rect(unavailable_evidence, option_kind),),
                )
            if option is None:
                raise AssertionError("resolved option must be present")
            option.scroll_into_view_if_needed()
            if _is_explicitly_disabled(option):
                self._validate_disabled_option_binding(
                    browser_page,
                    task,
                    option_kind,
                    option,
                )
                self._require_exact_detail_url(
                    browser_page,
                    detail_url,
                    task.model_name,
                )
                return self._legal_no(
                    task,
                    outcomes[option_kind],
                    browser_page,
                    (_css_rect(option, option_kind),),
                )
            option.click()
            self._wait_for_selected(
                browser_page,
                option,
                semantic_name=option_kind,
            )
            self._raise_if_blocked(browser_page)
            self._require_exact_detail_url(
                browser_page,
                detail_url,
                task.model_name,
            )

        selected_price, sold_out, current_sku, stock = (
            self._stable_selected_offer(
                browser_page,
                task,
            )
        )
        self._require_exact_detail_url(
            browser_page,
            detail_url,
            task.model_name,
        )
        if sold_out is not None:
            return self._legal_no(
                task,
                BusinessOutcome.SOLD_OUT,
                browser_page,
                (_css_rect(sold_out, "stock_status"),),
                current_sku=current_sku,
                region=stock.region,
                stock_state=stock.state,
            )
        if selected_price is None:
            raise AssertionError("stable selected price must be present")
        semantic_state = self._semantic_state(
            task,
            browser_page,
            outcome=BusinessOutcome.PRICE_FOUND,
            price=selected_price,
            rectangles=(),
            current_sku=current_sku,
            region=stock.region,
            stock_state=stock.state,
        )
        return AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=selected_price,
            url=browser_page.url,
            css_rectangles=(),
            semantic_state=semantic_state,
        )

    def resume(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        """Navigate to a saved official page and revalidate without store search."""
        self._validate_task(task)
        if (
            not isinstance(checkpoint, WebsiteObservationCheckpoint)
            or checkpoint.task_id != task.task_id
        ):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "官网恢复检查点与当前任务不一致",
            )
        browser_page = _playwright_page(page)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            return self._resume_no_model(task, browser_page, checkpoint.url)
        browser_page.goto(checkpoint.url, wait_until="domcontentloaded")
        if (
            self._honor_override is not None
            and visible_locators(
                browser_page,
                self._honor_override.detail_titles,
            )
        ):
            return self._observe_loaded_honor_detail(
                task,
                browser_page,
                checkpoint.url,
            )
        return self._observe_loaded_detail(
            task,
            browser_page,
            checkpoint.url,
        )

    def _resume_no_model(
        self,
        task: WebsiteTask,
        page: Any,
        search_url: str,
    ) -> AdapterObservation:
        page.goto(search_url, wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        self._raise_if_blocked(page)
        if self._honor_override is not None:
            override = self._honor_override
            _validate_honor_search_url(page.url, entry_url=self.spec.entry_url)
            override.require_store_title(page)
            keyword = self._wait_for_honor_visible(
                page,
                override.search_inputs,
                semantic_name="HONOR result search keyword",
            )
            result_region = self._wait_for_honor_visible(
                page,
                override.result_regions,
                semantic_name="HONOR result region",
            )
            cards = visible_locators(result_region, ("li.grid-items",))
            for card in cards:
                link = _unique_visible_locator(
                    card,
                    override.product_links,
                    semantic_name="HONOR product card link",
                )
                if override.card_matches_model(
                    task.model_name,
                    link.inner_text(),
                ):
                    raise LayoutRecognitionError(
                        "Official HONOR exact product appeared during recovery"
                    )
            return self._no_model_observation(
                task,
                page,
                keyword,
                result_region,
            )
        self._require_approved_store(page)
        keyword = _unique_visible_locator(
            page,
            self._locators.search_inputs,
            semantic_name="result search keyword",
        )
        if (
            normalize_product_text(keyword.input_value())
            != normalize_product_text(task.model_name)
        ):
            raise LayoutRecognitionError(
                "Official recovery search keyword does not match"
            )
        result_region = _unique_visible_locator(
            page,
            self._locators.result_regions,
            semantic_name="result region",
        )
        cards = visible_locators(result_region, self._locators.product_cards)
        if self._exact_product_cards(cards, task.model_name):
            raise LayoutRecognitionError(
                "Official exact product appeared during checkpoint recovery"
            )
        return self._no_model_observation(
            task,
            page,
            keyword,
            result_region,
        )

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        """Return the narrow live HONOR rereader used by formal capture."""
        self._validate_task(task)
        if (
            self._honor_override is None
            or task.brand != "HONOR"
            or task.channel is not WebsiteChannel.OFFICIAL
            or not isinstance(expected, VerifiedSemanticState)
            or expected.outcome is not BusinessOutcome.PRICE_FOUND
            or expected.css_rectangles
            or expected.brand != task.brand
            or expected.model_name != task.model_name
            or expected.capacity != f"{task.ram}+{task.storage}"
            or expected.color != task.color
        ):
            raise LayoutRecognitionError(
                "Official live verified-state reader is unavailable"
            )

        def read() -> VerifiedSemanticState:
            override = self._honor_override
            if override is None:
                raise AssertionError("HONOR override must be present")
            self._require_exact_detail_url(
                page,
                expected.canonical_url,
                task.model_name,
            )
            override.require_detail_store_title(page)
            detail_title = _unique_visible_locator(
                page,
                override.detail_titles,
                semantic_name="HONOR product detail title",
            )
            if not override.detail_matches_task(
                task,
                detail_title.inner_text(),
            ):
                raise LayoutRecognitionError(
                    "Official HONOR product detail model changed"
                )
            offer = self._stable_honor_live_offer(page, task)
            self._require_exact_detail_url(
                page,
                expected.canonical_url,
                task.model_name,
            )
            current = self._semantic_state(
                task,
                page,
                outcome=BusinessOutcome.PRICE_FOUND,
                price=offer.price,
                rectangles=(),
                current_sku=offer.current_sku,
                region=offer.region,
                stock_state=offer.stock_state,
            )
            if current != expected:
                raise LayoutRecognitionError(
                    "Official HONOR verified semantic state changed"
                )
            return current

        return read

    def _observe_honor_live(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> AdapterObservation:
        override = self._honor_override
        if override is None:
            raise AssertionError("HONOR override must be present")
        override.require_store_title(page)
        search_input = self._wait_for_honor_visible(
            page,
            override.search_inputs,
            semantic_name="HONOR store search input",
        )
        search_action = self._wait_for_honor_visible(
            page,
            override.search_actions,
            semantic_name="HONOR store search action",
        )
        search_input.fill(task.model_name)
        search_action.click()
        result_region = self._wait_for_honor_result_region(page)
        if result_region is None:
            search_input.press("Enter")
            result_region = self._wait_for_honor_result_region(page)
        if result_region is None:
            raise NonRetryableTechnicalError(
                "HONOR_SEARCH_RESULTS_MISSING",
                "荣耀官网搜索后未出现产品结果，请保留当前页面检查后重试",
            )
        _validate_honor_search_url(page.url, entry_url=self.spec.entry_url)
        override.require_store_title(page)

        result_input = self._wait_for_honor_visible(
            page,
            override.search_inputs,
            semantic_name="HONOR result search keyword",
        )
        if (
            normalize_product_text(result_input.input_value())
            != normalize_product_text(task.model_name)
        ):
            raise LayoutRecognitionError(
                "Official HONOR result search keyword does not match"
            )
        cards = visible_locators(result_region, ("li.grid-items",))
        if not cards:
            raise LayoutRecognitionError(
                "Official HONOR result cards are missing"
            )
        exact_links: list[Any] = []
        for card in cards:
            link = _unique_visible_locator(
                card,
                override.product_links,
                semantic_name="HONOR product card link",
            )
            if override.card_matches_model(
                task.model_name,
                link.inner_text(),
            ):
                exact_links.append(link)
        if not exact_links:
            raise NonRetryableTechnicalError(
                "HONOR_PRODUCT_MATCH_MISSING",
                "荣耀官网搜索结果未唯一匹配基础机型，已停在搜索结果页供检查",
            )
        if len(exact_links) != 1:
            raise NonRetryableTechnicalError(
                "HONOR_PRODUCT_MATCH_AMBIGUOUS",
                "荣耀官网搜索结果匹配到多个基础机型候选，已停在搜索结果页供检查",
            )
        detail_url = _approved_product_url(
            exact_links[0].get_attribute("href"),
            base_url=page.url,
            expected_host=_required_entry_host(self.spec.entry_url),
            brand=self.spec.brand,
            expected_model=task.model_name,
        )
        page.goto(detail_url, wait_until="domcontentloaded")
        return self._observe_loaded_honor_detail(task, page, detail_url)

    def _wait_for_honor_result_region(self, page: Any) -> Any | None:
        """Wait for rendered HONOR cards without navigating or creating a page."""
        override = self._honor_override
        if override is None:
            raise AssertionError("HONOR override must be present")
        for attempt in range(_HONOR_RENDER_POLLS + 1):
            self._raise_if_blocked(page)
            regions = visible_locators(page, override.result_regions)
            if len(regions) > 1:
                raise LayoutRecognitionError(
                    "Official HONOR result region is ambiguous"
                )
            if len(regions) == 1 and visible_locators(
                regions[0],
                ("li.grid-items",),
            ):
                return regions[0]
            if attempt < _HONOR_RENDER_POLLS:
                page.wait_for_timeout(_HONOR_RENDER_INTERVAL_MS)
        return None

    def _observe_loaded_honor_detail(
        self,
        task: WebsiteTask,
        page: Any,
        detail_url: str,
    ) -> AdapterObservation:
        override = self._honor_override
        if override is None:
            raise AssertionError("HONOR override must be present")
        page.wait_for_load_state("domcontentloaded")
        self._raise_if_blocked(page)
        self._require_exact_detail_url(
            page,
            detail_url,
            task.model_name,
        )
        override.require_detail_store_title(page)
        detail_title = self._wait_for_honor_visible(
            page,
            override.detail_titles,
            semantic_name="HONOR product detail title",
        )
        if not override.detail_matches_task(
            task,
            detail_title.inner_text(),
        ):
            raise LayoutRecognitionError(
                "Official HONOR product detail model does not match"
            )
        override.select_task_sku(page, task)
        self._raise_if_blocked(page)
        self._wait_for_honor_address_hydration(page)
        offer = self._stable_honor_live_offer(page, task)
        self._require_exact_detail_url(
            page,
            detail_url,
            task.model_name,
        )
        semantic_state = VerifiedSemanticState(
            canonical_url=page.url,
            brand=task.brand,
            model_name=task.model_name,
            capacity=f"{task.ram}+{task.storage}",
            color=task.color,
            current_sku=offer.current_sku,
            region=offer.region,
            stock_state=offer.stock_state,
            price=offer.price,
            outcome=BusinessOutcome.PRICE_FOUND,
            css_rectangles=(),
        )
        return AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=offer.price,
            url=page.url,
            css_rectangles=(),
            semantic_state=semantic_state,
        )

    def _wait_for_honor_live_contract(self, page: Any) -> bool:
        """Wait briefly for HONOR's known dynamic store shell when needed."""
        override = self._honor_override
        if override is None:
            raise AssertionError("HONOR override must be present")
        for attempt in range(_HONOR_RENDER_POLLS + 1):
            matches = visible_locators(page, override.search_inputs)
            if len(matches) == 1:
                return True
            if len(matches) > 1:
                raise LayoutRecognitionError(
                    "Official HONOR store search input is missing or ambiguous"
                )
            stores = visible_locators(page, self._locators.store_markers)
            if (
                len(stores) == 1
                and stores[0].inner_text().strip() == self.spec.store_name
            ):
                return False
            if attempt == _HONOR_RENDER_POLLS:
                return False
            page.wait_for_timeout(_HONOR_RENDER_INTERVAL_MS)
            self._raise_if_blocked(page)
        raise AssertionError("HONOR storefront readiness loop must return")

    def _wait_for_honor_visible(
        self,
        page: Any,
        selectors: tuple[str, ...],
        *,
        semantic_name: str,
    ) -> Any:
        """Return one approved visible HONOR element after bounded hydration."""
        for attempt in range(_HONOR_RENDER_POLLS + 1):
            matches = visible_locators(page, selectors)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1 or attempt == _HONOR_RENDER_POLLS:
                raise LayoutRecognitionError(
                    f"Official {semantic_name} is missing or ambiguous"
                )
            page.wait_for_timeout(_HONOR_RENDER_INTERVAL_MS)
            self._raise_if_blocked(page)
        raise AssertionError("HONOR element readiness loop must return")

    def _wait_for_honor_address_hydration(self, page: Any) -> None:
        override = self._honor_override
        if override is None:
            raise AssertionError("HONOR override must be present")
        for attempt in range(_HONOR_ADDRESS_HYDRATION_POLLS + 1):
            address = override.attached_address_root(page)
            if address.is_visible():
                return
            if attempt == _HONOR_ADDRESS_HYDRATION_POLLS:
                break
            page.wait_for_timeout(_HONOR_ADDRESS_HYDRATION_INTERVAL_MS)
            self._raise_if_blocked(page)
        raise LayoutRecognitionError(
            "Official HONOR product address region did not hydrate "
            "within the bounded wait"
        )

    def _stable_honor_live_offer(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> HonorStableOffer:
        override = self._honor_override
        if override is None:
            raise AssertionError("HONOR override must be present")
        previous: (
            tuple[str, str, str, str, tuple[PriceCandidate, ...]] | None
        ) = None
        for _ in range(_MAX_PRICE_POLLS):
            current_sku = override.selected_sku(page, task)
            stock = override.stock_snapshot(page)
            candidates = override.price_candidates(page)
            selected = choose_price(candidates, self.spec.price_policy)
            snapshot = (
                current_sku,
                stock.state_text,
                stock.bounded_context,
                stock.region_text,
                candidates,
            )
            if selected is not None and snapshot == previous:
                confirmed_sku = override.selected_sku(page, task)
                confirmed_stock = override.stock_snapshot(page)
                confirmed_candidates = override.price_candidates(page)
                confirmed_selected = choose_price(
                    confirmed_candidates,
                    self.spec.price_policy,
                )
                self._raise_if_blocked(page)
                post_price_sku = override.selected_sku(page, task)
                post_price_stock = override.stock_snapshot(page)
                self._raise_if_blocked(page)
                if (
                    confirmed_sku != current_sku
                    or post_price_sku != current_sku
                    or confirmed_stock.state_text != stock.state_text
                    or post_price_stock.state_text != stock.state_text
                    or confirmed_stock.bounded_context
                    != stock.bounded_context
                    or post_price_stock.bounded_context
                    != stock.bounded_context
                    or confirmed_stock.region_text != stock.region_text
                    or post_price_stock.region_text != stock.region_text
                    or confirmed_candidates != candidates
                    or confirmed_selected != selected
                ):
                    previous = None
                    page.wait_for_timeout(_POLL_INTERVAL_MS)
                    self._raise_if_blocked(page)
                    continue
                if confirmed_selected is None:
                    raise AssertionError(
                        "confirmed HONOR price must be present"
                    )
                return HonorStableOffer(
                    current_sku=post_price_sku,
                    region=post_price_stock.region_text,
                    stock_state=post_price_stock.state_text,
                    price=confirmed_selected,
                )
            previous = snapshot
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_blocked(page)
        raise LayoutRecognitionError(
            "Official HONOR selected offer did not reach a stable state"
        )

    def _validate_task(self, task: WebsiteTask) -> None:
        if not isinstance(task, WebsiteTask):
            raise TypeError("task must be a WebsiteTask")
        if task.channel is not WebsiteChannel.OFFICIAL:
            raise ValueError("task channel must be OFFICIAL")
        if task.brand != self.spec.brand:
            raise ValueError(
                "task brand must match the approved official store"
            )

    def _raise_if_blocked(self, page: Any) -> None:
        session_family = site_session_family(
            self.spec.brand,
            self.spec.channel,
        )
        if visible_locators(page, self._locators.risk_markers):
            raise SecurityVerificationRequired(
                session_family,
                "品牌官网需要人工完成安全验证",
            )
        if visible_locators(page, self._locators.login_markers):
            raise LoginRequired(session_family, "品牌官网需要人工登录")

    def _require_approved_store(self, page: Any) -> None:
        stores = visible_locators(page, self._locators.store_markers)
        if (
            len(stores) != 1
            or stores[0].inner_text().strip() != self.spec.store_name
        ):
            raise LayoutRecognitionError(
                "Official visible store identity does not match"
            )

    def _require_approved_detail_seller(self, page: Any) -> None:
        sellers = visible_locators(page, self._locators.detail_sellers)
        if (
            len(sellers) != 1
            or sellers[0].inner_text().strip() != self.spec.store_name
        ):
            raise LayoutRecognitionError(
                "Official product detail seller does not match"
            )

    def _open_model_results(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> tuple[Any, Any]:
        if self._apple_override is not None:
            keyword = _unique_visible_locator(
                page,
                self._locators.model_keywords,
                semantic_name="family model keyword",
            )
            if (
                normalize_product_text(keyword.inner_text())
                != normalize_product_text(task.model_name)
            ):
                raise LayoutRecognitionError(
                    "Official family model keyword does not match"
                )
            result_region = _unique_visible_locator(
                page,
                self._locators.result_regions,
                semantic_name="family model result region",
            )
            return result_region, keyword

        search_container = _unique_visible_locator(
            page,
            self._locators.search_containers,
            semantic_name="store search container",
        )
        search_input = _unique_visible_locator(
            search_container,
            self._locators.search_inputs,
            semantic_name="store search input",
        )
        search_action = _unique_visible_locator(
            search_container,
            self._locators.search_actions,
            semantic_name="store search action",
        )
        search_input.fill(task.model_name)
        search_action.click()
        page.wait_for_load_state("domcontentloaded")
        self._raise_if_blocked(page)
        _validate_search_url(
            page.url,
            expected_host=_required_entry_host(self.spec.entry_url),
            expected_model=task.model_name,
        )
        self._require_approved_store(page)

        result_input = _unique_visible_locator(
            page,
            self._locators.search_inputs,
            semantic_name="result search keyword",
        )
        if (
            normalize_product_text(result_input.input_value())
            != normalize_product_text(task.model_name)
        ):
            raise LayoutRecognitionError(
                "Official result search keyword does not match"
            )
        result_region = _unique_visible_locator(
            page,
            self._locators.result_regions,
            semantic_name="result region",
        )
        return result_region, result_input

    def _exact_product_cards(
        self,
        cards: tuple[Any, ...],
        model_name: str,
    ) -> tuple[Any, ...]:
        exact: list[Any] = []
        for card in cards:
            title = _unique_visible_locator(
                card,
                self._locators.product_titles,
                semantic_name="product card title",
            )
            if model_matches(model_name, title.inner_text()):
                exact.append(card)
        return tuple(exact)

    def _exact_capacity_option(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> tuple[Any | None, Any | None]:
        options = visible_locators(page, self._locators.capacity_options)

        if self._capacity_strategy == "ram_plus_storage":
            exact = tuple(
                option
                for option in options
                if capacity_matches(
                    option.inner_text(),
                    task.ram,
                    task.storage,
                )
            )
            return self._resolved_or_unavailable(
                page,
                exact,
                task,
                option_kind="capacity",
            )

        if self._capacity_strategy == "storage_only":
            exact = tuple(
                option
                for option in options
                if _storage_only_matches(option.inner_text(), task.storage)
            )
            return self._resolved_or_unavailable(
                page,
                exact,
                task,
                option_kind="capacity",
            )

        if self._capacity_strategy == "ram_plus_storage_or_storage":
            combined = tuple(
                option
                for option in options
                if capacity_matches(
                    option.inner_text(),
                    task.ram,
                    task.storage,
                )
            )
            if len(combined) > 1:
                raise LayoutRecognitionError(
                    "Official exact combined capacity is ambiguous"
                )
            if combined:
                return combined[0], None
            storage_only = tuple(
                option
                for option in options
                if _storage_only_matches(option.inner_text(), task.storage)
            )
            return self._resolved_or_unavailable(
                page,
                storage_only,
                task,
                option_kind="capacity",
            )
        raise LayoutRecognitionError(
            "Official capacity matching strategy is unsupported"
        )

    def _exact_color_option(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> tuple[Any | None, Any | None]:
        options = visible_locators(page, self._locators.color_options)
        exact = tuple(
            option
            for option in options
            if color_matches(task.color, option.inner_text())
        )
        return self._resolved_or_unavailable(
            page,
            exact,
            task,
            option_kind="color",
        )

    def _resolve_option(
        self,
        page: Any,
        task: WebsiteTask,
        option_kind: str,
    ) -> tuple[Any | None, Any | None]:
        if option_kind == "capacity":
            return self._exact_capacity_option(page, task)
        if option_kind == "color":
            return self._exact_color_option(page, task)
        raise AssertionError(f"unsupported option kind: {option_kind}")

    def _resolved_or_unavailable(
        self,
        page: Any,
        exact: tuple[Any, ...],
        task: WebsiteTask,
        *,
        option_kind: str,
    ) -> tuple[Any | None, Any | None]:
        if len(exact) > 1:
            raise LayoutRecognitionError(
                f"Official exact {option_kind} option is ambiguous"
            )
        if exact:
            return exact[0], None
        return None, self._bound_complete_option_group(
            page,
            task,
            option_kind,
        )

    def _bound_complete_option_group(
        self,
        page: Any,
        task: WebsiteTask,
        option_kind: str,
    ) -> Any:
        selectors = (
            self._locators.capacity_groups
            if option_kind == "capacity"
            else self._locators.color_groups
        )
        groups = visible_locators(page, selectors)
        if len(groups) != 1:
            raise LayoutRecognitionError(
                f"Official exact {option_kind} option is missing and "
                "its complete option group is unavailable"
            )
        group = groups[0]
        if group.get_attribute("data-options-complete") != "true":
            raise LayoutRecognitionError(
                f"Official exact {option_kind} option is missing without "
                "a complete option-group proof"
            )
        current_sku = self._current_sku_identity(page)
        group_sku = _required_sku(
            group.get_attribute("data-context-sku"),
            semantic_name=f"{option_kind} option group",
        )
        if group_sku != current_sku:
            raise LayoutRecognitionError(
                f"Official unavailable {option_kind} option group "
                "does not bind to the current SKU"
            )
        self._validate_prior_option_bindings(
            page,
            task,
            option_kind,
            current_sku,
        )
        return group

    def _validate_disabled_option_binding(
        self,
        page: Any,
        task: WebsiteTask,
        option_kind: str,
        option: Any,
    ) -> None:
        current_sku = self._current_sku_identity(page)
        binding_names = (
            ("data-sku", "data-context-sku")
            if option_kind == "color"
            else ("data-sku",)
        )
        target_skus = tuple(
            _required_sku(
                option.get_attribute(binding_name),
                semantic_name=f"disabled {option_kind}",
            )
            for binding_name in binding_names
        )
        if set(target_skus) != {current_sku}:
            raise LayoutRecognitionError(
                f"Official disabled {option_kind} does not bind "
                "to the current SKU"
            )
        self._validate_prior_option_bindings(
            page,
            task,
            option_kind,
            current_sku,
        )

    def _current_sku_identity(self, page: Any) -> str:
        marker = _unique_visible_locator(
            page,
            self._locators.current_sku_markers,
            semantic_name="current SKU marker",
        )
        return _required_sku(
            marker.get_attribute("data-current-sku"),
            semantic_name="current SKU marker",
        )

    def _validate_prior_option_bindings(
        self,
        page: Any,
        task: WebsiteTask,
        option_kind: str,
        current_sku: str,
    ) -> None:
        selection_order = (
            self._apple_override.selection_order
            if self._apple_override is not None
            else ("capacity", "color")
        )
        for prior_kind in selection_order[: selection_order.index(option_kind)]:
            binding_names: tuple[str, ...]
            if prior_kind == "capacity":
                prior_option = self._unique_selected_capacity(page, task)
                binding_names = ("data-sku",)
            else:
                prior_option = self._unique_selected_color(page, task)
                binding_names = ("data-sku", "data-context-sku")
            prior_skus = tuple(
                _required_sku(
                    prior_option.get_attribute(binding_name),
                    semantic_name=f"selected prior {prior_kind}",
                )
                for binding_name in binding_names
            )
            if set(prior_skus) != {current_sku}:
                raise LayoutRecognitionError(
                    f"Official selected prior {prior_kind} does not bind "
                    "to the current SKU"
                )

    def _wait_for_selected(
        self,
        page: Any,
        option: Any,
        *,
        semantic_name: str,
    ) -> None:
        for _ in range(_MAX_SELECTION_POLLS):
            if _is_approved_selected(option):
                return
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_blocked(page)
        raise LayoutRecognitionError(
            f"Official exact {semantic_name} option did not become selected"
        )

    def _selected_sku_identity(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> str:
        capacity = self._unique_selected_capacity(page, task)
        color = self._unique_selected_color(page, task)
        marker = _unique_visible_locator(
            page,
            self._locators.current_sku_markers,
            semantic_name="current SKU marker",
        )
        current_sku = _required_sku(
            marker.get_attribute("data-current-sku"),
            semantic_name="current SKU marker",
        )
        capacity_sku = _required_sku(
            capacity.get_attribute("data-sku"),
            semantic_name="selected capacity",
        )
        color_sku = _required_sku(
            color.get_attribute("data-sku"),
            semantic_name="selected color",
        )
        color_context_sku = _required_sku(
            color.get_attribute("data-context-sku"),
            semantic_name="selected color context",
        )
        if (
            capacity_sku,
            color_sku,
            color_context_sku,
        ) != (
            current_sku,
            current_sku,
            current_sku,
        ):
            raise LayoutRecognitionError(
                "Official selected options do not bind to the current SKU"
            )
        return current_sku

    def _unique_selected_capacity(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> Any:
        options = visible_locators(page, self._locators.capacity_options)
        selected = tuple(
            option for option in options if _is_approved_selected(option)
        )
        if len(selected) != 1:
            raise LayoutRecognitionError(
                "Official final selected capacity is missing or ambiguous"
            )
        label = selected[0].inner_text()
        if self._capacity_strategy == "ram_plus_storage":
            matches = capacity_matches(label, task.ram, task.storage)
        elif self._capacity_strategy == "storage_only":
            matches = _storage_only_matches(label, task.storage)
        else:
            matches = capacity_matches(
                label,
                task.ram,
                task.storage,
            ) or _storage_only_matches(label, task.storage)
        if not matches:
            raise LayoutRecognitionError(
                "Official final selected capacity changed"
            )
        return selected[0]

    def _unique_selected_color(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> Any:
        options = visible_locators(page, self._locators.color_options)
        selected = tuple(
            option for option in options if _is_approved_selected(option)
        )
        if (
            len(selected) != 1
            or not color_matches(task.color, selected[0].inner_text())
        ):
            raise LayoutRecognitionError(
                "Official final selected color is missing, ambiguous, or changed"
            )
        return selected[0]

    def _price_candidates(
        self,
        page: Any,
        current_sku: str,
    ) -> tuple[PriceCandidate, ...] | None:
        locators = visible_locators(page, self._locators.selling_prices)
        price_skus = tuple(
            _required_sku(
                locator.get_attribute("data-sku"),
                semantic_name="current selling price",
            )
            for locator in locators
        )
        distinct_skus = set(price_skus)
        if len(distinct_skus) > 1:
            raise LayoutRecognitionError(
                "Official selling price nodes have conflicting SKU bindings"
            )
        if distinct_skus and distinct_skus != {current_sku}:
            return None

        candidates: list[PriceCandidate] = []
        for locator in locators:
            style = locator.evaluate(_PRICE_STYLE_SCRIPT)
            if not isinstance(style, dict):
                raise LayoutRecognitionError(
                    "Official selling price style is unavailable"
                )
            color = style.get("color")
            line_through = style.get("effectiveLineThrough")
            context = style.get("contextText")
            if (
                not isinstance(color, str)
                or not color.strip()
                or type(line_through) is not bool
                or not isinstance(context, str)
                or not context.strip()
                or len(context) > _MAX_PRICE_CONTEXT_LENGTH
            ):
                raise LayoutRecognitionError(
                    "Official selling price context or style is invalid"
                )
            if (
                self._xiaomi_override is not None
                and not self._xiaomi_override.accepts_price_context(context)
            ):
                continue
            candidates.append(
                PriceCandidate(
                    text=locator.inner_text(),
                    context=context,
                    visible=locator.is_visible(),
                    computed_color=color,
                    selling_evidence=(
                        SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE
                    ),
                    effective_line_through=line_through,
                )
            )
        return tuple(candidates)

    def _stock_status(
        self,
        page: Any,
        current_sku: str,
    ) -> tuple[OfficialStockSample, Any]:
        states = visible_locators(page, self._locators.stock_states)
        if len(states) != 1:
            raise LayoutRecognitionError(
                "Official current-SKU stock state is missing or ambiguous"
            )
        state = states[0]
        stock_sku = _required_sku(
            state.get_attribute("data-sku"),
            semantic_name="stock state",
        )
        if stock_sku != current_sku:
            raise LayoutRecognitionError(
                "Official stock state does not bind to the current SKU"
            )
        stock_text = normalize_product_text(state.inner_text())
        unavailable_mask = [False] * len(stock_text)
        for marker in _UNAVAILABLE_STOCK_MARKERS:
            start = 0
            while (index := stock_text.find(marker, start)) >= 0:
                unavailable_mask[index : index + len(marker)] = (
                    [True] * len(marker)
                )
                start = index + len(marker)
        available_text = "".join(
            " " if masked else character
            for character, masked in zip(
                stock_text,
                unavailable_mask,
                strict=True,
            )
        )
        is_available = any(
            marker in available_text for marker in _AVAILABLE_STOCK_MARKERS
        )
        is_unavailable = any(unavailable_mask)
        if is_available == is_unavailable:
            raise LayoutRecognitionError(
                "Official current-SKU stock state is unrecognized or conflicting"
            )
        regions = visible_locators(page, self._locators.region_states)
        if len(regions) != 1:
            raise LayoutRecognitionError(
                "Official delivery region is missing or ambiguous"
            )
        region_text = _normalized_region(regions[0].inner_text())
        if not region_text:
            raise LayoutRecognitionError(
                "Official delivery region is blank"
            )
        return (
            OfficialStockSample(
                region=region_text,
                state=stock_text,
                unavailable=is_unavailable,
            ),
            state,
        )

    def _stable_selected_offer(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> tuple[Decimal | None, Any | None, str, OfficialStockSample]:
        previous: (
            tuple[str, OfficialStockSample, tuple[PriceCandidate, ...]]
            | None
        ) = None
        for _ in range(_MAX_PRICE_POLLS):
            current_sku = self._selected_sku_identity(page, task)
            stock, stock_locator = self._stock_status(page, current_sku)
            if stock.unavailable:
                self._confirm_selected_sku(page, task, current_sku)
                return None, stock_locator, current_sku, stock
            candidates = self._price_candidates(page, current_sku)
            if candidates is None:
                previous = None
                page.wait_for_timeout(_POLL_INTERVAL_MS)
                self._raise_if_blocked(page)
                continue
            selected = choose_price(candidates, self.spec.price_policy)
            snapshot = (current_sku, stock, candidates)
            if selected is not None and snapshot == previous:
                confirmed_sku = self._selected_sku_identity(page, task)
                confirmed_stock, confirmed_stock_locator = self._stock_status(
                    page,
                    confirmed_sku,
                )
                if confirmed_stock.unavailable:
                    self._confirm_selected_sku(
                        page,
                        task,
                        confirmed_sku,
                    )
                    return (
                        None,
                        confirmed_stock_locator,
                        confirmed_sku,
                        confirmed_stock,
                    )
                if (
                    confirmed_sku != current_sku
                    or confirmed_stock != stock
                ):
                    previous = None
                    page.wait_for_timeout(_POLL_INTERVAL_MS)
                    self._raise_if_blocked(page)
                    continue
                return selected, None, current_sku, confirmed_stock
            previous = snapshot
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_blocked(page)
        raise LayoutRecognitionError(
            "Official selected variant price did not reach a verified stable state"
        )

    def _confirm_selected_sku(
        self,
        page: Any,
        task: WebsiteTask,
        expected_sku: str,
    ) -> None:
        if self._selected_sku_identity(page, task) != expected_sku:
            raise LayoutRecognitionError(
                "Official selected SKU changed before the result was returned"
            )

    def _require_exact_detail_url(
        self,
        page: Any,
        detail_url: str,
        model_name: str,
    ) -> None:
        if not _url_is_exact_product(
            page.url,
            detail_url=detail_url,
            expected_host=_required_entry_host(self.spec.entry_url),
            brand=self.spec.brand,
            expected_model=model_name,
        ):
            raise LayoutRecognitionError(
                "Official product detail URL changed from the approved item"
            )

    @staticmethod
    def _no_model_observation(
        task: WebsiteTask,
        page: Any,
        keyword: Any,
        result_region: Any,
    ) -> AdapterObservation:
        return OfficialSiteAdapter._legal_no(
            task,
            BusinessOutcome.NO_MODEL,
            page,
            (
                _css_rect(keyword, "search_keyword"),
                _css_rect(result_region, "result_region"),
            ),
        )

    @staticmethod
    def _legal_no(
        task: WebsiteTask,
        outcome: BusinessOutcome,
        page: Any,
        rectangles: tuple[CssRect, ...],
        *,
        current_sku: str = "not-applicable",
        region: str = "not-applicable",
        stock_state: str = "not-applicable",
    ) -> AdapterObservation:
        semantic_state = OfficialSiteAdapter._semantic_state(
            task,
            page,
            outcome=outcome,
            price=None,
            rectangles=rectangles,
            current_sku=current_sku,
            region=region,
            stock_state=stock_state,
        )
        return AdapterObservation(
            outcome=outcome,
            price=None,
            url=page.url,
            css_rectangles=rectangles,
            semantic_state=semantic_state,
        )

    @staticmethod
    def _semantic_state(
        task: WebsiteTask,
        page: Any,
        *,
        outcome: BusinessOutcome,
        price: Decimal | None,
        rectangles: tuple[CssRect, ...],
        current_sku: str,
        region: str,
        stock_state: str,
    ) -> VerifiedSemanticState:
        return VerifiedSemanticState(
            canonical_url=page.url,
            brand=task.brand,
            model_name=task.model_name,
            capacity=f"{task.ram}+{task.storage}",
            color=task.color,
            current_sku=current_sku,
            region=region,
            stock_state=stock_state,
            price=price,
            outcome=outcome,
            css_rectangles=rectangles,
        )


def _normalized_region(value: str) -> str:
    return normalize_product_text(value).replace(" > ", ">")


def _playwright_page(page: BrowserPage) -> Any:
    required = (
        "goto",
        "locator",
        "title",
        "wait_for_load_state",
        "wait_for_timeout",
    )
    if not all(callable(getattr(page, name, None)) for name in required):
        raise TypeError(
            "page must provide the synchronous Playwright page surface"
        )
    return page


def _unique_visible_locator(
    scope: Any,
    selectors: tuple[str, ...],
    *,
    semantic_name: str,
) -> Any:
    visible = visible_locators(scope, selectors)
    if len(visible) != 1:
        raise LayoutRecognitionError(
            f"Official {semantic_name} is missing or ambiguous"
        )
    return visible[0]


def _storage_only_matches(label: str, storage: str) -> bool:
    actual = normalize_product_text(label)
    expected = normalize_product_text(storage)
    return bool(actual and expected and actual == expected)


def _is_explicitly_disabled(locator: Any) -> bool:
    if locator.get_attribute("disabled") is not None:
        return True
    if locator.get_attribute("aria-disabled") == "true":
        return True
    classes = set((locator.get_attribute("class") or "").lower().split())
    return bool(classes & _DISABLED_CLASSES)


def _is_approved_selected(locator: Any) -> bool:
    if locator.get_attribute("aria-selected") == "true":
        return True
    if locator.get_attribute("aria-checked") == "true":
        return True
    classes = set((locator.get_attribute("class") or "").lower().split())
    return bool(classes & _SELECTED_CLASSES)


def _approved_product_url(
    raw_href: object,
    *,
    base_url: str,
    expected_host: str,
    brand: str,
    expected_model: str,
) -> str:
    if not isinstance(raw_href, str) or not raw_href.strip():
        raise LayoutRecognitionError(
            "Official exact product URL is missing"
        )
    try:
        resolved = urljoin(base_url, raw_href.strip())
        parsed = urlsplit(resolved)
        port = parsed.port
        expected_path = _expected_product_path(brand, expected_model)
        approved_path = parsed.path == expected_path
        if brand == "HONOR":
            approved_path = (
                approved_path
                or HonorOfficialOverride.is_numeric_product_path(parsed.path)
            )
        approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == expected_host
            and port is None
            and parsed.username is None
            and parsed.password is None
            and approved_path
            and not parsed.query
            and not parsed.fragment
            and not url_contains_credentials(resolved)
        )
    except ValueError:
        approved = False
    if not approved:
        raise LayoutRecognitionError(
            "Official exact product URL is not approved"
        )
    return resolved


def _url_is_exact_product(
    raw_url: object,
    *,
    detail_url: str,
    expected_host: str,
    brand: str,
    expected_model: str,
) -> bool:
    try:
        normalized = _approved_product_url(
            raw_url,
            base_url=detail_url,
            expected_host=expected_host,
            brand=brand,
            expected_model=expected_model,
        )
    except LayoutRecognitionError:
        return False
    return normalized == detail_url


def _expected_product_path(brand: str, model_name: str) -> str:
    normalized_model = normalize_product_text(model_name)
    remainder: str | None = None
    for prefix in _PRODUCT_MODEL_PREFIXES[brand]:
        if normalized_model.startswith(prefix):
            remainder = normalized_model[len(prefix) :].strip()
            break
    if remainder is None or not remainder:
        raise LayoutRecognitionError(
            "Official requested model has no approved product URL token"
        )
    parts = _PRODUCT_TOKEN_PART.findall(remainder)
    compact_remainder = re.sub(r"[\s_-]+", "", remainder)
    if not parts or "".join(parts) != compact_remainder:
        raise LayoutRecognitionError(
            "Official requested model product URL token is invalid"
        )
    token_parts = (_PRODUCT_TOKEN_PREFIXES[brand], *(part.lower() for part in parts))
    return f"/product/{'-'.join(token_parts)}"


def _validate_search_url(
    raw_url: object,
    *,
    expected_host: str,
    expected_model: str,
) -> None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise LayoutRecognitionError(
            "Official store search URL is missing"
        )
    try:
        parsed = urlsplit(raw_url.strip())
        structural_approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == expected_host
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/search"
            and not parsed.fragment
            and not url_contains_credentials(raw_url)
        )
        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
        if (
            not structural_approved
            or len(query) != 1
            or query[0][0] != "q"
            or normalize_product_text(query[0][1])
            != normalize_product_text(expected_model)
        ):
            raise ValueError
    except (UnicodeError, ValueError):
        raise LayoutRecognitionError(
            "Official store search URL is not approved"
        )


def _validate_honor_search_url(
    raw_url: object,
    *,
    entry_url: str,
) -> None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise LayoutRecognitionError(
            "Official HONOR store search URL is missing"
        )
    try:
        parsed = urlsplit(raw_url.strip())
        expected = urlsplit(entry_url)
        approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == expected.hostname
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == expected.path
            and parsed.query == expected.query
            and not parsed.fragment
            and not url_contains_credentials(raw_url)
        )
    except ValueError:
        approved = False
    if not approved:
        raise LayoutRecognitionError(
            "Official HONOR store search URL is not approved"
        )


def _required_entry_host(entry_url: str) -> str:
    hostname = urlsplit(entry_url).hostname
    if hostname is None:
        raise LayoutRecognitionError(
            "Official approved store hostname is missing"
        )
    return hostname.lower()


def _required_sku(
    raw_sku: object,
    *,
    semantic_name: str,
) -> str:
    if (
        not isinstance(raw_sku, str)
        or _SKU.fullmatch(raw_sku.strip()) is None
    ):
        raise LayoutRecognitionError(
            f"Official {semantic_name} SKU binding is missing or invalid"
        )
    return raw_sku.strip()


def _css_rect(locator: Any, role: str) -> CssRect:
    box = locator.bounding_box()
    if not isinstance(box, dict):
        raise LayoutRecognitionError(
            "Official evidence rectangle is unavailable"
        )
    try:
        values = tuple(
            float(box[key]) for key in ("x", "y", "width", "height")
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise LayoutRecognitionError(
            "Official evidence rectangle is invalid"
        ) from error
    if not all(math.isfinite(value) for value in values):
        raise LayoutRecognitionError(
            "Official evidence rectangle is invalid"
        )
    try:
        return CssRect(
            x=values[0],
            y=values[1],
            width=values[2],
            height=values[3],
            role=role,
        )
    except ValueError as error:
        raise LayoutRecognitionError(
            "Official evidence rectangle is invalid"
        ) from error

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.catalog import SiteSpec
from quote_app.sites.detail_capture_view import (
    apply_capture_scale,
    position_detail_for_capture,
    position_result_cards_for_capture,
    restore_capture_scale,
)
from quote_app.sites.locators import (
    JD_CAPACITY_OPTIONS,
    JD_COLOR_OPTIONS,
    JD_CURRENT_SKU_SELLING_PRICES,
    JD_CURRENT_SKU_MARKERS,
    JD_DETAIL_SELLER_MARKERS,
    JD_DETAIL_TITLES,
    JD_DELIVERY_REGIONS,
    JD_EMPTY_RESULTS,
    JD_LOGIN_MARKERS,
    JD_MODERN_CURRENT_SKU_SELLING_PRICES,
    JD_MODERN_DETAIL_SELLER_MARKERS,
    JD_MODERN_DETAIL_TITLES,
    JD_MODERN_DELIVERY_REGIONS,
    JD_MODERN_SKU_OPTIONS,
    JD_PRODUCT_CARDS,
    JD_PRODUCT_LINKS,
    JD_PRODUCT_TITLES,
    JD_RESULT_REGIONS,
    JD_RISK_CONTROL_MARKERS,
    JD_SEARCH_ACTIONS,
    JD_SEARCH_INPUTS,
    JD_STOCK_STATES,
    JD_STORE_MARKERS,
    JD_STORE_SEARCH_CONTAINERS,
    unique_visible_locator,
    visible_locators,
)
from quote_app.sites.matching import (
    capacity_matches,
    color_matches,
    model_matches,
    normalize_product_text,
)
from quote_app.sites.navigation import click_and_wait_for_navigation
from quote_app.sites.prices import (
    PriceCandidate,
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

_UNAVAILABLE_STOCK_MARKERS = (
    "无货",
    "暂时缺货",
    "暂时无货",
    "售罄",
    "已抢光",
    "不可购买",
)
_AVAILABLE_STOCK_MARKERS = ("现货", "有货", "库存充足")
_DISABLED_CLASSES = frozenset(
    {
        "disabled",
        "disable",
        "sold-out",
        "unavailable",
    }
)
_SELECTED_CLASSES = frozenset({"selected", "checked", "active"})
_JD_ITEM_PATH = re.compile(r"^/[0-9]+\.html$")
_JD_STORE_SEARCH_PATH = re.compile(r"^/view_search-[0-9-]+\.html$")
_JD_RESULT_MODEL_VARIANTS = (
    "PRO",
    "PLUS",
    "ULTRA",
    "MAX",
    "MINI",
    "LITE",
    "NEO",
    "AIR",
    "RSR",
    "EDGE",
    "FE",
    "GT",
    "青春版",
    "活力版",
    "竞速版",
    "至尊版",
)
_JD_RESULT_ACCESSORY_MARKERS = (
    "手机壳",
    "保护壳",
    "保护套",
    "手机套",
    "钢化膜",
    "屏幕膜",
    "贴膜",
    "充电器",
    "数据线",
    "耳机",
    "配件",
    "适用",
    "支架",
)
_NUMERIC_SKU = re.compile(r"^[0-9]+$")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_MAX_SELECTION_POLLS = 5
_MAX_MODERN_SELECTION_POLLS = 20
_MAX_PRICE_POLLS = 5
_MAX_STORE_READY_POLLS = 10
_MAX_RESULT_READY_POLLS = 30
_MAX_DETAIL_READY_POLLS = 30
_MAX_MODERN_SKU_SCAN_STEPS = 8
_MAX_SEARCH_URL_POLLS = 20
_MAX_VERIFIED_STATE_POLLS = 10
_POLL_INTERVAL_MS = 100
_STORE_READY_INTERVAL_MS = 500
_MODERN_SELECTION_INTERVAL_MS = 250
_MODERN_SKU_SCAN_INTERVAL_MS = 500
_MODERN_SKU_SCAN_PIXELS = 520
_VERIFIED_STATE_INTERVAL_MS = 250
_JD_MODERN_SELLER_UI_SUFFIXES = (
    "自营",
    "关注店铺",
    "进店逛逛",
)
_JD_RISK_CONTROL_TEXTS = (
    "访问过于频繁",
    "操作过于频繁",
    "请完成安全验证",
    "拖动滑块",
    "请在下方验证",
)
_GENERIC_COLOR_NAMES = frozenset(
    {
        "黑色",
        "白色",
        "蓝色",
        "绿色",
        "红色",
        "紫色",
        "灰色",
        "银色",
        "金色",
        "橙色",
        "粉色",
        "黄色",
    }
)
_PRICE_STYLE_SCRIPT = """
(element) => {
  let current = element;
  let effectiveLineThrough = false;
  let color = "";
  while (current) {
    const style = window.getComputedStyle(current);
    if (!color) color = style.color;
    if ((style.textDecorationLine || style.textDecoration || "")
        .includes("line-through")) {
      effectiveLineThrough = true;
    }
    current = current.parentElement;
  }
  return {color, effectiveLineThrough};
}
"""
_PRICE_CONTEXT_SCRIPT = "(element) => element.parentElement?.innerText || element.innerText"


@dataclass(frozen=True, slots=True)
class JDStockSample:
    region: str
    state: str
    unavailable: bool


class JDAdapter:
    """Fail-closed observer for one approved JD self-operated store."""

    channel = WebsiteChannel.JD

    def __init__(self, spec: SiteSpec) -> None:
        if not isinstance(spec, SiteSpec):
            raise TypeError("spec must be a SiteSpec")
        spec.validate_approved()
        if spec.channel is not WebsiteChannel.JD:
            raise ValueError("spec channel must be JD")
        self.spec = spec

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
        stage = ["京东店铺页"]
        try:
            return self._observe_with_stage(task, page, stage)
        except LayoutRecognitionError as error:
            if error.stage is not None:
                raise
            raise LayoutRecognitionError(
                str(error),
                stage=stage[0],
            ) from error

    def resume(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        """Navigate straight to a saved item and fully revalidate its offer."""
        self._validate_task(task)
        if (
            not isinstance(checkpoint, WebsiteObservationCheckpoint)
            or checkpoint.task_id != task.task_id
        ):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "京东恢复检查点与当前任务不一致",
            )
        browser_page = _playwright_page(page)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            return self._resume_no_model(task, browser_page, checkpoint.url)
        return self._observe_detail(task, browser_page, checkpoint.url)

    def _resume_no_model(
        self,
        task: WebsiteTask,
        browser_page: Any,
        search_url: str,
    ) -> AdapterObservation:
        browser_page.goto(search_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        self._raise_if_authentication_blocked(browser_page)
        self._wait_for_valid_store_search_url(
            browser_page,
            task.model_name,
        )
        self._require_approved_store(browser_page)
        result_region = self._wait_for_result_region(browser_page)
        result_search_input = self._validated_result_search_input(
            browser_page,
            task.model_name,
        )
        cards = visible_locators(result_region, JD_PRODUCT_CARDS)
        if self._exact_product_cards(cards, task.model_name):
            raise LayoutRecognitionError(
                "JD exact product appeared during checkpoint recovery"
            )
        return self._no_model_observation(
            task,
            browser_page,
            result_region,
            result_search_input,
        )

    def _observe_with_stage(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        stage: list[str],
    ) -> AdapterObservation:
        self._validate_task(task)
        browser_page = _playwright_page(page)
        self._raise_if_authentication_blocked(browser_page)
        current_search_result = self._current_search_result(
            task,
            browser_page,
        )
        if current_search_result is not None:
            stage[0] = "京东搜索页"
            if isinstance(current_search_result, AdapterObservation):
                return current_search_result
            detail_url = current_search_result
        else:
            browser_page.goto(self.spec.entry_url, wait_until="domcontentloaded")
            self._raise_if_authentication_blocked(browser_page)
            self._require_approved_store(browser_page)
            detail_url = self._exact_entry_product_url(
                browser_page,
                task.model_name,
            )
        if detail_url is None:
            search_input, search_action = self._wait_for_store_search_controls(
                browser_page,
            )
            stage[0] = "京东搜索页"
            search_input.fill(task.model_name)
            click_and_wait_for_navigation(
                browser_page,
                search_action,
                semantic_name="京东店铺搜索",
            )
            self._raise_if_authentication_blocked(browser_page)
            self._wait_for_valid_store_search_url(
                browser_page,
                task.model_name,
            )
            searched = self._search_result(task, browser_page)
            if isinstance(searched, AdapterObservation):
                return searched
            detail_url = searched
        stage[0] = "京东商品详情页"
        return self._observe_detail(task, browser_page, detail_url)

    def _current_search_result(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> AdapterObservation | str | None:
        """Reuse a verified, visible store-search page after manual handling."""

        try:
            _validate_store_search_url(page.url, expected_model=task.model_name)
        except LayoutRecognitionError:
            return None
        return self._search_result(task, page)

    def _search_result(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> AdapterObservation | str:
        self._require_approved_store(page)
        result_region = self._wait_for_result_region(page)
        result_search_input = self._validated_result_search_input(
            page,
            task.model_name,
        )
        product_cards = visible_locators(result_region, JD_PRODUCT_CARDS)
        empty_states = visible_locators(result_region, JD_EMPTY_RESULTS)
        if not product_cards:
            if len(empty_states) != 1:
                raise LayoutRecognitionError(
                    "JD result cards are missing and no explicit empty state is visible"
                )
            return self._no_model_observation(
                task,
                page,
                result_region,
                result_search_input,
            )
        if empty_states:
            raise LayoutRecognitionError(
                "JD result cards conflict with an explicit empty state"
            )
        exact_cards = self._exact_product_cards(product_cards, task.model_name)
        if not exact_cards:
            return self._no_model_observation(
                task,
                page,
                result_region,
                result_search_input,
            )
        return self._exact_product_detail_url(
            exact_cards,
            base_url=page.url,
        )

    def _observe_detail(
        self,
        task: WebsiteTask,
        browser_page: Any,
        detail_url: str,
    ) -> AdapterObservation:
        browser_page.goto(detail_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        self._raise_if_authentication_blocked(browser_page)
        if _approved_item_url(browser_page.url, base_url=detail_url) != detail_url:
            raise LayoutRecognitionError(
                "JD product detail redirected away from the approved item"
            )
        if self._wait_for_detail_layout(browser_page):
            return self._observe_modern_detail(task, browser_page, detail_url)
        self._require_approved_detail_seller(browser_page)
        detail_title = unique_visible_locator(
            browser_page,
            JD_DETAIL_TITLES,
            semantic_name="product detail title",
        )
        if not model_matches(task.model_name, detail_title.inner_text()):
            raise LayoutRecognitionError("JD product detail model does not match")

        capacity = self._exact_option(
            browser_page,
            JD_CAPACITY_OPTIONS,
            lambda label: capacity_matches(label, task.ram, task.storage),
            semantic_name="capacity",
        )
        if _is_explicitly_disabled(capacity):
            return self._legal_no(
                task,
                BusinessOutcome.CAPACITY_UNAVAILABLE,
                browser_page,
                (_css_rect(capacity, "capacity"),),
            )
        self._prepare_exact_option(capacity)
        self._wait_for_selected(browser_page, capacity, "capacity")
        self._raise_if_authentication_blocked(browser_page)
        capacity_context_sku = self._wait_for_capacity_context(
            browser_page,
            capacity,
        )

        color = self._exact_option(
            browser_page,
            JD_COLOR_OPTIONS,
            lambda label: _jd_color_matches(task.color, label),
            semantic_name="color",
        )
        color_context_sku = _required_numeric_sku(
            color.get_attribute("data-context-sku"),
            semantic_name="color capacity context",
        )
        if color_context_sku != capacity_context_sku:
            raise LayoutRecognitionError(
                "JD exact color does not bind to the confirmed capacity context"
            )
        if _is_explicitly_disabled(color):
            return self._legal_no(
                task,
                BusinessOutcome.COLOR_UNAVAILABLE,
                browser_page,
                (_css_rect(color, "color"),),
            )
        self._prepare_exact_option(color)
        self._wait_for_selected(browser_page, color, "color")
        self._raise_if_authentication_blocked(browser_page)
        current_sku = self._selected_sku_identity(
            browser_page,
            capacity,
            color,
        )
        final_item_url = _approved_item_url(
            browser_page.url,
            base_url=detail_url,
        )
        final_url_sku = _sku_from_item_url(final_item_url)
        initial_url_sku = _sku_from_item_url(detail_url)
        if final_url_sku != initial_url_sku and final_url_sku != current_sku:
            raise LayoutRecognitionError(
                "JD selected SKU does not explain the changed item URL"
            )

        stock, _stock_locator = self._stock_snapshot(
            browser_page,
            current_sku,
        )

        selected_price, final_stock = self._stable_selected_price(
            browser_page,
            current_sku,
            stock,
        )
        semantic_state = self._semantic_state(
            task,
            browser_page,
            outcome=BusinessOutcome.PRICE_FOUND,
            price=selected_price,
            rectangles=(),
            current_sku=current_sku,
            region=final_stock.region,
            stock_state=final_stock.state,
        )
        return AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=selected_price,
            url=browser_page.url,
            css_rectangles=(),
            semantic_state=semantic_state,
        )

    def _validate_task(self, task: WebsiteTask) -> None:
        if not isinstance(task, WebsiteTask):
            raise TypeError("task must be a WebsiteTask")
        if task.channel is not WebsiteChannel.JD:
            raise ValueError("task channel must be JD")
        if task.brand != self.spec.brand:
            raise ValueError("task brand must match the approved JD store")

    def _raise_if_authentication_blocked(self, page: Any) -> None:
        hostname = (urlsplit(page.url).hostname or "").lower()
        path = urlsplit(page.url).path.lower()
        if (
            hostname == "cfe.m.jd.com"
            or any(marker in path for marker in ("/captcha", "/risk_", "/risk/"))
            or visible_locators(page, JD_RISK_CONTROL_MARKERS)
            or _jd_visible_risk_text(page)
        ):
            raise SecurityVerificationRequired(
                "jd",
                "京东需要人工完成安全验证",
            )
        if (
            hostname == "passport.jd.com"
            or "/login" in path
            or visible_locators(page, JD_LOGIN_MARKERS)
        ):
            raise LoginRequired("jd", "京东需要人工登录")

    def _require_approved_store(self, page: Any) -> None:
        markers = visible_locators(page, JD_STORE_MARKERS)
        if markers:
            if len(markers) != 1 or markers[0].inner_text().strip() != self.spec.store_name:
                raise LayoutRecognitionError("JD visible store identity does not match")
            return
        if self.spec.store_name not in page.title():
            raise LayoutRecognitionError("JD approved store identity is missing")

    def _exact_entry_product_url(
        self,
        page: Any,
        model_name: str,
    ) -> str | None:
        matching_urls: set[str] = set()
        for link in visible_locators(page, JD_PRODUCT_LINKS):
            href = link.get_attribute("href")
            if not _is_result_item_link(href, base_url=page.url):
                continue
            if not _modern_result_card_matches(
                model_name,
                link.inner_text(),
            ):
                continue
            matching_urls.add(
                _approved_result_item_url(href, base_url=page.url)
            )
        if len(matching_urls) > 1:
            raise LayoutRecognitionError(
                "JD exact entry product result is ambiguous"
            )
        return next(iter(matching_urls), None)

    def _wait_for_store_search_controls(self, page: Any) -> tuple[Any, Any]:
        for poll in range(_MAX_STORE_READY_POLLS):
            self._raise_if_authentication_blocked(page)
            try:
                self._require_approved_store(page)
                search_container = unique_visible_locator(
                    page,
                    JD_STORE_SEARCH_CONTAINERS,
                    semantic_name="store search container",
                )
                search_input = unique_visible_locator(
                    search_container,
                    JD_SEARCH_INPUTS,
                    semantic_name="store search input",
                )
                search_action = unique_visible_locator(
                    search_container,
                    JD_SEARCH_ACTIONS,
                    semantic_name="store search action",
                )
                return search_input, search_action
            except LayoutRecognitionError:
                if poll + 1 == _MAX_STORE_READY_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("JD store readiness loop did not return or raise")

    def _wait_for_valid_store_search_url(self, page: Any, model_name: str) -> bool:
        for poll in range(_MAX_SEARCH_URL_POLLS):
            self._raise_if_authentication_blocked(page)
            try:
                _validate_store_search_url(page.url, expected_model=model_name)
                return True
            except LayoutRecognitionError:
                if _is_approved_queryless_store_search_url(page.url):
                    return False
                if poll + 1 == _MAX_SEARCH_URL_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("JD store-search URL readiness loop did not return")

    def _wait_for_result_region(self, page: Any) -> Any:
        for poll in range(_MAX_RESULT_READY_POLLS):
            self._raise_if_authentication_blocked(page)
            try:
                return unique_visible_locator(
                    page,
                    JD_RESULT_REGIONS,
                    semantic_name="result region",
                )
            except LayoutRecognitionError:
                if poll + 1 == _MAX_RESULT_READY_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("JD result-region readiness loop did not return")

    def _wait_for_detail_layout(self, page: Any) -> bool:
        """Wait for one detail layout and its exact approved seller identity."""

        for poll in range(_MAX_DETAIL_READY_POLLS):
            self._raise_if_authentication_blocked(page)
            modern_titles = visible_locators(page, JD_MODERN_DETAIL_TITLES)
            legacy_titles = visible_locators(page, JD_DETAIL_TITLES)
            if modern_titles and legacy_titles:
                raise LayoutRecognitionError(
                    "JD product detail layout is ambiguous"
                )
            if modern_titles:
                sellers = visible_locators(
                    page,
                    JD_MODERN_DETAIL_SELLER_MARKERS,
                )
                if sellers:
                    if len(sellers) != 1 or not _modern_seller_matches(
                        sellers[0].inner_text(),
                        self.spec.store_name,
                    ):
                        raise LayoutRecognitionError(
                            "JD modern product detail seller does not match "
                            "the approved store"
                        )
                    return True
            elif legacy_titles:
                sellers = visible_locators(page, JD_DETAIL_SELLER_MARKERS)
                if sellers:
                    if (
                        len(sellers) != 1
                        or sellers[0].inner_text().strip()
                        != self.spec.store_name
                    ):
                        raise LayoutRecognitionError(
                            "JD product detail seller does not match the "
                            "approved store"
                        )
                    return False
            if poll + 1 == _MAX_DETAIL_READY_POLLS:
                raise LayoutRecognitionError(
                    "JD product detail title or approved seller is missing"
                )
            page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("JD detail-layout readiness loop did not return")

    def _require_approved_detail_seller(self, page: Any) -> None:
        sellers = visible_locators(page, JD_DETAIL_SELLER_MARKERS)
        if len(sellers) != 1 or not _modern_seller_matches(
            sellers[0].inner_text(),
            self.spec.store_name,
        ):
            raise LayoutRecognitionError(
                "JD product detail seller does not match the approved store"
            )

    def _observe_modern_detail(
        self,
        task: WebsiteTask,
        page: Any,
        detail_url: str,
    ) -> AdapterObservation:
        """Handle JD's current React product page without changing legacy flow."""

        sellers = visible_locators(page, JD_MODERN_DETAIL_SELLER_MARKERS)
        if len(sellers) != 1 or not _modern_seller_matches(
            sellers[0].inner_text(),
            self.spec.store_name,
        ):
            raise LayoutRecognitionError(
                "JD modern product detail seller does not match the approved store"
            )
        title = unique_visible_locator(
            page,
            JD_MODERN_DETAIL_TITLES,
            semantic_name="modern product detail title",
        )
        if not _modern_result_card_matches(task.model_name, title.inner_text()):
            raise LayoutRecognitionError("JD modern product detail model does not match")
        self._wait_for_modern_sku_options(page, task)
        capacity = self._exact_option(
            page,
            JD_MODERN_SKU_OPTIONS,
            lambda label: capacity_matches(
                _modern_option_label(label),
                task.ram,
                task.storage,
            ),
            semantic_name="modern capacity",
        )
        if _is_modern_unavailable(capacity) and not _is_modern_selected(
            capacity
        ):
            return self._legal_no(
                task,
                BusinessOutcome.CAPACITY_UNAVAILABLE,
                page,
                (_css_rect(capacity, "capacity"),),
            )
        def capacity_matcher(label: str) -> bool:
            return capacity_matches(
                _modern_option_label(label),
                task.ram,
                task.storage,
            )
        self._prepare_exact_option(capacity)
        capacity = self._wait_for_modern_selected(
            page,
            capacity_matcher,
            "capacity",
        )
        self._raise_if_authentication_blocked(page)
        color = self._exact_option(
            page,
            JD_MODERN_SKU_OPTIONS,
            lambda label: _jd_color_matches(
                task.color,
                _modern_option_label(label),
            ),
            semantic_name="modern color",
        )
        if _is_modern_unavailable(color) and not _is_modern_selected(color):
            return self._legal_no(
                task,
                BusinessOutcome.COLOR_UNAVAILABLE,
                page,
                (_css_rect(color, "color"),),
            )
        def color_matcher(label: str) -> bool:
            return _jd_color_matches(task.color, _modern_option_label(label))
        self._prepare_exact_option(color)
        color = self._wait_for_modern_selected(page, color_matcher, "color")
        self._raise_if_authentication_blocked(page)
        selected_labels = {
            _modern_option_label(option.inner_text())
            for option in visible_locators(page, JD_MODERN_SKU_OPTIONS)
            if _is_modern_selected(option)
        }
        expected_capacity = _modern_option_label(f"{task.ram}+{task.storage}")
        has_selected_colour = any(
            _jd_color_matches(task.color, selected_label)
            for selected_label in selected_labels
        )
        if expected_capacity not in selected_labels or not has_selected_colour:
            raise LayoutRecognitionError(
                "JD modern selected options do not match the requested configuration"
            )
        canonical_url = _approved_result_item_url(page.url, base_url=detail_url)
        current_sku = _sku_from_item_url(canonical_url)
        region = unique_visible_locator(
            page,
            JD_MODERN_DELIVERY_REGIONS,
            semantic_name="modern delivery region",
        ).inner_text().strip()
        if not region:
            raise LayoutRecognitionError("JD modern delivery region is blank")
        selected_price = self._modern_selected_price(page)
        semantic_state = self._semantic_state(
            task,
            page,
            outcome=BusinessOutcome.PRICE_FOUND,
            price=selected_price,
            rectangles=(),
            current_sku=current_sku,
            region=region,
            stock_state="JD modern selectable configuration",
            canonical_url=canonical_url,
        )
        return AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=selected_price,
            url=canonical_url,
            css_rectangles=(),
            semantic_state=semantic_state,
        )

    def _wait_for_modern_sku_options(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> None:
        """Keep one JD detail page active while its SKU controls render below fold."""

        for step in range(_MAX_MODERN_SKU_SCAN_STEPS):
            self._raise_if_authentication_blocked(page)
            options = visible_locators(page, JD_MODERN_SKU_OPTIONS)
            has_capacity = any(
                capacity_matches(
                    _modern_option_label(option.inner_text()),
                    task.ram,
                    task.storage,
                )
                for option in options
            )
            has_color = any(
                _jd_color_matches(
                    task.color,
                    _modern_option_label(option.inner_text()),
                )
                for option in options
            )
            if has_capacity and has_color:
                return
            if step + 1 == _MAX_MODERN_SKU_SCAN_STEPS:
                break
            page.evaluate(
                f"() => window.scrollBy(0, {_MODERN_SKU_SCAN_PIXELS})"
            )
            page.wait_for_timeout(_MODERN_SKU_SCAN_INTERVAL_MS)
        raise LayoutRecognitionError(
            "JD modern detail did not reveal the requested SKU options"
        )

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        """Reread the exact selected JD offer before formal capture."""
        self._validate_task(task)
        if (
            not isinstance(expected, VerifiedSemanticState)
            or expected.brand != task.brand
            or expected.model_name != task.model_name
            or expected.capacity != f"{task.ram}+{task.storage}"
            or expected.color != task.color
        ):
            raise LayoutRecognitionError(
                "JD live verified-state reader is unavailable"
            )
        browser_page = _playwright_page(page)

        def read() -> VerifiedSemanticState:
            for poll in range(_MAX_VERIFIED_STATE_POLLS):
                self._raise_if_authentication_blocked(browser_page)
                try:
                    if expected.outcome is not BusinessOutcome.PRICE_FOUND:
                        self._read_legal_no_state(
                            task,
                            browser_page,
                            expected,
                        )
                        # Result-card positioning may scroll the view after
                        # validation.  That must not turn a still-valid
                        # no-model business state into a capture failure.
                        return expected
                    elif expected.css_rectangles:
                        raise LayoutRecognitionError(
                            "JD price state has unexpected evidence rectangles"
                        )
                    elif visible_locators(browser_page, JD_MODERN_DETAIL_TITLES):
                        current = self._read_modern_price_state(
                            task,
                            browser_page,
                            expected.canonical_url,
                        )
                    else:
                        current = self._read_legacy_price_state(
                            task,
                            browser_page,
                            expected.canonical_url,
                        )
                    if expected.outcome is BusinessOutcome.PRICE_FOUND:
                        if _same_quote_price_state(current, expected):
                            # Delivery wording and stock labels are live retail
                            # UI, not quotation fields.  Preserve the first
                            # fully verified offer so their harmless refreshes
                            # cannot invalidate a selected SKU and price just
                            # before macOS captures the screen.
                            return expected
                    elif current == expected:
                        return current
                except LayoutRecognitionError:
                    pass
                if poll + 1 < _MAX_VERIFIED_STATE_POLLS:
                    browser_page.wait_for_timeout(_VERIFIED_STATE_INTERVAL_MS)
            raise LayoutRecognitionError(
                "JD verified offer did not stabilize before capture"
            )

        return read

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        """Read the evidence targets again after final JD positioning."""

        self._validate_task(task)
        browser_page = _playwright_page(page)
        self._raise_if_authentication_blocked(browser_page)
        if expected.outcome is not BusinessOutcome.NO_MODEL:
            return expected.css_rectangles
        self._read_legal_no_state(task, browser_page, expected)
        result_region = unique_visible_locator(
            browser_page,
            JD_RESULT_REGIONS,
            semantic_name="result region",
        )
        result_search_input = self._validated_result_search_input(
            browser_page,
            task.model_name,
        )
        if result_search_input is None:
            return (_css_rect(result_region, "result_region"),)
        return (
            _css_rect(result_search_input, "search_keyword"),
            _css_rect(result_region, "result_region"),
        )

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> None:
        """Apply JD-only 90% framing for the pending formal screenshot."""

        self._validate_task(task)
        browser_page = _playwright_page(page)
        apply_capture_scale(browser_page, scale=0.9)
        try:
            self._prepare_capture_view_at_scale(task, browser_page, expected)
        except BaseException:
            try:
                restore_capture_scale(browser_page)
            except Exception:
                pass
            raise

    def _prepare_capture_view_at_scale(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> None:
        """Verify the selected offer, then position its final screenshot view."""

        self._validate_task(task)
        browser_page = _playwright_page(page)
        self._raise_if_authentication_blocked(browser_page)
        if expected.outcome is BusinessOutcome.NO_MODEL:
            self._read_legal_no_state(task, browser_page, expected)
            result_region = unique_visible_locator(
                browser_page,
                JD_RESULT_REGIONS,
                semantic_name="result region",
            )
            result_search_input = self._validated_result_search_input(
                browser_page,
                task.model_name,
            )
            if result_search_input is None:
                raise LayoutRecognitionError(
                    "JD no-model search keyword is not visible for capture"
                )
            product_cards = visible_locators(result_region, JD_PRODUCT_CARDS)
            product_name = _first_visible_product_title(
                result_region,
                JD_PRODUCT_CARDS,
                JD_PRODUCT_TITLES,
            )
            position_result_cards_for_capture(
                browser_page,
                search_input=result_search_input,
                product_name=product_name,
                product_card=product_cards[0] if product_cards else None,
                site_name="JD",
            )
            return
        if expected.outcome is not BusinessOutcome.PRICE_FOUND:
            return
        if visible_locators(browser_page, JD_MODERN_DETAIL_TITLES):
            current = self._read_modern_price_state(
                task,
                browser_page,
                expected.canonical_url,
            )
            title = unique_visible_locator(
                browser_page,
                JD_MODERN_DETAIL_TITLES,
                semantic_name="modern product detail title",
            )
            options = visible_locators(browser_page, JD_MODERN_SKU_OPTIONS)
            capacity = self._selected_modern_option(
                options,
                lambda label: capacity_matches(
                    _modern_option_label(label), task.ram, task.storage
                ),
                semantic_name="modern capacity",
            )
            color = self._selected_modern_option(
                options,
                lambda label: _jd_color_matches(task.color, _modern_option_label(label)),
                semantic_name="modern color",
            )
            prices = visible_locators(
                browser_page,
                JD_MODERN_CURRENT_SKU_SELLING_PRICES,
            )
        else:
            current = self._read_legacy_price_state(
                task,
                browser_page,
                expected.canonical_url,
            )
            title = unique_visible_locator(
                browser_page,
                JD_DETAIL_TITLES,
                semantic_name="product detail title",
            )
            capacity = self._exact_option(
                browser_page,
                JD_CAPACITY_OPTIONS,
                lambda label: capacity_matches(label, task.ram, task.storage),
                semantic_name="capacity",
            )
            color = self._exact_option(
                browser_page,
                JD_COLOR_OPTIONS,
                lambda label: _jd_color_matches(task.color, label),
                semantic_name="color",
            )
            prices = visible_locators(browser_page, JD_CURRENT_SKU_SELLING_PRICES)
        if not _same_quote_price_state(current, expected):
            raise LayoutRecognitionError("JD verified offer changed before formal capture")
        position_detail_for_capture(
            browser_page,
            title=title,
            prices=prices,
            capacity=capacity,
            color=color,
            site_name="JD",
        )

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> None:
        """Restore the page scale after the runner consumes JD evidence."""

        self._validate_task(task)
        if not isinstance(expected, VerifiedSemanticState):
            raise LayoutRecognitionError(
                "JD capture state is unavailable for restoration"
            )
        restore_capture_scale(_playwright_page(page))

    def _read_legal_no_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> VerifiedSemanticState:
        if expected.outcome is BusinessOutcome.NO_MODEL:
            if page.url != expected.canonical_url:
                raise LayoutRecognitionError(
                    "JD no-model search URL changed before capture"
                )
            self._require_approved_store(page)
            result_region = unique_visible_locator(
                page,
                JD_RESULT_REGIONS,
                semantic_name="result region",
            )
            result_search_input = self._validated_result_search_input(
                page,
                task.model_name,
            )
            product_cards = visible_locators(
                result_region,
                JD_PRODUCT_CARDS,
            )
            empty_states = visible_locators(
                result_region,
                JD_EMPTY_RESULTS,
            )
            if self._exact_product_cards(
                product_cards,
                task.model_name,
            ):
                raise LayoutRecognitionError(
                    "JD exact product appeared before no-model capture"
                )
            if not product_cards and len(empty_states) != 1:
                raise LayoutRecognitionError(
                    "JD no-model evidence changed before capture"
                )
            if product_cards and empty_states:
                raise LayoutRecognitionError(
                    "JD no-model result became conflicting before capture"
                )
            return self._no_model_observation(
                task,
                page,
                result_region,
                result_search_input,
            ).semantic_state

        canonical_url = _approved_item_url(
            page.url,
            base_url=expected.canonical_url,
        )
        if canonical_url != expected.canonical_url:
            raise LayoutRecognitionError(
                "JD product detail URL changed before legal-no capture"
            )
        if visible_locators(page, JD_MODERN_DETAIL_TITLES):
            return self._read_modern_legal_no_state(task, page, expected)
        return self._read_legacy_legal_no_state(task, page, expected)

    def _read_modern_legal_no_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> VerifiedSemanticState:
        sellers = visible_locators(page, JD_MODERN_DETAIL_SELLER_MARKERS)
        if len(sellers) != 1 or not _modern_seller_matches(
            sellers[0].inner_text(),
            self.spec.store_name,
        ):
            raise LayoutRecognitionError(
                "JD modern seller changed before legal-no capture"
            )
        title = unique_visible_locator(
            page,
            JD_MODERN_DETAIL_TITLES,
            semantic_name="modern product detail title",
        )
        if not _modern_result_card_matches(
            task.model_name,
            title.inner_text(),
        ):
            raise LayoutRecognitionError(
                "JD modern model changed before legal-no capture"
            )
        capacity = self._exact_option(
            page,
            JD_MODERN_SKU_OPTIONS,
            lambda label: capacity_matches(
                _modern_option_label(label),
                task.ram,
                task.storage,
            ),
            semantic_name="modern capacity",
        )
        if expected.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE:
            if not _is_modern_unavailable(capacity):
                raise LayoutRecognitionError(
                    "JD unavailable capacity changed before capture"
                )
            return self._legal_no(
                task,
                expected.outcome,
                page,
                (_css_rect(capacity, "capacity"),),
            ).semantic_state
        if not _is_modern_selected(capacity):
            raise LayoutRecognitionError(
                "JD modern capacity changed before legal-no capture"
            )
        color = self._exact_option(
            page,
            JD_MODERN_SKU_OPTIONS,
            lambda label: _jd_color_matches(
                task.color,
                _modern_option_label(label),
            ),
            semantic_name="modern color",
        )
        if (
            expected.outcome is not BusinessOutcome.COLOR_UNAVAILABLE
            or not _is_modern_unavailable(color)
        ):
            raise LayoutRecognitionError(
                "JD modern legal-no state changed before capture"
            )
        return self._legal_no(
            task,
            expected.outcome,
            page,
            (_css_rect(color, "color"),),
        ).semantic_state

    def _read_legacy_legal_no_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> VerifiedSemanticState:
        self._require_approved_detail_seller(page)
        title = unique_visible_locator(
            page,
            JD_DETAIL_TITLES,
            semantic_name="product detail title",
        )
        if not model_matches(task.model_name, title.inner_text()):
            raise LayoutRecognitionError(
                "JD model changed before legal-no capture"
            )
        capacity = self._exact_option(
            page,
            JD_CAPACITY_OPTIONS,
            lambda label: capacity_matches(
                label,
                task.ram,
                task.storage,
            ),
            semantic_name="capacity",
        )
        if expected.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE:
            if not _is_explicitly_disabled(capacity):
                raise LayoutRecognitionError(
                    "JD unavailable capacity changed before capture"
                )
            return self._legal_no(
                task,
                expected.outcome,
                page,
                (_css_rect(capacity, "capacity"),),
            ).semantic_state
        if not _is_approved_selected(capacity):
            raise LayoutRecognitionError(
                "JD capacity changed before legal-no capture"
            )
        color = self._exact_option(
            page,
            JD_COLOR_OPTIONS,
            lambda label: _jd_color_matches(task.color, label),
            semantic_name="color",
        )
        if expected.outcome is BusinessOutcome.COLOR_UNAVAILABLE:
            if not _is_explicitly_disabled(color):
                raise LayoutRecognitionError(
                    "JD unavailable color changed before capture"
                )
            return self._legal_no(
                task,
                expected.outcome,
                page,
                (_css_rect(color, "color"),),
            ).semantic_state
        if (
            expected.outcome is not BusinessOutcome.SOLD_OUT
            or not _is_approved_selected(color)
        ):
            raise LayoutRecognitionError(
                "JD legal-no state changed before capture"
            )
        current_sku = self._selected_sku_identity(
            page,
            capacity,
            color,
        )
        stock, stock_locator = self._stock_snapshot(page, current_sku)
        if not stock.unavailable:
            raise LayoutRecognitionError(
                "JD sold-out state changed before capture"
            )
        return self._legal_no(
            task,
            expected.outcome,
            page,
            (_css_rect(stock_locator, "stock_status"),),
            current_sku=current_sku,
            region=stock.region,
            stock_state=stock.state,
        ).semantic_state

    def _read_modern_price_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected_url: str,
    ) -> VerifiedSemanticState:
        canonical_url = _approved_result_item_url(
            page.url,
            base_url=expected_url,
        )
        if canonical_url != expected_url:
            raise LayoutRecognitionError(
                "JD modern product detail URL changed before capture"
            )
        sellers = visible_locators(page, JD_MODERN_DETAIL_SELLER_MARKERS)
        if len(sellers) != 1 or not _modern_seller_matches(
            sellers[0].inner_text(),
            self.spec.store_name,
        ):
            raise LayoutRecognitionError(
                "JD modern product detail seller changed before capture"
            )
        title = unique_visible_locator(
            page,
            JD_MODERN_DETAIL_TITLES,
            semantic_name="modern product detail title",
        )
        if not _modern_result_card_matches(
            task.model_name,
            title.inner_text(),
        ):
            raise LayoutRecognitionError(
                "JD modern product detail model changed before capture"
            )
        selected_labels = {
            _modern_option_label(option.inner_text())
            for option in visible_locators(page, JD_MODERN_SKU_OPTIONS)
            if _is_modern_selected(option)
        }
        expected_capacity = _modern_option_label(
            f"{task.ram}+{task.storage}"
        )
        if (
            expected_capacity not in selected_labels
            or not any(
                _jd_color_matches(task.color, selected_label)
                for selected_label in selected_labels
            )
        ):
            raise LayoutRecognitionError(
                "JD modern selected configuration changed before capture"
            )
        region = unique_visible_locator(
            page,
            JD_MODERN_DELIVERY_REGIONS,
            semantic_name="modern delivery region",
        ).inner_text().strip()
        if not region:
            raise LayoutRecognitionError(
                "JD modern delivery region changed before capture"
            )
        return self._semantic_state(
            task,
            page,
            outcome=BusinessOutcome.PRICE_FOUND,
            price=self._modern_selected_price(page),
            rectangles=(),
            current_sku=_sku_from_item_url(canonical_url),
            region=region,
            stock_state="JD modern selectable configuration",
            canonical_url=canonical_url,
        )

    def _read_legacy_price_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected_url: str,
    ) -> VerifiedSemanticState:
        canonical_url = _approved_item_url(
            page.url,
            base_url=expected_url,
        )
        if canonical_url != expected_url:
            raise LayoutRecognitionError(
                "JD product detail URL changed before capture"
            )
        self._require_approved_detail_seller(page)
        title = unique_visible_locator(
            page,
            JD_DETAIL_TITLES,
            semantic_name="product detail title",
        )
        if not model_matches(task.model_name, title.inner_text()):
            raise LayoutRecognitionError(
                "JD product detail model changed before capture"
            )
        capacity = self._exact_option(
            page,
            JD_CAPACITY_OPTIONS,
            lambda label: capacity_matches(
                label,
                task.ram,
                task.storage,
            ),
            semantic_name="capacity",
        )
        color = self._exact_option(
            page,
            JD_COLOR_OPTIONS,
            lambda label: _jd_color_matches(task.color, label),
            semantic_name="color",
        )
        if not _is_approved_selected(capacity) or not _is_approved_selected(
            color
        ):
            raise LayoutRecognitionError(
                "JD selected configuration changed before capture"
            )
        current_sku = self._selected_sku_identity(
            page,
            capacity,
            color,
        )
        stock, _stock_locator = self._stock_snapshot(page, current_sku)
        price, final_stock = self._stable_selected_price(
            page,
            current_sku,
            stock,
        )
        return self._semantic_state(
            task,
            page,
            outcome=BusinessOutcome.PRICE_FOUND,
            price=price,
            rectangles=(),
            current_sku=current_sku,
            region=final_stock.region,
            stock_state=final_stock.state,
            canonical_url=canonical_url,
        )

    def _exact_product_cards(
        self,
        cards: tuple[Any, ...],
        model_name: str,
    ) -> tuple[Any, ...]:
        exact: list[Any] = []
        for card in cards:
            card_classes = set((card.get_attribute("class") or "").split())
            if "jItem" in card_classes:
                matches = _modern_result_card_matches(model_name, card.inner_text())
            else:
                titles = visible_locators(card, JD_PRODUCT_TITLES)
                if titles:
                    title = unique_visible_locator(
                        card,
                        JD_PRODUCT_TITLES,
                        semantic_name="product card title",
                    )
                    card_text = title.inner_text()
                else:
                    card_text = card.inner_text()
                matches = model_matches(model_name, card_text)
            if matches:
                exact.append(card)
        return tuple(exact)

    def _exact_product_detail_url(
        self,
        cards: tuple[Any, ...],
        *,
        base_url: str,
    ) -> str:
        available_cards = tuple(
            card for card in cards if not _result_card_is_unavailable(card)
        )
        detail_urls = {
            self._card_detail_url(card, base_url=base_url)
            for card in (available_cards or cards)
        }
        if not detail_urls:
            raise LayoutRecognitionError("JD exact product link is missing")
        if len(detail_urls) != 1:
            raise LayoutRecognitionError("JD exact product result is ambiguous")
        return detail_urls.pop()

    def _card_detail_url(self, card: Any, *, base_url: str) -> str:
        links = visible_locators(card, JD_PRODUCT_LINKS)
        if not links:
            raise LayoutRecognitionError("JD exact product link is missing")
        item_links = tuple(
            link
            for link in links
            if _is_result_item_link(link.get_attribute("href"), base_url=base_url)
        )
        if not item_links:
            raise LayoutRecognitionError("JD exact product link is not approved")
        urls = {
            _approved_result_item_url(link.get_attribute("href"), base_url=base_url)
            for link in item_links
        }
        if len(urls) != 1:
            raise LayoutRecognitionError("JD exact product result is ambiguous")
        return urls.pop()

    def _validated_result_search_input(
        self,
        page: Any,
        model_name: str,
    ) -> Any | None:
        visible_inputs = visible_locators(page, JD_SEARCH_INPUTS)
        if len(visible_inputs) != 1:
            return None
        result_search_input = visible_inputs[0]
        actual_keyword = result_search_input.input_value()
        if not isinstance(actual_keyword, str) or not actual_keyword.strip():
            return None
        if normalize_product_text(actual_keyword) != normalize_product_text(model_name):
            raise LayoutRecognitionError(
                "JD result search keyword does not match the requested model"
            )
        return result_search_input

    def _no_model_observation(
        self,
        task: WebsiteTask,
        page: Any,
        result_region: Any,
        result_search_input: Any | None,
    ) -> AdapterObservation:
        if result_search_input is None:
            product_cards = visible_locators(result_region, JD_PRODUCT_CARDS)
            if not product_cards and visible_locators(page, JD_SEARCH_INPUTS):
                raise LayoutRecognitionError(
                    "JD no-model result lacks a visible verified search keyword"
                )
            # JD can render a complete searched product grid before restoring
            # the result-page input value.  The approved URL proves the query;
            # when none of those visible cards matches the requested model,
            # the grid itself is the required no-quotation evidence.
            _validate_store_search_url(page.url, expected_model=task.model_name)
            rectangles = (_css_rect(result_region, "result_region"),)
        else:
            rectangles = (
                _css_rect(result_search_input, "search_keyword"),
                _css_rect(result_region, "result_region"),
            )
        return self._legal_no(
            task,
            BusinessOutcome.NO_MODEL,
            page,
            rectangles,
        )

    def _exact_option(
        self,
        page: Any,
        selectors: tuple[str, ...],
        matches: Any,
        *,
        semantic_name: str,
    ) -> Any:
        options = visible_locators(page, selectors)
        if not options:
            raise LayoutRecognitionError(
                f"JD {semantic_name} option structure is missing"
            )
        exact = tuple(option for option in options if matches(option.inner_text()))
        if len(exact) != 1:
            raise LayoutRecognitionError(
                f"JD exact {semantic_name} option is missing or ambiguous"
            )
        return exact[0]

    @staticmethod
    def _selected_modern_option(
        options: tuple[Any, ...],
        matches: Any,
        *,
        semantic_name: str,
    ) -> Any:
        selected = tuple(
            option
            for option in options
            if _is_modern_selected(option)
            and matches(option.inner_text())
        )
        if len(selected) != 1:
            raise LayoutRecognitionError(
                f"JD selected {semantic_name} option is missing or ambiguous"
            )
        return selected[0]

    @staticmethod
    def _prepare_exact_option(option: Any) -> None:
        option.scroll_into_view_if_needed()
        option.click()

    def _wait_for_modern_selected(
        self,
        page: Any,
        matcher: Any,
        semantic_name: str,
    ) -> Any:
        for _ in range(_MAX_MODERN_SELECTION_POLLS):
            option = self._exact_option(
                page,
                JD_MODERN_SKU_OPTIONS,
                matcher,
                semantic_name=f"modern {semantic_name}",
            )
            if _is_modern_selected(option):
                return option
            page.wait_for_timeout(_MODERN_SELECTION_INTERVAL_MS)
            self._raise_if_authentication_blocked(page)
        raise LayoutRecognitionError(
            f"JD modern exact {semantic_name} option did not reach a selected state"
        )

    def _modern_selected_price(self, page: Any) -> Decimal:
        previous: tuple[PriceCandidate, ...] | None = None
        for _ in range(_MAX_PRICE_POLLS):
            price_nodes = tuple(
                locator.nth(index)
                for selector in JD_MODERN_CURRENT_SKU_SELLING_PRICES
                for locator in (page.locator(selector),)
                for index in range(locator.count())
                if locator.nth(index).is_visible()
            )
            candidates: list[PriceCandidate] = []
            for locator in price_nodes:
                style = locator.evaluate(_PRICE_STYLE_SCRIPT)
                is_struck_through = (
                    bool(style.get("effectiveLineThrough"))
                    if isinstance(style, dict)
                    else False
                )
                is_struck_through = is_struck_through or (
                    "product-price--gray-line-through"
                    in (locator.get_attribute("class") or "").split()
                )
                context = (
                    "京东当前已选配置"
                    if is_struck_through
                    else locator.evaluate(_PRICE_CONTEXT_SCRIPT)
                )
                if (
                    not isinstance(style, dict)
                    or not isinstance(context, str)
                    or not isinstance(style.get("color"), str)
                    or type(style.get("effectiveLineThrough")) is not bool
                ):
                    raise LayoutRecognitionError("JD modern selling price is invalid")
                candidates.append(
                    PriceCandidate(
                        text=locator.inner_text(),
                        context=context,
                        visible=locator.is_visible(),
                        computed_color=style["color"],
                        selling_evidence=(
                            SellingPriceEvidence.VERIFIED_CURRENT_SKU_STRUCK_THROUGH_PRICE
                            if is_struck_through
                            else SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE
                        ),
                        effective_line_through=style["effectiveLineThrough"],
                    )
                )
            selected = choose_price(tuple(candidates), self.spec.price_policy)
            if selected is not None and tuple(candidates) == previous:
                return selected
            previous = tuple(candidates)
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_authentication_blocked(page)
        raise NonRetryableTechnicalError(
            "NO_VALID_SELLING_PRICE",
            "目标配置仅展示补贴价或划线原价，需人工补充",
        )

    def _stock_snapshot(
        self,
        page: Any,
        current_sku: str,
    ) -> tuple[JDStockSample, Any]:
        states = visible_locators(page, JD_STOCK_STATES)
        if len(states) != 1:
            raise LayoutRecognitionError(
                "JD stock state is missing or ambiguous"
            )
        state = states[0]
        stock_sku = _required_numeric_sku(
            state.get_attribute("data-sku"),
            semantic_name="stock state",
        )
        if stock_sku != current_sku:
            raise LayoutRecognitionError(
                "JD stock state does not bind to the current SKU"
            )
        state_text = normalize_product_text(state.inner_text())
        is_available = any(
            marker in state_text for marker in _AVAILABLE_STOCK_MARKERS
        )
        is_unavailable = any(
            marker in state_text for marker in _UNAVAILABLE_STOCK_MARKERS
        )
        if is_available == is_unavailable:
            raise LayoutRecognitionError(
                "JD stock state is unrecognized or conflicting"
            )
        region = unique_visible_locator(
            page,
            JD_DELIVERY_REGIONS,
            semantic_name="delivery region",
        )
        region_text = _normalized_region(region.inner_text())
        if not region_text:
            raise LayoutRecognitionError("JD delivery region is blank")
        return (
            JDStockSample(
                region=region_text,
                state=state_text,
                unavailable=is_unavailable,
            ),
            state,
        )

    def _selected_sku_identity(
        self,
        page: Any,
        capacity: Any,
        color: Any,
    ) -> str:
        marker = unique_visible_locator(
            page,
            JD_CURRENT_SKU_MARKERS,
            semantic_name="current SKU marker",
        )
        current_sku = _required_numeric_sku(
            marker.get_attribute("data-current-sku"),
            semantic_name="current SKU marker",
        )
        capacity_sku = _required_numeric_sku(
            capacity.get_attribute("data-sku"),
            semantic_name="selected capacity",
        )
        color_sku = _required_numeric_sku(
            color.get_attribute("data-sku"),
            semantic_name="selected color",
        )
        if (capacity_sku, color_sku) != (current_sku, current_sku):
            raise LayoutRecognitionError(
                "JD selected options do not bind to the current SKU"
            )
        return current_sku

    def _price_candidates(
        self,
        page: Any,
        current_sku: str,
    ) -> tuple[PriceCandidate, ...] | None:
        candidates: list[PriceCandidate] = []
        price_locators = visible_locators(page, JD_CURRENT_SKU_SELLING_PRICES)
        price_skus = tuple(
            _required_numeric_sku(
                locator.get_attribute("data-sku"),
                semantic_name="current selling price",
            )
            for locator in price_locators
        )
        distinct_skus = set(price_skus)
        if len(distinct_skus) > 1:
            raise LayoutRecognitionError(
                "JD selling price nodes have conflicting SKU bindings"
            )
        if distinct_skus and distinct_skus != {current_sku}:
            return None
        for locator in price_locators:
            style = locator.evaluate(_PRICE_STYLE_SCRIPT)
            if not isinstance(style, dict):
                raise LayoutRecognitionError("JD selling price style is unavailable")
            color = style.get("color")
            line_through = style.get("effectiveLineThrough")
            if not isinstance(color, str) or not color.strip() or type(line_through) is not bool:
                raise LayoutRecognitionError("JD selling price style is invalid")
            candidates.append(
                PriceCandidate(
                    text=locator.inner_text(),
                    context="JD 当前已选 SKU 售价",
                    visible=locator.is_visible(),
                    computed_color=color,
                    selling_evidence=(
                        SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE
                    ),
                    effective_line_through=line_through,
                )
            )
        return tuple(candidates)

    def _wait_for_selected(
        self,
        page: Any,
        option: Any,
        semantic_name: str,
    ) -> None:
        for _ in range(_MAX_SELECTION_POLLS):
            if _is_approved_selected(option):
                return
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_authentication_blocked(page)
        raise LayoutRecognitionError(
            f"JD exact {semantic_name} option did not reach a selected state"
        )

    def _wait_for_capacity_context(
        self,
        page: Any,
        capacity: Any,
    ) -> str:
        capacity_sku = _required_numeric_sku(
            capacity.get_attribute("data-sku"),
            semantic_name="selected capacity",
        )
        for _ in range(_MAX_SELECTION_POLLS):
            marker = unique_visible_locator(
                page,
                JD_CURRENT_SKU_MARKERS,
                semantic_name="current SKU marker",
            )
            current_sku = _required_numeric_sku(
                marker.get_attribute("data-current-sku"),
                semantic_name="current SKU marker",
            )
            if current_sku == capacity_sku:
                return current_sku
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_authentication_blocked(page)
        raise LayoutRecognitionError(
            "JD capacity selection did not update the current SKU context"
        )

    def _stable_selected_price(
        self,
        page: Any,
        current_sku: str,
        expected_stock: JDStockSample,
    ) -> tuple[Decimal, JDStockSample]:
        previous: tuple[PriceCandidate, ...] | None = None
        for _ in range(_MAX_PRICE_POLLS):
            stock, _stock_locator = self._stock_snapshot(
                page,
                current_sku,
            )
            if stock != expected_stock:
                raise LayoutRecognitionError(
                    "JD stock state changed during final price sampling"
                )
            candidates = self._price_candidates(page, current_sku)
            if candidates is None:
                previous = None
                page.wait_for_timeout(_POLL_INTERVAL_MS)
                self._raise_if_authentication_blocked(page)
                continue
            selected = choose_price(candidates, self.spec.price_policy)
            if selected is not None and candidates == previous:
                return selected, stock
            previous = candidates
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_authentication_blocked(page)
        raise LayoutRecognitionError(
            "JD selected variant price did not reach a verified stable state"
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
        semantic_state = JDAdapter._semantic_state(
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
        canonical_url: str | None = None,
    ) -> VerifiedSemanticState:
        return VerifiedSemanticState(
            canonical_url=canonical_url or page.url,
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


def _jd_visible_risk_text(page: Any) -> bool:
    bodies = visible_locators(page, ("body", "main"))
    return any(
        marker in body.inner_text()
        for body in bodies
        for marker in _JD_RISK_CONTROL_TEXTS
    )


def _same_quote_price_state(
    current: VerifiedSemanticState,
    expected: VerifiedSemanticState,
) -> bool:
    """Compare the price-bearing fields that must remain true for a quote."""

    return (
        current.canonical_url == expected.canonical_url
        and current.brand == expected.brand
        and current.model_name == expected.model_name
        and current.capacity == expected.capacity
        and current.color == expected.color
        and current.current_sku == expected.current_sku
        and current.price == expected.price
        and current.outcome is expected.outcome
    )


def _playwright_page(page: BrowserPage) -> Any:
    required = (
        "goto",
        "locator",
        "title",
        "wait_for_load_state",
        "wait_for_timeout",
    )
    if not all(callable(getattr(page, name, None)) for name in required):
        raise TypeError("page must provide the synchronous Playwright page surface")
    return page


def _is_explicitly_disabled(locator: Any) -> bool:
    if locator.get_attribute("disabled") is not None:
        return True
    if locator.get_attribute("aria-disabled") == "true":
        return True
    classes = set((locator.get_attribute("class") or "").lower().split())
    return bool(classes & _DISABLED_CLASSES)


def _is_modern_unavailable(locator: Any) -> bool:
    """Recognize JD's visible modern-SKU unavailable state."""

    classes = set((locator.get_attribute("class") or "").lower().split())
    return "specification-item-sku--lack" in classes or "无货" in locator.inner_text()


def _is_modern_selected(locator: Any) -> bool:
    if _is_approved_selected(locator):
        return True
    classes = set((locator.get_attribute("class") or "").lower().split())
    return "specification-item-sku--selected" in classes


def _modern_option_label(value: str) -> str:
    return normalize_product_text(value).replace("无货", " ").strip()


def _jd_color_matches(target: str, candidate: str) -> bool:
    """Match an exact colour or one unique marketing name for a base colour.

    The base table can provide a generic colour such as ``黑色`` while an
    approved JD SKU exposes HONOR's marketing colour ``幻夜黑``.  The caller
    still requires exactly one visible matching option, so this does not turn
    an ambiguous page into a selection.
    """

    if color_matches(target, candidate):
        return True
    normalized_target = normalize_product_text(target)
    normalized_candidate = normalize_product_text(candidate)
    if normalized_target not in _GENERIC_COLOR_NAMES:
        return False
    base_colour = normalized_target.removesuffix("色")
    return bool(
        base_colour
        and normalized_candidate.endswith(base_colour)
        and "/" not in normalized_candidate
    )


def _is_approved_selected(locator: Any) -> bool:
    if locator.get_attribute("aria-selected") == "true":
        return True
    if locator.get_attribute("aria-checked") == "true":
        return True
    classes = set((locator.get_attribute("class") or "").lower().split())
    return bool(classes & _SELECTED_CLASSES)


def _approved_item_url(raw_href: object, *, base_url: str) -> str:
    if not isinstance(raw_href, str) or not raw_href.strip():
        raise LayoutRecognitionError("JD exact product link is missing")
    try:
        resolved = urljoin(base_url, raw_href.strip())
        parsed = urlsplit(resolved)
        port = parsed.port
        approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "item.jd.com"
            and port is None
            and parsed.username is None
            and parsed.password is None
            and _JD_ITEM_PATH.fullmatch(parsed.path) is not None
            and not parsed.query
            and not parsed.fragment
            and not url_contains_credentials(resolved)
        )
    except ValueError:
        approved = False
    if not approved:
        raise LayoutRecognitionError("JD exact product link is not approved")
    return resolved


def _is_result_item_link(raw_href: object, *, base_url: str) -> bool:
    """Identify a JD item link on a result card without accepting its destination."""

    if not isinstance(raw_href, str) or not raw_href.strip():
        return False
    try:
        parsed = urlsplit(urljoin(base_url, raw_href.strip()))
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "item.jd.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and _JD_ITEM_PATH.fullmatch(parsed.path) is not None
            and not parsed.fragment
        )
    except ValueError:
        return False


def _approved_result_item_url(raw_href: object, *, base_url: str) -> str:
    """Canonicalize a result-card URL; only JD's opaque ``pcdk`` key is allowed."""

    if not _is_result_item_link(raw_href, base_url=base_url):
        raise LayoutRecognitionError("JD exact product link is not approved")
    resolved = urljoin(base_url, str(raw_href).strip())
    parsed = urlsplit(resolved)
    try:
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise LayoutRecognitionError("JD exact product link is not approved") from error
    if any(key != "pcdk" or not value for key, value in query):
        raise LayoutRecognitionError("JD exact product link is not approved")
    return f"https://item.jd.com{parsed.path}"


def _modern_result_card_matches(model_name: str, card_text: str) -> bool:
    """Match a base model in JD's current result-card text without variants."""

    wanted = normalize_product_text(model_name)
    actual = normalize_product_text(card_text)
    if not wanted or not actual or any(
        marker in actual for marker in _JD_RESULT_ACCESSORY_MARKERS
    ):
        return False
    pattern = re.compile(re.escape(wanted).replace(r"\ ", r"\s*"))
    matches = tuple(pattern.finditer(actual))
    if len(matches) != 1:
        return False
    match = matches[0]
    if match.start() and _is_attached_ascii(actual[match.start() - 1]):
        return False
    suffix = actual[match.end() :]
    if suffix and _is_attached_ascii(suffix[0]):
        return False
    meaningful_suffix = suffix.lstrip()
    return not any(
        meaningful_suffix.startswith(variant) for variant in _JD_RESULT_MODEL_VARIANTS
    )


def _modern_seller_matches(actual_name: str, expected_name: str) -> bool:
    """Accept only the known UI labels that JD appends to its shop identity."""

    actual = normalize_product_text(actual_name)
    expected = normalize_product_text(expected_name)
    if not actual or not expected or not actual.startswith(expected):
        return False
    remainder = actual.removeprefix(expected).strip()
    return not remainder or any(
        remainder.startswith(prefix) for prefix in _JD_MODERN_SELLER_UI_SUFFIXES
    )


def _result_card_is_unavailable(card: Any) -> bool:
    """Treat visible result-card stock text as a tie-breaker, never a model match."""

    card_text = normalize_product_text(card.inner_text())
    return any(marker in card_text for marker in _UNAVAILABLE_STOCK_MARKERS)


def _is_attached_ascii(character: str) -> bool:
    return character.isascii() and character.isalnum()


def _validate_store_search_url(
    raw_url: object,
    *,
    expected_model: str,
) -> None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise LayoutRecognitionError("JD store search URL is missing")
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
        structural_approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "mall.jd.com"
            and port is None
            and parsed.username is None
            and parsed.password is None
            and _JD_STORE_SEARCH_PATH.fullmatch(parsed.path) is not None
            and not parsed.fragment
            and not url_contains_credentials(raw_url)
        )
        if not structural_approved or not _valid_percent_encoding(parsed.query):
            raise ValueError
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
        if len(query_items) != 1 or query_items[0][0] != "keyword":
            raise ValueError
        keyword = query_items[0][1]
        if not keyword or "\ufffd" in keyword:
            raise ValueError
        if "%" in keyword:
            if not _valid_percent_encoding(keyword):
                raise ValueError
            keyword = unquote(keyword, encoding="utf-8", errors="strict")
        if not keyword or "\ufffd" in keyword or "%" in keyword:
            raise ValueError
        if normalize_product_text(keyword) != normalize_product_text(expected_model):
            raise ValueError
    except (UnicodeError, ValueError):
        raise LayoutRecognitionError("JD store search URL is not approved")


def _is_approved_queryless_store_search_url(raw_url: object) -> bool:
    """Allow JD's final URL only after it has removed a previously submitted query.

    This is never enough to prove an empty result: a legal no-model outcome still
    requires the visible search field to show the requested model.
    """

    if not isinstance(raw_url, str) or not raw_url.strip():
        return False
    try:
        parsed = urlsplit(raw_url.strip())
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "mall.jd.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and _JD_STORE_SEARCH_PATH.fullmatch(parsed.path) is not None
            and not parsed.query
            and not parsed.fragment
            and not url_contains_credentials(raw_url)
        )
    except ValueError:
        return False


def _valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if _PERCENT_ESCAPE.match(value, index) is None:
            return False
        index += 3
    return True


def _sku_from_item_url(item_url: str) -> str:
    matched = _JD_ITEM_PATH.fullmatch(urlsplit(item_url).path)
    if matched is None:
        raise LayoutRecognitionError("JD item URL SKU is unavailable")
    return urlsplit(item_url).path.removeprefix("/").removesuffix(".html")


def _required_numeric_sku(raw_sku: object, *, semantic_name: str) -> str:
    if (
        not isinstance(raw_sku, str)
        or _NUMERIC_SKU.fullmatch(raw_sku.strip()) is None
    ):
        raise LayoutRecognitionError(
            f"JD {semantic_name} SKU binding is missing or invalid"
        )
    return raw_sku.strip()


def _first_visible_product_title(
    result_region: Any,
    card_selectors: tuple[str, ...],
    title_selectors: tuple[str, ...],
) -> Any | None:
    """Return the first readable title in a visible related-product card."""

    for card in visible_locators(result_region, card_selectors):
        titles = visible_locators(card, title_selectors)
        if titles:
            return titles[0]
    return None


def _css_rect(locator: Any, role: str) -> CssRect:
    box = locator.bounding_box()
    if not isinstance(box, dict):
        raise LayoutRecognitionError("JD evidence rectangle is unavailable")
    try:
        values = tuple(float(box[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise LayoutRecognitionError("JD evidence rectangle is invalid") from error
    if not all(math.isfinite(value) for value in values):
        raise LayoutRecognitionError("JD evidence rectangle is invalid")
    try:
        return CssRect(
            x=values[0],
            y=values[1],
            width=values[2],
            height=values[3],
            role=role,
        )
    except ValueError as error:
        raise LayoutRecognitionError("JD evidence rectangle is invalid") from error

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
    TMALL_DETAIL_SELLER_MARKERS,
    TMALL_DETAIL_TITLES,
    TMALL_DELIVERY_REGIONS,
    TMALL_EMPTY_RESULTS,
    TMALL_LOGIN_MARKERS,
    TMALL_PRODUCT_CARDS,
    TMALL_PRODUCT_LINKS,
    TMALL_PRODUCT_TITLES,
    TMALL_RESULT_REGIONS,
    TMALL_RISK_CONTROL_MARKERS,
    TMALL_SEARCH_ACTIONS,
    TMALL_SEARCH_INPUTS,
    TMALL_SKU_GROUP_LABELS,
    TMALL_SKU_GROUPS,
    TMALL_SKU_OPTION_ROOTS,
    TMALL_SKU_VALUES,
    TMALL_STOCK_STATES,
    TMALL_STORE_MARKERS,
    TMALL_STORE_SEARCH_CONTAINERS,
    TMALL_STORE_SEARCH_FORMS,
    TMALL_SYSTEM_ERROR_MARKERS,
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
_AVAILABLE_STOCK_MARKERS = (
    "现货",
    "有货",
    "库存充足",
)
_DISABLED_CLASSES = frozenset(
    {
        "disabled",
        "disable",
        "sold-out",
        "unavailable",
    }
)
_SELECTED_CLASSES = frozenset({"selected", "checked", "active"})
_TMALL_RESULT_MODEL_VARIANTS = (
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
_TMALL_RESULT_VARIANT_SEPARATORS = " -·"
_TMALL_RESULT_ACCESSORY_MARKERS = (
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
_TMALL_ITEM_PATH = "/item.htm"
_TMALL_SEARCH_PATH = "/"
_TMALL_STORE_FORM_PATH = "/search.htm"
_TMALL_SEARCH_QUERY_KEYS = frozenset(
    {
        "q",
        "type",
        "search",
        "newHeader_b",
        "searcy_type",
        "from",
        "spm",
    }
)
_TMALL_ITEM_OPTIONAL_QUERY_KEYS = frozenset(
    {
        "rn",
        "abbucket",
        "skuId",
        "sku_properties",
        "mi_id",
        "spm",
        "ali_refid",
        "ali_trackid",
        "bxsign",
        "scm",
        "scm_id",
        "utparam",
        "from",
        "source",
    }
)
_NUMERIC_SKU = re.compile(r"^[0-9]+$")
_TMALL_SKU_PROPERTIES = re.compile(r"^[0-9]+:[0-9]+(?:;[0-9]+:[0-9]+)*$")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_LOGIN_HOSTS = frozenset({"login.tmall.com", "login.taobao.com"})
_RISK_HOSTS = frozenset(
    {
        "captcha.tmall.com",
        "sec.taobao.com",
        "aq.taobao.com",
        "verify.taobao.com",
    }
)
_MAX_SELECTION_POLLS = 5
_MAX_PRICE_POLLS = 5
_MAX_STORE_READY_POLLS = 10
_MAX_PRICE_CONTEXT_LENGTH = 1000
_TMALL_CURRENT_SELLING_PRICE_CONTAINERS = (
    "#tbpcDetail_SkuPanelRightWrap",
)
_TMALL_CURRENT_SELLING_PRICE_VALUES = (
    '[class^="highlightPrice--"]',
)
_POLL_INTERVAL_MS = 100
_STORE_READY_INTERVAL_MS = 500
_OBSERVED_SUBSIDY_PRICE_MARKER = "平台加补后"
_STORE_LOGIN_BENEFIT_GATE = "登录后可查看完整店铺优惠权益"
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
        && current.matches
        && (current.matches('[class^="priceWrap--"]')
            || current.matches('[class^="normalPrice--"]'))) {
      contextText = (current.innerText || current.textContent || "").trim();
    }
    current = current.parentElement;
  }
  return {color, effectiveLineThrough, contextText};
}
"""


@dataclass(frozen=True, slots=True)
class TmallStockSample:
    region: str
    state: str


@dataclass(frozen=True, slots=True)
class TmallVisibleConfigurationEvidence:
    title: str
    configuration: tuple[str, str]
    stock: TmallStockSample
    price_candidates: tuple[PriceCandidate, ...]


class TmallAdapter:
    """Fail-closed observer for one approved Tmall flagship store."""

    channel = WebsiteChannel.TMALL

    def __init__(self, spec: SiteSpec) -> None:
        if not isinstance(spec, SiteSpec):
            raise TypeError("spec must be a SiteSpec")
        spec.validate_approved()
        if spec.channel is not WebsiteChannel.TMALL:
            raise ValueError("spec channel must be TMALL")
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
        stage = ["天猫店铺页"]
        try:
            self._validate_task(task)
            browser_page = _playwright_page(page)
            browser_page.goto(self.spec.entry_url, wait_until="domcontentloaded")
            self._wait_for_approved_store(browser_page)
            detail_url = self._exact_entry_product_url(
                browser_page,
                task.model_name,
            )
            if detail_url is None:
                search_input, search_action = self._wait_for_store_search_controls(
                    browser_page,
                )
                stage[0] = "天猫搜索页"
                search_input.fill(task.model_name)
                click_and_wait_for_navigation(
                    browser_page,
                    search_action,
                    semantic_name="天猫店铺搜索",
                )
                self._raise_if_blocked_or_error(browser_page)
                _validate_store_search_url(
                    browser_page.url,
                    expected_host=_required_entry_host(self.spec.entry_url),
                    expected_model=task.model_name,
                )
                self._wait_for_approved_store(browser_page)

                result_region = self._wait_for_result_region(browser_page)
                result_search_input = self._validated_result_search_input(
                    browser_page,
                    task.model_name,
                )
                product_cards = visible_locators(
                    result_region,
                    TMALL_PRODUCT_CARDS,
                )
                empty_states = visible_locators(
                    result_region,
                    TMALL_EMPTY_RESULTS,
                )
                if not product_cards:
                    if len(empty_states) != 1:
                        raise LayoutRecognitionError(
                            "Tmall result cards are missing and no explicit "
                            "empty state is visible"
                        )
                    return self._no_model_observation(
                        task,
                        browser_page,
                        result_region,
                        result_search_input,
                    )
                if empty_states:
                    raise LayoutRecognitionError(
                        "Tmall result cards conflict with an explicit empty state"
                    )
                exact_cards = self._exact_product_cards(
                    product_cards,
                    task.model_name,
                )
                if not exact_cards:
                    return self._no_model_observation(
                        task,
                        browser_page,
                        result_region,
                        result_search_input,
                    )
                detail_url = self._exact_product_detail_url(
                    exact_cards,
                    base_url=browser_page.url,
                )
            stage[0] = "天猫商品详情页"
            return self._observe_detail(task, browser_page, detail_url)
        except LayoutRecognitionError as error:
            if error.stage is not None:
                raise
            raise LayoutRecognitionError(str(error), stage=stage[0]) from error

    def _observe_detail(
        self,
        task: WebsiteTask,
        browser_page: Any,
        detail_url: str,
    ) -> AdapterObservation:
        browser_page.goto(detail_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        self._raise_if_blocked_or_error(browser_page)
        self._require_exact_detail_url(browser_page, detail_url)
        self._wait_for_detail_layout(browser_page, task, detail_url)

        capacity_group = self._sku_option_group(
            browser_page,
            "存储容量",
        )
        capacity = self._exact_option(
            capacity_group,
            TMALL_SKU_VALUES,
            lambda label: capacity_matches(label, task.ram, task.storage),
            semantic_name="capacity",
        )
        if _is_explicitly_disabled(capacity):
            self._require_exact_detail_url(browser_page, detail_url)
            return self._legal_no(
                task,
                BusinessOutcome.CAPACITY_UNAVAILABLE,
                browser_page,
                (_css_rect(capacity, "capacity"),),
            )
        self._prepare_exact_option(capacity)
        self._wait_for_selected(browser_page, capacity, "capacity")
        self._raise_if_blocked_or_error(browser_page)
        self._require_exact_detail_url(browser_page, detail_url)

        color_group = self._sku_option_group(
            browser_page,
            "机身颜色",
        )
        color = self._exact_option(
            color_group,
            TMALL_SKU_VALUES,
            lambda label: _tmall_color_matches(task.color, label),
            semantic_name="color",
        )
        if _is_explicitly_disabled(color):
            self._unique_selected_option(
                capacity_group,
                TMALL_SKU_VALUES,
                lambda label: capacity_matches(label, task.ram, task.storage),
                semantic_name="capacity",
            )
            self._require_exact_detail_url(browser_page, detail_url)
            return self._legal_no(
                task,
                BusinessOutcome.COLOR_UNAVAILABLE,
                browser_page,
                (_css_rect(color, "color"),),
            )
        self._prepare_exact_option(color)
        self._wait_for_selected(browser_page, color, "color")
        self._raise_if_blocked_or_error(browser_page)
        self._require_exact_detail_url(browser_page, detail_url)

        configuration = self._selected_configuration_snapshot(
            browser_page,
            task,
        )
        self._require_exact_detail_url(browser_page, detail_url)

        selected_price, final_stock = self._stable_visible_price(
            browser_page,
            task,
            configuration,
        )
        if self._selected_configuration_snapshot(browser_page, task) != configuration:
            raise LayoutRecognitionError(
                "Tmall selected visible configuration changed before the final result"
            )
        self._require_exact_detail_url(browser_page, detail_url)
        current_sku = f"visible:{configuration[0]}|{configuration[1]}"
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

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> None:
        """Apply Tmall-only 80% framing for the pending formal screenshot."""

        self._validate_task(task)
        browser_page = _playwright_page(page)
        try:
            apply_capture_scale(browser_page, scale=0.8)
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
        """Set the final screenshot view only after the offer is verified."""

        self._validate_task(task)
        browser_page = _playwright_page(page)
        self._raise_if_blocked_or_error(browser_page)
        if expected.outcome is BusinessOutcome.NO_MODEL:
            self._read_legal_no_state(task, browser_page, expected)
            result_region = _unique_visible_locator(
                browser_page,
                TMALL_RESULT_REGIONS,
                semantic_name="result region",
            )
            product_cards = visible_locators(
                result_region,
                TMALL_PRODUCT_CARDS,
            )
            product_name = _first_visible_product_title(
                result_region,
                TMALL_PRODUCT_CARDS,
                TMALL_PRODUCT_TITLES,
            )
            position_result_cards_for_capture(
                browser_page,
                product_name=product_name,
                product_card=product_cards[0] if product_cards else None,
                site_name="Tmall",
            )
            return
        if expected.outcome is not BusinessOutcome.PRICE_FOUND:
            return
        self._require_exact_detail_url(browser_page, expected.canonical_url)
        self._require_approved_detail_seller(browser_page)
        title = self._matching_detail_titles(browser_page, task)[0]
        capacity = self._exact_option(
            self._sku_option_group(browser_page, "存储容量"),
            TMALL_SKU_VALUES,
            lambda label: capacity_matches(label, task.ram, task.storage),
            semantic_name="capacity",
        )
        color = self._exact_option(
            self._sku_option_group(browser_page, "机身颜色"),
            TMALL_SKU_VALUES,
            lambda label: _tmall_color_matches(task.color, label),
            semantic_name="color",
        )
        if not _is_approved_selected(capacity) or not _is_approved_selected(color):
            raise LayoutRecognitionError(
                "Tmall selected configuration changed before formal capture"
            )
        configuration = self._selected_configuration_snapshot(browser_page, task)
        price, stock = self._stable_visible_price(
            browser_page,
            task,
            configuration,
        )
        current_sku = f"visible:{configuration[0]}|{configuration[1]}"
        current = self._semantic_state(
            task,
            browser_page,
            outcome=BusinessOutcome.PRICE_FOUND,
            price=price,
            rectangles=(),
            current_sku=current_sku,
            region=stock.region,
            stock_state=stock.state,
        )
        if current != expected:
            raise LayoutRecognitionError(
                "Tmall verified offer changed before formal capture"
            )
        position_detail_for_capture(
            browser_page,
            title=title,
            prices=self._visible_current_price_locators(browser_page),
            capacity=capacity,
            color=color,
            site_name="Tmall",
        )

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> None:
        """Restore the page scale after the runner consumes Tmall evidence."""

        self._validate_task(task)
        if not isinstance(expected, VerifiedSemanticState):
            raise LayoutRecognitionError(
                "Tmall capture state is unavailable for restoration"
            )
        restore_capture_scale(_playwright_page(page))

    def resume(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        """Navigate to the saved Tmall page and revalidate without store search."""
        self._validate_task(task)
        if (
            not isinstance(checkpoint, WebsiteObservationCheckpoint)
            or checkpoint.task_id != task.task_id
        ):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "天猫恢复检查点与当前任务不一致",
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
        self._raise_if_blocked_or_error(browser_page)
        _validate_store_search_url(
            browser_page.url,
            expected_host=_required_entry_host(self.spec.entry_url),
            expected_model=task.model_name,
        )
        self._wait_for_approved_store(browser_page)
        result_region = self._wait_for_result_region(browser_page)
        result_search_input = self._validated_result_search_input(
            browser_page,
            task.model_name,
        )
        cards = visible_locators(result_region, TMALL_PRODUCT_CARDS)
        if self._exact_product_cards(cards, task.model_name):
            raise LayoutRecognitionError(
                "Tmall exact product appeared during checkpoint recovery"
            )
        return self._no_model_observation(
            task,
            browser_page,
            result_region,
            result_search_input,
        )

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        """Reread the exact selected Tmall offer before formal capture."""
        self._validate_task(task)
        if (
            not isinstance(expected, VerifiedSemanticState)
            or expected.brand != task.brand
            or expected.model_name != task.model_name
            or expected.capacity != f"{task.ram}+{task.storage}"
            or expected.color != task.color
        ):
            raise LayoutRecognitionError(
                "Tmall live verified-state reader is unavailable"
            )
        browser_page = _playwright_page(page)

        def read() -> VerifiedSemanticState:
            self._raise_if_blocked_or_error(browser_page)
            if expected.outcome is not BusinessOutcome.PRICE_FOUND:
                self._read_legal_no_state(
                    task,
                    browser_page,
                    expected,
                )
                # The current page has been revalidated above.  Keep the
                # original state coordinates in the stable semantic probe:
                # final screenshot positioning may deliberately scroll the
                # result list, which changes geometry but not the legal-no
                # business fact.
                return expected
            if expected.css_rectangles:
                raise LayoutRecognitionError(
                    "Tmall price state has unexpected evidence rectangles"
                )
            self._require_exact_detail_url(
                browser_page,
                expected.canonical_url,
            )
            self._require_approved_detail_seller(browser_page)
            self._matching_detail_titles(browser_page, task)
            configuration = self._selected_configuration_snapshot(
                browser_page,
                task,
            )
            price, stock = self._stable_visible_price(
                browser_page,
                task,
                configuration,
            )
            self._require_exact_detail_url(
                browser_page,
                expected.canonical_url,
            )
            current_sku = f"visible:{configuration[0]}|{configuration[1]}"
            current = self._semantic_state(
                task,
                browser_page,
                outcome=BusinessOutcome.PRICE_FOUND,
                price=price,
                rectangles=(),
                current_sku=current_sku,
                region=stock.region,
                stock_state=stock.state,
            )
            if current != expected:
                raise LayoutRecognitionError(
                    "Tmall verified offer changed before formal capture"
                )
            return current

        return read

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        """Read evidence geometry after the final marketplace view is positioned."""

        self._validate_task(task)
        browser_page = _playwright_page(page)
        self._raise_if_blocked_or_error(browser_page)
        if expected.outcome is not BusinessOutcome.NO_MODEL:
            return expected.css_rectangles
        self._read_legal_no_state(task, browser_page, expected)
        result_region = _unique_visible_locator(
            browser_page,
            TMALL_RESULT_REGIONS,
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

    def _read_legal_no_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> VerifiedSemanticState:
        if expected.outcome is BusinessOutcome.NO_MODEL:
            if page.url != expected.canonical_url:
                raise LayoutRecognitionError(
                    "Tmall no-model search URL changed before capture"
                )
            self._require_approved_store(page)
            result_region = _unique_visible_locator(
                page,
                TMALL_RESULT_REGIONS,
                semantic_name="result region",
            )
            result_search_input = self._validated_result_search_input(
                page,
                task.model_name,
            )
            cards = visible_locators(
                result_region,
                TMALL_PRODUCT_CARDS,
            )
            if self._exact_product_cards(cards, task.model_name):
                raise LayoutRecognitionError(
                    "Tmall exact product appeared before no-model capture"
                )
            return self._no_model_observation(
                task,
                page,
                result_region,
                result_search_input,
            ).semantic_state

        self._require_exact_detail_url(
            page,
            expected.canonical_url,
        )
        self._require_approved_detail_seller(page)
        self._matching_detail_titles(page, task)
        capacity_group = self._sku_option_group(page, "存储容量")
        capacity = self._exact_option(
            capacity_group,
            TMALL_SKU_VALUES,
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
                    "Tmall unavailable capacity changed before capture"
                )
            return self._legal_no(
                task,
                expected.outcome,
                page,
                (_css_rect(capacity, "capacity"),),
            ).semantic_state
        if (
            expected.outcome is not BusinessOutcome.COLOR_UNAVAILABLE
            or not _is_approved_selected(capacity)
        ):
            raise LayoutRecognitionError(
                "Tmall capacity changed before legal-no capture"
            )
        self._unique_selected_option(
            capacity_group,
            TMALL_SKU_VALUES,
            lambda label: capacity_matches(label, task.ram, task.storage),
            semantic_name="capacity",
        )
        color = self._exact_option(
            self._sku_option_group(page, "机身颜色"),
            TMALL_SKU_VALUES,
            lambda label: _tmall_color_matches(task.color, label),
            semantic_name="color",
        )
        if not _is_explicitly_disabled(color):
            raise LayoutRecognitionError(
                "Tmall unavailable color changed before capture"
            )
        return self._legal_no(
            task,
            expected.outcome,
            page,
            (_css_rect(color, "color"),),
        ).semantic_state

    def _validate_task(self, task: WebsiteTask) -> None:
        if not isinstance(task, WebsiteTask):
            raise TypeError("task must be a WebsiteTask")
        if task.channel is not WebsiteChannel.TMALL:
            raise ValueError("task channel must be TMALL")
        if task.brand != self.spec.brand:
            raise ValueError("task brand must match the approved Tmall store")

    def _raise_if_blocked_or_error(self, page: Any) -> None:
        try:
            parsed = urlsplit(page.url)
            hostname = (parsed.hostname or "").lower()
            path = parsed.path.lower()
        except ValueError:
            hostname = ""
            path = ""
        if (
            hostname in _RISK_HOSTS
            or "/_____tmd_____/punish" in path
            or visible_locators(page, TMALL_RISK_CONTROL_MARKERS)
        ):
            raise SecurityVerificationRequired(
                "tmall",
                "天猫需要人工完成安全验证",
            )
        if hostname in _LOGIN_HOSTS or visible_locators(page, TMALL_LOGIN_MARKERS):
            raise LoginRequired("tmall", "天猫需要人工登录")
        if _visible_page_contains(page, _STORE_LOGIN_BENEFIT_GATE):
            raise LoginRequired("tmall", "天猫需要人工登录")
        if visible_locators(page, TMALL_SYSTEM_ERROR_MARKERS):
            raise LayoutRecognitionError("Tmall recognized system error page")

    def _require_approved_store(self, page: Any) -> None:
        markers = _all_visible_locators(page, TMALL_STORE_MARKERS)
        if not markers:
            raise LayoutRecognitionError("Tmall approved store identity is missing")
        if any(
            marker.inner_text().strip() != self.spec.store_name
            for marker in markers
        ):
            raise LayoutRecognitionError(
                "Tmall visible store identity does not match"
            )

    def _wait_for_approved_store(self, page: Any) -> None:
        for poll in range(_MAX_STORE_READY_POLLS):
            self._raise_if_blocked_or_error(page)
            try:
                self._require_approved_store(page)
                return
            except LayoutRecognitionError:
                if poll + 1 == _MAX_STORE_READY_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("Tmall approved-store readiness loop did not return")

    def _exact_entry_product_url(
        self,
        page: Any,
        model_name: str,
    ) -> str | None:
        matching_urls = {
            _approved_item_url(
                link.get_attribute("href"),
                base_url=page.url,
            )
            for link in visible_locators(page, TMALL_PRODUCT_LINKS)
            if model_matches(model_name, link.inner_text())
        }
        if len(matching_urls) > 1:
            raise LayoutRecognitionError(
                "Tmall exact entry product result is ambiguous"
            )
        return next(iter(matching_urls), None)

    def _wait_for_store_search_controls(self, page: Any) -> tuple[Any, Any]:
        for poll in range(_MAX_STORE_READY_POLLS):
            self._raise_if_blocked_or_error(page)
            try:
                self._require_approved_store(page)
                _unique_locator(
                    page,
                    TMALL_STORE_SEARCH_FORMS,
                    semantic_name="store search navigation form",
                )
                search_container = _unique_visible_locator(
                    page,
                    TMALL_STORE_SEARCH_CONTAINERS,
                    semantic_name="store search container",
                )
                search_input = _unique_visible_locator(
                    search_container,
                    TMALL_SEARCH_INPUTS,
                    semantic_name="store search input",
                )
                search_action = _unique_visible_locator(
                    search_container,
                    TMALL_SEARCH_ACTIONS,
                    semantic_name="store search action",
                )
                return search_input, search_action
            except LayoutRecognitionError:
                if poll + 1 == _MAX_STORE_READY_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("Tmall store readiness loop did not return or raise")

    def _wait_for_result_region(self, page: Any) -> Any:
        for poll in range(_MAX_STORE_READY_POLLS):
            self._raise_if_blocked_or_error(page)
            try:
                return _unique_visible_locator(
                    page,
                    TMALL_RESULT_REGIONS,
                    semantic_name="result region",
                )
            except LayoutRecognitionError:
                if poll + 1 == _MAX_STORE_READY_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("Tmall result-region readiness loop did not return")

    def _wait_for_detail_layout(
        self,
        page: Any,
        task: WebsiteTask,
        detail_url: str,
    ) -> None:
        for poll in range(_MAX_STORE_READY_POLLS):
            self._raise_if_blocked_or_error(page)
            try:
                self._require_exact_detail_url(page, detail_url)
                self._require_approved_detail_seller(page)
                self._matching_detail_titles(page, task)
                return
            except LayoutRecognitionError:
                if poll + 1 == _MAX_STORE_READY_POLLS:
                    raise
                page.wait_for_timeout(_STORE_READY_INTERVAL_MS)
        raise AssertionError("Tmall detail-layout readiness loop did not return")

    def _require_approved_detail_seller(self, page: Any) -> None:
        sellers = visible_locators(page, TMALL_DETAIL_SELLER_MARKERS)
        if (
            len(sellers) != 1
            or sellers[0].inner_text().strip() != self.spec.store_name
        ):
            raise LayoutRecognitionError(
                "Tmall product detail seller does not match the approved store"
            )

    @staticmethod
    def _matching_detail_titles(page: Any, task: WebsiteTask) -> tuple[Any, ...]:
        matching: list[Any] = []
        # Tmall's generated ItemTitle classname is not a contract.  Inspect
        # each bounded title family and retain only a title whose visible text
        # proves the requested base model.
        for selector in TMALL_DETAIL_TITLES:
            locator = page.locator(selector)
            for index in range(locator.count()):
                title = locator.nth(index)
                if title.is_visible() and _tmall_result_card_matches(
                    task.model_name,
                    title.inner_text(),
                ):
                    matching.append(title)
        if not matching:
            raise LayoutRecognitionError("Tmall product detail model does not match")
        return tuple(matching)

    @staticmethod
    def _require_exact_detail_url(page: Any, detail_url: str) -> None:
        if _approved_item_url(page.url, base_url=detail_url) != detail_url:
            raise LayoutRecognitionError(
                "Tmall product detail URL changed from the approved item"
            )

    def _exact_product_cards(
        self,
        cards: tuple[Any, ...],
        model_name: str,
    ) -> tuple[Any, ...]:
        exact: list[Any] = []
        for card in cards:
            title = _unique_visible_locator(
                card,
                TMALL_PRODUCT_TITLES,
                semantic_name="product card title",
            )
            if _tmall_result_card_matches(model_name, title.inner_text()):
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
        candidates = available_cards or cards
        if not candidates:
            raise LayoutRecognitionError("Tmall exact product link is missing")
        # The same approved base model can appear more than once with separate
        # platform offers.  Stock status remains the first tie-breaker; within
        # the same visible state retain the store's rendered order so the
        # selection is repeatable rather than treating normal duplicate offers
        # as a technical fault.
        return self._card_detail_url(candidates[0], base_url=base_url)

    @staticmethod
    def _card_detail_url(card: Any, *, base_url: str) -> str:
        product_link = _unique_visible_locator(
            card,
            TMALL_PRODUCT_LINKS,
            semantic_name="exact product link",
        )
        return _approved_result_item_url(
            product_link.get_attribute("href"),
            base_url=base_url,
        )

    def _validated_result_search_input(
        self,
        page: Any,
        model_name: str,
    ) -> Any | None:
        result_search_input = _unique_visible_locator(
            page,
            TMALL_SEARCH_INPUTS,
            semantic_name="result search keyword",
        )
        actual_keyword = result_search_input.input_value()
        if not isinstance(actual_keyword, str):
            raise LayoutRecognitionError(
                "Tmall result search keyword does not match the requested model"
            )
        if normalize_product_text(actual_keyword) == normalize_product_text(model_name):
            return result_search_input
        if actual_keyword.strip():
            raise LayoutRecognitionError(
                "Tmall result search keyword does not match the requested model"
            )
        try:
            _validate_store_search_url(
                page.url,
                expected_host=_required_entry_host(self.spec.entry_url),
                expected_model=model_name,
            )
        except LayoutRecognitionError:
            raise LayoutRecognitionError(
                "Tmall result search keyword does not match the requested model"
            ) from None
        return None

    def _no_model_observation(
        self,
        task: WebsiteTask,
        page: Any,
        result_region: Any,
        result_search_input: Any | None,
    ) -> AdapterObservation:
        if result_search_input is None:
            # The live store keeps the result input blank after a successful
            # GBK/UTF-8 query.  The approved result URL has already proven the
            # requested model, so retain the visible result region as evidence.
            return self._legal_no(
                task,
                BusinessOutcome.NO_MODEL,
                page,
                (_css_rect(result_region, "result_region"),),
            )
        return self._legal_no(
            task,
            BusinessOutcome.NO_MODEL,
            page,
            (
                _css_rect(result_search_input, "search_keyword"),
                _css_rect(result_region, "result_region"),
            ),
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
                f"Tmall {semantic_name} option structure is missing"
            )
        exact = tuple(option for option in options if matches(option.inner_text()))
        if len(exact) != 1:
            raise LayoutRecognitionError(
                f"Tmall exact {semantic_name} option is missing or ambiguous"
            )
        return exact[0]

    @staticmethod
    def _prepare_exact_option(option: Any) -> None:
        option.scroll_into_view_if_needed()
        option.click()

    def _sku_option_group(self, page: Any, label_text: str) -> Any:
        option_root = _unique_visible_locator(
            page,
            TMALL_SKU_OPTION_ROOTS,
            semantic_name="SKU option root",
        )
        groups = visible_locators(option_root, TMALL_SKU_GROUPS)
        matched: list[Any] = []
        for group in groups:
            label = _unique_visible_locator(
                group,
                TMALL_SKU_GROUP_LABELS,
                semantic_name="SKU group label",
            )
            if label.inner_text().strip() == label_text:
                matched.append(group)
        if len(matched) != 1:
            raise LayoutRecognitionError(
                f"Tmall {label_text} SKU group is missing or ambiguous"
            )
        return matched[0]

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
            self._raise_if_blocked_or_error(page)
        raise LayoutRecognitionError(
            f"Tmall exact {semantic_name} option did not reach a selected state"
        )

    def _selected_configuration_snapshot(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> tuple[str, str]:
        capacity = self._unique_selected_option(
            self._sku_option_group(page, "存储容量"),
            TMALL_SKU_VALUES,
            lambda label: capacity_matches(label, task.ram, task.storage),
            semantic_name="capacity",
        )
        color = self._unique_selected_option(
            self._sku_option_group(page, "机身颜色"),
            TMALL_SKU_VALUES,
            lambda label: _tmall_color_matches(task.color, label),
            semantic_name="color",
        )
        return (
            normalize_product_text(capacity.inner_text()),
            normalize_product_text(color.inner_text()),
        )

    def _matching_detail_title_snapshot(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> str:
        titles = {
            normalize_product_text(title.inner_text())
            for title in self._matching_detail_titles(page, task)
        }
        if len(titles) != 1:
            raise LayoutRecognitionError(
                "Tmall matching detail title is missing or ambiguous"
            )
        return next(iter(titles))

    def _unique_selected_option(
        self,
        page: Any,
        selectors: tuple[str, ...],
        matches: Any,
        *,
        semantic_name: str,
    ) -> Any:
        options = visible_locators(page, selectors)
        selected = tuple(
            option for option in options if _is_approved_selected(option)
        )
        if len(selected) != 1 or not matches(selected[0].inner_text()):
            raise LayoutRecognitionError(
                f"Tmall final selected {semantic_name} is missing, ambiguous, or changed"
            )
        return selected[0]

    def _visible_stock_sample(self, page: Any) -> TmallStockSample:
        state = _unique_visible_locator(
            page, TMALL_STOCK_STATES, semantic_name="stock state"
        )
        region = _unique_visible_locator(
            page, TMALL_DELIVERY_REGIONS, semantic_name="delivery region"
        )
        state_text = normalize_product_text(state.inner_text())
        if not state_text:
            raise LayoutRecognitionError("Tmall stock state is blank")
        is_available = any(
            marker in state_text for marker in _AVAILABLE_STOCK_MARKERS
        )
        is_unavailable = any(
            marker in state_text for marker in _UNAVAILABLE_STOCK_MARKERS
        )
        if is_available and is_unavailable:
            raise LayoutRecognitionError("Tmall stock state is conflicting")
        if not is_available and not is_unavailable:
            raise LayoutRecognitionError("Tmall stock state is unrecognized")
        region_text = _normalized_region(region.inner_text())
        if not region_text:
            raise LayoutRecognitionError("Tmall delivery region is blank")
        return TmallStockSample(region=region_text, state=state_text)

    def _visible_current_price_locators(self, page: Any) -> tuple[Any, ...]:
        container = _unique_visible_locator(
            page,
            _TMALL_CURRENT_SELLING_PRICE_CONTAINERS,
            semantic_name="current selling price container",
        )
        return visible_locators(
            container,
            _TMALL_CURRENT_SELLING_PRICE_VALUES,
        )

    def _price_candidates(
        self,
        page: Any,
    ) -> tuple[PriceCandidate, ...]:
        candidates: list[PriceCandidate] = []
        price_locators = self._visible_current_price_locators(page)
        for locator in price_locators:
            style = locator.evaluate(_PRICE_STYLE_SCRIPT)
            if not isinstance(style, dict):
                raise LayoutRecognitionError(
                    "Tmall selling price style is unavailable"
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
                    "Tmall selling price context or style is invalid"
                )
            if (
                _OBSERVED_SUBSIDY_PRICE_MARKER in locator.inner_text()
                or _OBSERVED_SUBSIDY_PRICE_MARKER in context
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

    def _visible_configuration_evidence(
        self,
        page: Any,
        task: WebsiteTask,
    ) -> TmallVisibleConfigurationEvidence:
        return TmallVisibleConfigurationEvidence(
            title=self._matching_detail_title_snapshot(page, task),
            configuration=self._selected_configuration_snapshot(page, task),
            stock=self._visible_stock_sample(page),
            price_candidates=self._price_candidates(page),
        )

    def _stable_visible_price(
        self,
        page: Any,
        task: WebsiteTask,
        configuration: tuple[str, str],
    ) -> tuple[Decimal, TmallStockSample]:
        title = self._matching_detail_title_snapshot(page, task)
        transition_sample = self._visible_configuration_evidence(page, task)
        if transition_sample.configuration != configuration:
            raise LayoutRecognitionError(
                "Tmall selected visible configuration changed during result polling"
            )
        if transition_sample.title != title:
            raise LayoutRecognitionError(
                "Tmall matching detail title changed during result polling"
            )
        page.wait_for_timeout(_POLL_INTERVAL_MS)
        self._raise_if_blocked_or_error(page)

        previous: TmallVisibleConfigurationEvidence | None = None
        for _ in range(_MAX_PRICE_POLLS - 1):
            snapshot = self._visible_configuration_evidence(page, task)
            if snapshot.configuration != configuration:
                raise LayoutRecognitionError(
                    "Tmall selected visible configuration changed during result polling"
                )
            if snapshot.title != title:
                raise LayoutRecognitionError(
                    "Tmall matching detail title changed during result polling"
                )
            selected = choose_price(
                snapshot.price_candidates,
                self.spec.price_policy,
            )
            if selected is not None and snapshot == previous:
                return selected, snapshot.stock
            previous = snapshot
            page.wait_for_timeout(_POLL_INTERVAL_MS)
            self._raise_if_blocked_or_error(page)
        raise LayoutRecognitionError(
            "Tmall selected variant price did not reach a verified stable state"
        )

    @staticmethod
    def _legal_no(
        task: WebsiteTask,
        outcome: BusinessOutcome,
        page: Any,
        rectangles: tuple[CssRect, ...],
        *,
        current_sku: str = "not-applicable",
    ) -> AdapterObservation:
        semantic_state = TmallAdapter._semantic_state(
            task,
            page,
            outcome=outcome,
            price=None,
            rectangles=rectangles,
            current_sku=current_sku,
            region="not-applicable",
            stock_state="not-applicable",
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
        raise TypeError("page must provide the synchronous Playwright page surface")
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
            f"Tmall {semantic_name} structural element is missing or ambiguous"
        )
    return visible[0]


def _unique_locator(
    scope: Any,
    selectors: tuple[str, ...],
    *,
    semantic_name: str,
) -> Any:
    for selector in selectors:
        locator = scope.locator(selector)
        if locator.count() == 1:
            return locator.nth(0)
        if locator.count() > 1:
            break
    raise LayoutRecognitionError(
        f"Tmall {semantic_name} structural element is missing or ambiguous"
    )


def _all_visible_locators(
    scope: Any,
    selectors: tuple[str, ...],
) -> tuple[Any, ...]:
    visible: list[Any] = []
    for selector in selectors:
        locator = scope.locator(selector)
        visible.extend(
            candidate
            for index in range(locator.count())
            if (candidate := locator.nth(index)).is_visible()
        )
    return tuple(visible)


def _visible_page_contains(page: Any, text: str) -> bool:
    bodies = visible_locators(page, ("body",))
    return any(text in body.inner_text() for body in bodies)


def _is_explicitly_disabled(locator: Any) -> bool:
    if locator.get_attribute("disabled") is not None:
        return True
    if locator.get_attribute("aria-disabled") == "true":
        return True
    classes = set((locator.get_attribute("class") or "").lower().split())
    return bool(classes & _DISABLED_CLASSES)


def _tmall_color_matches(target: str, candidate: str) -> bool:
    """Match an exact colour or a single marketing label for a base colour."""

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
    return bool(classes & _SELECTED_CLASSES) or any(
        class_name.startswith("isselected--") for class_name in classes
    )


def _result_card_is_unavailable(card: Any) -> bool:
    """Treat visible result-card stock text as a tie-breaker, never a model match."""

    card_text = normalize_product_text(card.inner_text())
    return any(marker in card_text for marker in _UNAVAILABLE_STOCK_MARKERS)


def _tmall_result_card_matches(model_name: str, card_text: str) -> bool:
    """Match an exact base model in Tmall's long, promotion-heavy card title."""

    wanted = normalize_product_text(model_name)
    actual = normalize_product_text(card_text)
    if not wanted or not actual or any(
        marker in actual for marker in _TMALL_RESULT_ACCESSORY_MARKERS
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
    meaningful_suffix = suffix.lstrip(_TMALL_RESULT_VARIANT_SEPARATORS)
    return not any(
        meaningful_suffix.startswith(variant)
        for variant in _TMALL_RESULT_MODEL_VARIANTS
    )


def _is_attached_ascii(character: str) -> bool:
    return character.isascii() and character.isalnum()


def _approved_item_url(raw_href: object, *, base_url: str) -> str:
    if not isinstance(raw_href, str) or not raw_href.strip():
        raise LayoutRecognitionError("Tmall exact product link is missing")
    try:
        resolved = urljoin(base_url, raw_href.strip())
        parsed = urlsplit(resolved)
        port = parsed.port
        if not _valid_percent_encoding(parsed.query):
            raise ValueError
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
        query_keys = tuple(key for key, _value in query_items)
        item_ids = tuple(value for key, value in query_items if key == "id")
        approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "detail.tmall.com"
            and port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == _TMALL_ITEM_PATH
            and len(item_ids) == 1
            and _NUMERIC_SKU.fullmatch(item_ids[0]) is not None
            and len(query_keys) == len(set(query_keys))
            and set(query_keys) <= {"id"} | _TMALL_ITEM_OPTIONAL_QUERY_KEYS
            and all(value for _key, value in query_items)
            and all(
                key != "skuId" or _NUMERIC_SKU.fullmatch(value) is not None
                for key, value in query_items
            )
            and all(
                key != "sku_properties"
                or _TMALL_SKU_PROPERTIES.fullmatch(value) is not None
                for key, value in query_items
            )
            and not parsed.fragment
            and not url_contains_credentials(resolved)
        )
    except (UnicodeError, ValueError):
        approved = False
    if not approved:
        raise LayoutRecognitionError("Tmall exact product link is not approved")
    return f"https://detail.tmall.com/item.htm?id={item_ids[0]}"


def _approved_result_item_url(raw_href: object, *, base_url: str) -> str:
    """Canonicalize a result-card link while discarding inert tracking data.

    A card can carry site-generated tracking parameters.  The task never
    follows those parameters: it retains only the numeric item ID and then
    navigates to the canonical detail URL, where the stricter detail-page
    validation remains in force.
    """

    if not isinstance(raw_href, str) or not raw_href.strip():
        raise LayoutRecognitionError("Tmall exact product link is missing")
    try:
        resolved = urljoin(base_url, raw_href.strip())
        parsed = urlsplit(resolved)
        port = parsed.port
        if not _valid_percent_encoding(parsed.query):
            raise ValueError
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
        query_keys = tuple(key for key, _value in query_items)
        item_ids = tuple(value for key, value in query_items if key == "id")
        approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "detail.tmall.com"
            and port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == _TMALL_ITEM_PATH
            and len(item_ids) == 1
            and _NUMERIC_SKU.fullmatch(item_ids[0]) is not None
            and len(query_keys) == len(set(query_keys))
            # A card URL is never followed with its query string: this
            # function retains only the numeric item ID below. Therefore an
            # unknown non-empty tracking key cannot affect navigation, while
            # the host, path, single ID, credentials and fragment checks stay
            # fail-closed.
            and all(key and value for key, value in query_items)
            and not parsed.fragment
            and not url_contains_credentials(resolved)
        )
    except (UnicodeError, ValueError):
        approved = False
    if not approved:
        raise LayoutRecognitionError("Tmall exact product link is not approved")
    return f"https://detail.tmall.com/item.htm?id={item_ids[0]}"


def _validate_store_search_form_action(
    raw_action: object,
    *,
    expected_host: str,
) -> None:
    if not isinstance(raw_action, str) or not raw_action.strip():
        raise LayoutRecognitionError("Tmall store search form action is missing")
    try:
        resolved = urljoin(f"https://{expected_host}/", raw_action.strip())
        parsed = urlsplit(resolved)
        port = parsed.port
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname != expected_host
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != _TMALL_STORE_FORM_PATH
            or parsed.fragment
            or url_contains_credentials(resolved)
            or not _valid_percent_encoding(parsed.query)
        ):
            raise ValueError
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
        if (
            len(query_items) != 1
            or query_items[0][0] != "scene"
            or not query_items[0][1]
            or "%" in query_items[0][1]
            or "\ufffd" in query_items[0][1]
        ):
            raise ValueError
    except (UnicodeError, ValueError):
        raise LayoutRecognitionError(
            "Tmall store search form action is not approved"
        )


def _validate_store_search_url(
    raw_url: object,
    *,
    expected_host: str,
    expected_model: str,
) -> None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise LayoutRecognitionError("Tmall store search URL is missing")
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
        structural_approved = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == expected_host
            and port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == _TMALL_SEARCH_PATH
            and not parsed.fragment
            and not url_contains_credentials(raw_url)
        )
        if not structural_approved or not _valid_percent_encoding(parsed.query):
            raise ValueError
        if not any(
            _search_query_matches_exact_model(
                parsed.query,
                expected_model=expected_model,
                encoding=encoding,
            )
            for encoding in ("utf-8", "gbk")
        ):
            raise ValueError
    except (UnicodeError, ValueError):
        raise LayoutRecognitionError("Tmall store search URL is not approved")


def _search_query_matches_exact_model(
    query: str,
    *,
    expected_model: str,
    encoding: str,
) -> bool:
    try:
        query_items = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding=encoding,
            errors="strict",
        )
    except UnicodeError:
        return False
    if (
        len(query_items) != len(_TMALL_SEARCH_QUERY_KEYS)
        or {key for key, _value in query_items} != _TMALL_SEARCH_QUERY_KEYS
        or len({key for key, _value in query_items}) != len(query_items)
        or any(not value for _key, value in query_items)
    ):
        return False
    keyword = next(value for key, value in query_items if key == "q")
    candidates = [keyword]
    if "%" in keyword:
        if not _valid_percent_encoding(keyword):
            return False
        for nested_encoding in ("utf-8", "gbk"):
            try:
                candidates.append(
                    unquote(
                        keyword,
                        encoding=nested_encoding,
                        errors="strict",
                    )
                )
            except UnicodeError:
                continue
    return any(
        candidate
        and "\ufffd" not in candidate
        and "%" not in candidate
        and normalize_product_text(candidate)
        == normalize_product_text(expected_model)
        for candidate in candidates
    )


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


def _required_entry_host(entry_url: str) -> str:
    hostname = urlsplit(entry_url).hostname
    if hostname is None:
        raise LayoutRecognitionError("Tmall approved store hostname is missing")
    return hostname.lower()


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
        raise LayoutRecognitionError("Tmall evidence rectangle is unavailable")
    try:
        values = tuple(float(box[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise LayoutRecognitionError("Tmall evidence rectangle is invalid") from error
    if not all(math.isfinite(value) for value in values):
        raise LayoutRecognitionError("Tmall evidence rectangle is invalid")
    try:
        return CssRect(
            x=values[0],
            y=values[1],
            width=values[2],
            height=values[3],
            role=role,
        )
    except ValueError as error:
        raise LayoutRecognitionError("Tmall evidence rectangle is invalid") from error

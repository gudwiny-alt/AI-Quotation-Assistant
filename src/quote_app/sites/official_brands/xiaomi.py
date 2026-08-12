from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.detail_capture_view import (
    ensure_capture_scale,
    restore_capture_scale,
)
from quote_app.sites.matching import capacity_matches, color_matches, model_matches
from quote_app.sites.official_brands.base import LiveOfficialAdapterBase
from quote_app.sites.official_brands.models import (
    ApprovedHostFamily,
    OfficialBusinessState,
    OfficialCaptureView,
    OfficialDetailIdentity,
    OfficialManualAction,
    OfficialOfferSnapshot,
)
from quote_app.sites.protocol import AdapterObservation, BrowserPage
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import LayoutRecognitionError

_DETAIL_PATH = "/shop/buy/detail"
_CARD_PATH = "/shop/buy"
_MONEY = re.compile(r"(?<!\d)(?:[\u00a5￥]\s*)?(\d[\d,]*(?:\.\d{1,2})?)\s*(?:元)?(?!\d)")
_PLAIN_MONEY = re.compile(
    r"^\s*(?:[\u00a5￥]\s*)?\d[\d,]*(?:\.\d{1,2})?\s*(?:元)?\s*$"
)
_LABELED_MONEY = re.compile(
    r"^\s*(?:销售价|售价|现价|当前价)\s*[:：]?\s*"
    r"(?:[\u00a5￥]\s*)?\d[\d,]*(?:\.\d{1,2})?\s*(?:元)?\s*$"
)
_PRICE_CANDIDATE = re.compile(
    r"^\s*(?:(?:销售价|售价|现价|当前价)\s*[:：]?\s*)?"
    r"[\u00a5￥]?\s*\d[\d,.]*\s*元?\s*$"
)
_SELLING_PRICE_WORDS = ("销售价", "售价", "现价", "当前价")
_EXCLUDED_PRICE_WORDS = (
    "起",
    "原价",
    "划线",
    "优惠券",
    "券后",
    "补贴",
    "国补",
    "以旧换新",
    "分期",
    "月供",
    "每期",
    "MICARE",
    "碎屏",
    "延保",
    "云空间",
)
_DISABLED_CLASSES = frozenset(("disabled", "disable", "unavailable"))

_SEARCH_KEYWORD = (
    '[data-xiaomi-role="search-keyword"]',
    'input[role="searchbox"]',
    'input[type="search"]',
    'input[placeholder*="搜索"]',
)
_RESULT_REGIONS = (
    '[data-xiaomi-role="results"]',
    '[class*="search-result"]',
    '[class*="goods-list"]',
    "main",
)
_EMPTY_RESULTS = (
    '[data-xiaomi-role="empty-results"]',
    '[class*="empty-result"]',
    '[class*="no-result"]',
)
_PRODUCT_LINKS = (
    '[data-xiaomi-role="product-link"]',
    'a[href*="/shop/buy?product_id="]',
)
_PRODUCT_TITLES = (
    '[data-xiaomi-role="product-title"]',
    "h2",
    "h3",
    '[class*="title"]',
    '[class*="name"]',
)
_DETAIL_TITLES = ('[data-xiaomi-role="detail-title"]', "h1", "h2")
_CAPACITY_GROUPS = ('[data-xiaomi-role="capacity-group"]',)
_CAPACITY_OPTIONS = ('[data-xiaomi-role="capacity-option"]',)
_COLOR_GROUPS = ('[data-xiaomi-role="color-group"]',)
_COLOR_OPTIONS = ('[data-xiaomi-role="color-option"]',)
_MAIN_PRICE = ('[data-xiaomi-role="main-price"]',)
_PURCHASE_SUMMARIES = (
    '[data-xiaomi-role="purchase-summary"]',
    "aside",
    '[class*="purchase-summary"]',
    '[class*="product-info"]',
    '[class~="product-con"]',
)
_SELLING_PRICES = (
    '[data-xiaomi-role="selling-price"]',
    '[class~="price-info"] > span',
)
_RISK_MARKERS = (
    '[data-xiaomi-role="risk-control"]',
    '[id*="captcha"]',
    '[class*="captcha"]',
)
_PRICE_STYLE_SCRIPT = """
(element) => {
  let current = element;
  let lineThrough = false;
  let color = "";
  let contextText = null;
  while (current) {
    const style = window.getComputedStyle(current);
    if (!color) color = style.color || "";
    if ((style.textDecorationLine || style.textDecoration || "")
        .includes("line-through")) lineThrough = true;
    if (contextText === null && current.dataset) {
      contextText = current.dataset.priceKind || null;
    }
    current = current.parentElement;
  }
  const all = Array.from(document.querySelectorAll("body *"));
  const versionAnchor = all.find(node =>
    (node.textContent || "").trim() === "选择版本");
  const serviceAnchor = all.find(node =>
    (node.textContent || "").includes("选择小米提供的"));
  const beforeVersion = !versionAnchor ||
    Boolean(element.compareDocumentPosition(versionAnchor) & Node.DOCUMENT_POSITION_FOLLOWING);
  const beforeServices = !serviceAnchor ||
    Boolean(element.compareDocumentPosition(serviceAnchor) & Node.DOCUMENT_POSITION_FOLLOWING);
  const nearestContextText = element.parentElement
    ? (element.parentElement.textContent || "").trim()
    : "";
  const nearestContextClass = element.parentElement
    ? String(element.parentElement.className || "")
    : "";
  const purchaseSummary = versionAnchor
    ? versionAnchor.closest(
        "aside,[data-xiaomi-role='purchase-summary']," +
        "[class*='purchase-summary'],[class*='product-info']," +
        "[class~='product-con']")
    : null;
  const nearestContextIsSummary = Boolean(
    purchaseSummary && element.parentElement === purchaseSummary);
  const clone = element.cloneNode(true);
  const sourceDescendants = Array.from(element.querySelectorAll("*"));
  const cloneDescendants = Array.from(clone.querySelectorAll("*"));
  sourceDescendants.forEach((source, index) => {
    const sourceStyle = window.getComputedStyle(source);
    const decoration = sourceStyle.textDecorationLine ||
      sourceStyle.textDecoration || "";
    const isStruck = ["DEL", "S", "STRIKE"].includes(source.tagName) ||
      decoration.includes("line-through");
    const isHidden = sourceStyle.display === "none" ||
      sourceStyle.visibility === "hidden";
    if (isStruck || isHidden) cloneDescendants[index]?.remove();
  });
  const effectiveText = (clone.textContent || "").trim();
  const rect = element.getBoundingClientRect();
  const inViewport = rect.width > 0 && rect.height > 0 &&
    rect.top >= 0 && rect.left >= 0 &&
    rect.bottom <= window.innerHeight && rect.right <= window.innerWidth;
  return {color, effectiveLineThrough: lineThrough, contextText,
          beforeVersion, beforeServices, nearestContextText,
          nearestContextClass, nearestContextIsSummary, effectiveText,
          inViewport};
}
"""


class _PriceUnavailable(LayoutRecognitionError):
    """Transient absence of the structurally approved Xiaomi selling price."""


class XiaomiOfficialAdapter(LiveOfficialAdapterBase):
    """Live Xiaomi official-store adapter isolated from the frozen legacy path."""

    approved_host_families = (
        ApprovedHostFamily("www.mi.com", allow_subdomains=False),
    )

    def __init__(self, spec: Any) -> None:
        super().__init__(spec)
        self._prepared: set[tuple[int, str, str]] = set()

    def _manual_action(self, page: BrowserPage) -> OfficialManualAction | None:
        browser_page = _playwright_page(page)
        # Xiaomi's ordinary "请提前登录" purchase hint is not a login wall.
        if _visible(browser_page, _RISK_MARKERS):
            return OfficialManualAction.SECURITY_VERIFICATION
        return None

    def _observe_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        browser_page = _playwright_page(page)
        search_url = f"https://www.mi.com/shop/search?keyword={quote(task.model_name)}"
        browser_page.goto(search_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        self.require_approved_url(browser_page.url)
        self.raise_if_manual_action(browser_page)
        self._wait_for_search_results(browser_page, task)

        exact_link = self._exact_result_link(browser_page, task.model_name)
        if exact_link is None:
            return self.build_observation(
                task,
                self._no_model_state(task, browser_page),
            )
        href = exact_link.get_attribute("href")
        card_url = _approved_product_url(href, allow_card=True)
        # Navigate the controlled page directly so a target=_blank card cannot
        # strand the runner on the old search tab.
        browser_page.goto(card_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        if not _is_detail_url(browser_page.url):
            browser_page.goto(card_url, wait_until="domcontentloaded")
            browser_page.wait_for_load_state("domcontentloaded")
        if not _is_detail_url(browser_page.url):
            raise LayoutRecognitionError("Xiaomi product card did not reach a detail URL")
        return self._observe_loaded_detail(task, browser_page)

    def _resume_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        browser_page = _playwright_page(page)
        browser_page.goto(checkpoint.url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser_page)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            state = self._read_business_state(task, browser_page)
            if state.outcome is not BusinessOutcome.NO_MODEL:
                raise LayoutRecognitionError("Xiaomi recovered no-model state changed")
            return self.build_observation(task, state)
        if not _is_detail_url(browser_page.url):
            raise LayoutRecognitionError("Xiaomi recovery URL is not a product detail")
        return self._observe_loaded_detail(task, browser_page)

    def _observe_loaded_detail(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> AdapterObservation:
        # Xiaomi's option controls sit below the title/price area.  Scale the
        # detail page before any option locator is allowed to scroll so the
        # title, selling price, capacity and colour can remain in one view.
        try:
            ensure_capture_scale(page, scale=0.8)
            return self._observe_scaled_detail(task, page)
        except Exception:
            restore_capture_scale(page)
            raise

    def _observe_scaled_detail(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> AdapterObservation:
        identity = _detail_identity(page.url)
        self._wait_for_detail_title(page, task.model_name)

        self._wait_for_option_group(page, "capacity", task)
        capacity = self._exact_option(page, "capacity", task)
        if capacity is None or _is_disabled(capacity):
            return self.build_observation(
                task,
                self._configuration_no_state(
                    task,
                    page,
                    identity,
                    BusinessOutcome.CAPACITY_UNAVAILABLE,
                ),
            )
        capacity.scroll_into_view_if_needed()
        capacity.click()
        self._wait_for_selected_option(page, "capacity", task)

        # Xiaomi rebuilds the option DOM after each choice; never reuse locators.
        self._wait_for_option_group(page, "color", task)
        color = self._exact_option(page, "color", task)
        if color is None or _is_disabled(color):
            return self.build_observation(
                task,
                self._configuration_no_state(
                    task,
                    page,
                    identity,
                    BusinessOutcome.COLOR_UNAVAILABLE,
                ),
            )
        color.scroll_into_view_if_needed()
        color.click()
        self._wait_for_selected_option(page, "capacity", task)
        self._wait_for_selected_option(page, "color", task)

        snapshot = self._wait_for_stable_xiaomi_offer(task, page)
        return self.build_observation(
            task,
            OfficialBusinessState.price_found(
                identity=snapshot.identity,
                brand=task.brand,
                model_name=task.model_name,
                capacity=snapshot.capacity,
                color=snapshot.color,
                price=snapshot.price,
            ),
        )

    def _read_business_state(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> OfficialBusinessState:
        browser_page = _playwright_page(page)
        if _is_search_url(browser_page.url):
            self._wait_for_search_results(browser_page, task)
            if self._exact_result_link(browser_page, task.model_name) is not None:
                raise LayoutRecognitionError("Xiaomi exact model appeared after no-model result")
            return self._no_model_state(task, browser_page)

        identity = _detail_identity(browser_page.url)
        self._wait_for_detail_title(browser_page, task.model_name)
        self._wait_for_option_group(browser_page, "capacity", task)
        selected_capacity = self._exact_option(browser_page, "capacity", task)
        if selected_capacity is None or _is_disabled(selected_capacity):
            return self._configuration_no_state(
                task,
                browser_page,
                identity,
                BusinessOutcome.CAPACITY_UNAVAILABLE,
            )
        self._require_unique_selected_option(browser_page, "capacity", task)

        self._wait_for_option_group(browser_page, "color", task)
        selected_color = self._exact_option(browser_page, "color", task)
        if selected_color is None or _is_disabled(selected_color):
            return self._configuration_no_state(
                task,
                browser_page,
                identity,
                BusinessOutcome.COLOR_UNAVAILABLE,
            )
        self._require_unique_selected_option(browser_page, "color", task)
        price = self._read_current_selling_price(browser_page)
        return OfficialBusinessState.price_found(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=_capacity_text(task),
            color=task.color,
            price=price,
        )

    def _offer_matches_task(
        self,
        task: WebsiteTask,
        snapshot: OfficialOfferSnapshot,
    ) -> bool:
        return (
            snapshot.brand == task.brand
            and model_matches(task.model_name, snapshot.model_name)
            and capacity_matches(snapshot.capacity, task.ram, task.storage)
            and color_matches(task.color, snapshot.color)
            and _is_detail_url(snapshot.identity.canonical_url)
            and snapshot.identity.product_key.isdigit()
        )

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> None:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser_page = _playwright_page(page)
        key = (id(browser_page), task.task_id, expected.current_sku)
        if key in self._prepared:
            return
        self.raise_if_manual_action(browser_page)
        if expected.outcome is BusinessOutcome.PRICE_FOUND:
            try:
                ensure_capture_scale(browser_page, scale=0.8)
                proof_locators = self._capture_proof_locators(task, browser_page)
                if not _proof_group_fits_current_viewport(
                    browser_page,
                    proof_locators,
                ):
                    if not _scroll_proof_group_into_view(
                        browser_page,
                        proof_locators,
                    ):
                        raise LayoutRecognitionError(
                            "Xiaomi title, price, capacity and color must fit the same viewport"
                        )
                    proof_locators = self._capture_proof_locators(task, browser_page)
                    if not _proof_group_fits_current_viewport(
                        browser_page,
                        proof_locators,
                    ):
                        raise LayoutRecognitionError(
                            "Xiaomi title, price, capacity and color must fit the same viewport"
                        )
                current = self._read_business_state(task, browser_page)
                if self.build_observation(task, current).semantic_state != expected:
                    raise LayoutRecognitionError(
                        "Xiaomi capture view changed the selected offer"
                    )
            except Exception:
                restore_capture_scale(browser_page)
                raise
        else:
            if expected.outcome is BusinessOutcome.NO_MODEL:
                keyword = self._require_search_keyword(browser_page, task.model_name)
                region = _first_visible(browser_page, _RESULT_REGIONS)
                if region is None or not _scroll_proof_group_into_view(
                    browser_page,
                    (keyword, region),
                ):
                    raise LayoutRecognitionError(
                        "Xiaomi search keyword and results must fit the same viewport"
                    )
            else:
                kind = (
                    "capacity"
                    if expected.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE
                    else "color"
                )
                group = self._option_group(browser_page, kind)
                if group is None:
                    raise LayoutRecognitionError(
                        f"Xiaomi complete {kind} options are unavailable"
                    )
                group.scroll_into_view_if_needed()
            current = self._read_business_state(task, browser_page)
            current_state = self.build_observation(task, current).semantic_state
            if not self._same_legal_no_business_state(current_state, expected):
                raise LayoutRecognitionError("Xiaomi legal-no capture view changed")
        self._prepared.add(key)

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> None:
        browser_page = _playwright_page(page)
        self._prepared.discard(
            (id(browser_page), task.task_id, expected.current_sku)
        )
        if expected.outcome is not BusinessOutcome.NO_MODEL:
            restore_capture_scale(browser_page)

    def _capture_proof_locators(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> tuple[Any, ...]:
        _proof_price, price_locator = self._current_selling_price(page)
        proof_locators = tuple(
            locator
            for locator in (
                self._detail_title(page, task.model_name),
                price_locator,
                self._option_group(page, "capacity"),
                self._option_group(page, "color"),
            )
            if locator is not None
        )
        if len(proof_locators) != 4:
            raise LayoutRecognitionError(
                "Xiaomi title, price, capacity and color are incomplete"
            )
        return proof_locators

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        current = self._read_business_state(task, page)
        current_state = self.build_observation(task, current).semantic_state
        if expected.outcome is BusinessOutcome.PRICE_FOUND:
            if current_state != expected:
                raise LayoutRecognitionError("Xiaomi capture state changed")
            return ()
        if not self._same_legal_no_business_state(current_state, expected):
            raise LayoutRecognitionError("Xiaomi legal-no capture state changed")
        return current.capture_view.css_rectangles

    def _exact_result_link(self, page: Any, model_name: str) -> Any | None:
        exact: list[tuple[str, Any]] = []
        for link in _visible(page, _PRODUCT_LINKS):
            title = _link_title(link)
            if model_matches(model_name, title):
                # An exact visible model with an illegal target is technical
                # drift, not evidence that the model does not exist.
                approved = _approved_product_url(
                    link.get_attribute("href"),
                    allow_card=True,
                )
                product_id = parse_qs(urlsplit(approved).query)["product_id"][0]
                exact.append((product_id, link))
        distinct_ids = {product_id for product_id, _link in exact}
        if len(distinct_ids) > 1:
            raise LayoutRecognitionError("Xiaomi exact model result is ambiguous")
        return exact[0][1] if exact else None

    def _wait_for_search_results(self, page: Any, task: WebsiteTask) -> None:
        parsed = urlsplit(page.url)
        url_keywords = parse_qs(parsed.query, keep_blank_values=True).get("keyword", [])
        if url_keywords != [task.model_name]:
            raise LayoutRecognitionError("Xiaomi result URL keyword does not match the task")
        previous: tuple[tuple[str, str], ...] | None = None
        stable_reads = 0
        elapsed_waits = 0
        for _ in range(41):
            self.raise_if_manual_action(page)
            links = _visible(page, _PRODUCT_LINKS)
            signature = tuple(
                (_link_title(link), str(link.get_attribute("href") or ""))
                for link in links
            )
            if signature == previous:
                stable_reads += 1
            else:
                stable_reads = 0
            previous = signature
            region = _first_visible(page, _RESULT_REGIONS)
            explicit_empty = self._explicit_empty_result(page)
            if stable_reads >= 3 and region is not None:
                exact = self._exact_result_link(page, task.model_name)
                if exact is not None:
                    return
                if elapsed_waits >= 40 and (signature or explicit_empty):
                    return
            if elapsed_waits < 40:
                page.wait_for_timeout(250)
                elapsed_waits += 1
        raise LayoutRecognitionError("Xiaomi search results did not stabilize")

    def _require_search_keyword(self, page: Any, model_name: str) -> Any:
        keyword = _first_visible(page, _SEARCH_KEYWORD)
        parsed = urlsplit(page.url)
        url_keywords = parse_qs(parsed.query, keep_blank_values=True).get("keyword", [])
        if (
            keyword is None
            or _input_value(keyword) != model_name
            or url_keywords != [model_name]
        ):
            raise LayoutRecognitionError("Xiaomi result keyword does not match the task")
        return keyword

    def _explicit_empty_result(self, page: Any) -> bool:
        return _first_visible(page, _EMPTY_RESULTS) is not None

    def _wait_for_detail_title(self, page: Any, model_name: str) -> None:
        for _ in range(21):
            self.raise_if_manual_action(page)
            if self._detail_title(page, model_name) is not None:
                return
            page.wait_for_timeout(250)
        raise LayoutRecognitionError("Xiaomi detail title does not match the task")

    def _wait_for_option_group(
        self,
        page: Any,
        kind: str,
        task: WebsiteTask,
    ) -> None:
        previous: tuple[str, ...] | None = None
        stable_reads = 0
        elapsed_waits = 0
        for _ in range(21):
            self.raise_if_manual_action(page)
            group = self._option_group(page, kind)
            options = self._option_locators(page, kind) if group is not None else ()
            signature = tuple(option.inner_text().strip() for option in options)
            if signature and signature == previous:
                stable_reads += 1
            else:
                stable_reads = 0
            previous = signature
            if stable_reads >= 3:
                target = self._exact_option(page, kind, task)
                if target is not None or elapsed_waits >= 20:
                    return
            if elapsed_waits < 20:
                page.wait_for_timeout(250)
                elapsed_waits += 1
        raise LayoutRecognitionError(f"Xiaomi {kind} options did not stabilize")

    def _wait_for_stable_xiaomi_offer(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> OfficialOfferSnapshot:
        first_snapshot: OfficialOfferSnapshot | None = None
        for tick in range(41):
            try:
                first_snapshot = self._instant_offer_snapshot(task, page)
            except _PriceUnavailable:
                pass
            if first_snapshot is not None:
                break
            if tick < 40:
                page.wait_for_timeout(250)
        if first_snapshot is None:
            raise CaptureQualityError(
                "CAPTURE_UNSTABLE",
                "Xiaomi selling price did not appear within ten seconds",
            )

        previous: OfficialOfferSnapshot | None = first_snapshot
        stable_reads = 1
        for tick in range(40):
            page.wait_for_timeout(250)
            try:
                current = self._instant_offer_snapshot(task, page)
            except _PriceUnavailable:
                previous = None
                stable_reads = 0
            else:
                if current == previous:
                    stable_reads += 1
                else:
                    stable_reads = 1
                previous = current
                # Thirteen reads at 250 ms spacing cover a full three seconds.
                if stable_reads >= 13:
                    return current
        raise CaptureQualityError(
            "CAPTURE_UNSTABLE",
            "Xiaomi selected offer did not remain stable for three seconds",
        )

    def _instant_offer_snapshot(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> OfficialOfferSnapshot:
        self.raise_if_manual_action(page)
        identity = _detail_identity(page.url)
        self._require_exact_detail_title(page, task.model_name)
        self._require_unique_selected_option(page, "capacity", task)
        self._require_unique_selected_option(page, "color", task)
        snapshot = OfficialOfferSnapshot(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=_capacity_text(task),
            color=task.color,
            price=self._read_current_selling_price(page),
        )
        self.require_approved_url(snapshot.identity.canonical_url)
        if not self._offer_matches_task(task, snapshot):
            raise CaptureQualityError(
                "CAPTURE_UNSTABLE",
                "Xiaomi selected offer does not match the task",
            )
        return snapshot

    def _wait_for_selected_option(
        self,
        page: Any,
        kind: str,
        task: WebsiteTask,
    ) -> None:
        for _ in range(20):
            self.raise_if_manual_action(page)
            try:
                self._require_unique_selected_option(page, kind, task)
            except LayoutRecognitionError:
                page.wait_for_timeout(250)
                continue
            return
        raise LayoutRecognitionError(f"Xiaomi selected {kind} option did not stabilize")

    def _require_unique_selected_option(
        self,
        page: Any,
        kind: str,
        task: WebsiteTask,
    ) -> Any:
        options = self._option_locators(page, kind)
        selected = tuple(option for option in options if _is_explicitly_selected(option))
        if len(selected) != 1:
            raise LayoutRecognitionError(f"Xiaomi selected {kind} option is not unique")
        target = selected[0]
        matches = (
            capacity_matches(target.inner_text(), task.ram, task.storage)
            if kind == "capacity"
            else color_matches(task.color, target.inner_text())
        )
        if not matches or _is_disabled(target):
            raise LayoutRecognitionError(f"Xiaomi selected {kind} option is unavailable")
        return target

    def _exact_option(self, page: Any, kind: str, task: WebsiteTask) -> Any | None:
        options = self._option_locators(page, kind)
        exact = [
            option
            for option in options
            if (
                capacity_matches(option.inner_text(), task.ram, task.storage)
                if kind == "capacity"
                else color_matches(task.color, option.inner_text())
            )
        ]
        if len(exact) > 1:
            raise LayoutRecognitionError(f"Xiaomi exact {kind} option is ambiguous")
        return exact[0] if exact else None

    def _option_group(self, page: Any, kind: str) -> Any | None:
        selectors = _CAPACITY_GROUPS if kind == "capacity" else _COLOR_GROUPS
        group = _first_visible(page, selectors)
        if group is not None:
            return group
        anchor = "选择版本" if kind == "capacity" else "选择颜色"
        try:
            anchors = page.get_by_text(anchor, exact=True)
            if anchors.count() < 1:
                return None
            return anchors.nth(0).locator(
                "xpath=following::*[@role='list' or self::ul or self::ol][1]"
            )
        except (AttributeError, RuntimeError):
            return None

    def _option_locators(self, page: Any, kind: str) -> tuple[Any, ...]:
        selectors = _CAPACITY_OPTIONS if kind == "capacity" else _COLOR_OPTIONS
        options = _visible(page, selectors)
        if options:
            return options
        group = self._option_group(page, kind)
        if group is None:
            return ()
        return _visible(group, ('[role="listitem"]', "li", "button"))

    def _detail_title(self, page: Any, model_name: str) -> Any | None:
        summary = self._purchase_summary(page)
        if summary is not None:
            summary_exact = [
                item
                for item in _visible(summary, _DETAIL_TITLES)
                if model_matches(model_name, item.inner_text())
            ]
            if summary_exact:
                return summary_exact[0]
        exact = [
            item
            for item in _visible(page, _DETAIL_TITLES)
            if model_matches(model_name, item.inner_text())
        ]
        if exact:
            return exact[0]
        try:
            headings = page.get_by_role("heading", name=model_name, exact=True)
            return headings.nth(0) if headings.count() else None
        except AttributeError:
            return None

    def _require_exact_detail_title(self, page: Any, model_name: str) -> None:
        if self._detail_title(page, model_name) is None:
            raise LayoutRecognitionError("Xiaomi detail title does not match the task")

    def _read_current_selling_price(self, page: Any) -> Decimal:
        return self._current_selling_price(page)[0]

    def _current_selling_price(self, page: Any) -> tuple[Decimal, Any]:
        summary = self._purchase_summary(page)
        candidates = _visible(summary, _SELLING_PRICES) if summary is not None else ()
        if not candidates and summary is not None:
            try:
                candidates = _visible_locator_items(
                    summary.get_by_text(
                        _PRICE_CANDIDATE
                    )
                )
            except AttributeError:
                candidates = ()
        # Xiaomi changes the purchase-column wrapper class independently of
        # the stable price and option controls.  When that wrapper cannot be
        # identified, keep the same strict price semantics but source the
        # candidate from the whole detail page.
        if not candidates:
            candidates = _visible(page, _SELLING_PRICES)
        if not candidates:
            try:
                candidates = _visible_locator_items(page.get_by_text(_PRICE_CANDIDATE))
            except AttributeError:
                candidates = ()
        values: list[tuple[Decimal, Any]] = []
        for candidate in candidates:
            text = candidate.inner_text().strip()
            style = candidate.evaluate(_PRICE_STYLE_SCRIPT)
            if not isinstance(style, dict):
                continue
            effective_text = (
                str(style["effectiveText"]).strip()
                if "effectiveText" in style
                else text
            )
            if style.get("inViewport") is not True:
                continue
            context = str(style.get("contextText") or "").lower()
            is_fixture_selling = context == "selling"
            if not is_fixture_selling and not (
                _PLAIN_MONEY.fullmatch(effective_text)
                or _LABELED_MONEY.fullmatch(effective_text)
            ):
                continue
            nearest_context = str(style.get("nearestContextText") or "")
            nearest_is_summary = style.get("nearestContextIsSummary") is True
            semantic_text = f"{effective_text} {nearest_context}".upper()
            price_context = (
                effective_text.upper()
                if (
                    is_fixture_selling
                    or _LABELED_MONEY.fullmatch(effective_text)
                    or nearest_is_summary
                )
                else semantic_text
            )
            if any(term in price_context for term in _EXCLUDED_PRICE_WORDS):
                continue
            if bool(style.get("effectiveLineThrough")):
                continue
            color = str(style.get("color") or "").replace(" ", "").lower()
            if not is_fixture_selling and not _looks_red(color):
                continue
            amount = _decimal_amount(effective_text)
            if amount is not None:
                values.append((amount, candidate))
        unique_amounts = tuple(dict.fromkeys(amount for amount, _candidate in values))
        if not unique_amounts:
            raise _PriceUnavailable("Xiaomi current red selling price is unavailable")
        if len(unique_amounts) != 1:
            raise LayoutRecognitionError("Xiaomi current red selling price is unavailable")
        selected_amount = unique_amounts[0]
        selected_locator = next(
            candidate for amount, candidate in values if amount == selected_amount
        )
        return selected_amount, selected_locator

    def _purchase_summary(self, page: Any) -> Any | None:
        fixture_summary = _first_visible(page, _MAIN_PRICE)
        if fixture_summary is not None:
            return fixture_summary
        for summary in _visible(page, _PURCHASE_SUMMARIES):
            try:
                anchors = summary.get_by_text("选择版本", exact=True)
                if anchors.count() and anchors.nth(0).is_visible():
                    return summary
            except (AttributeError, RuntimeError):
                continue
        try:
            anchors = page.get_by_text("选择版本", exact=True)
            if anchors.count() < 1:
                return None
            for xpath in (
                "xpath=ancestor::aside[1]",
                "xpath=ancestor::*[@data-xiaomi-role='purchase-summary'][1]",
                "xpath=ancestor::*[contains(@class,'product-info')][1]",
                "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' product-con ')][1]",
            ):
                summary = anchors.nth(0).locator(xpath)
                if summary.count() and summary.nth(0).is_visible():
                    return summary.nth(0)
        except (AttributeError, RuntimeError):
            return None
        return None

    def _no_model_state(self, task: WebsiteTask, page: Any) -> OfficialBusinessState:
        keyword = self._require_search_keyword(page, task.model_name)
        region = _first_visible(page, _RESULT_REGIONS)
        links = _visible(page, _PRODUCT_LINKS)
        if region is None or (not links and not self._explicit_empty_result(page)):
            raise LayoutRecognitionError("Xiaomi no-model evidence is incomplete")
        if self._exact_result_link(page, task.model_name) is not None:
            raise LayoutRecognitionError("Xiaomi exact model exists on no-model page")
        return OfficialBusinessState.legal_no(
            canonical_url=self.require_approved_url(page.url),
            brand=task.brand,
            model_name=task.model_name,
            capacity=_capacity_text(task),
            color=task.color,
            outcome=BusinessOutcome.NO_MODEL,
            capture_view=OfficialCaptureView(
                (_css_rect(keyword, "search_keyword"), _css_rect(region, "result_region"))
            ),
            detail_identity=None,
        )

    def _configuration_no_state(
        self,
        task: WebsiteTask,
        page: Any,
        identity: OfficialDetailIdentity,
        outcome: BusinessOutcome,
    ) -> OfficialBusinessState:
        kind = "capacity" if outcome is BusinessOutcome.CAPACITY_UNAVAILABLE else "color"
        group = self._option_group(page, kind)
        if group is None:
            raise LayoutRecognitionError(f"Xiaomi complete {kind} options are unavailable")
        target = self._exact_option(page, kind, task)
        if target is not None and not _is_disabled(target):
            raise LayoutRecognitionError(f"Xiaomi target {kind} is available")
        if outcome is BusinessOutcome.COLOR_UNAVAILABLE:
            self._require_unique_selected_option(page, "capacity", task)
        return OfficialBusinessState.legal_no(
            canonical_url=identity.canonical_url,
            brand=task.brand,
            model_name=task.model_name,
            capacity=_capacity_text(task),
            color=task.color,
            outcome=outcome,
            capture_view=OfficialCaptureView((_css_rect(group, kind),)),
            detail_identity=identity.product_key,
        )


def _playwright_page(page: BrowserPage) -> Any:
    if not callable(getattr(page, "locator", None)) or not callable(
        getattr(page, "goto", None)
    ):
        raise TypeError("page must expose the synchronous Playwright page API")
    return page


def _visible(scope: Any, selectors: tuple[str, ...]) -> tuple[Any, ...]:
    found: list[Any] = []
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    found.append(item)
        except (AttributeError, RuntimeError):
            continue
        if found:
            break
    return tuple(found)


def _visible_locator_items(locator: Any) -> tuple[Any, ...]:
    return tuple(
        item
        for index in range(locator.count())
        if (item := locator.nth(index)).is_visible()
    )


def _first_visible(scope: Any, selectors: tuple[str, ...]) -> Any | None:
    matches = _visible(scope, selectors)
    return matches[0] if matches else None


def _link_title(link: Any) -> str:
    for name in ("aria-label", "title"):
        value = link.get_attribute(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = _visible(link, _PRODUCT_TITLES)
    return nested[0].inner_text().strip() if nested else link.inner_text().strip()


def _approved_product_url(value: str | None, *, allow_card: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Xiaomi product URL is missing")
    normalized = value.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    elif normalized.startswith("/"):
        normalized = f"https://www.mi.com{normalized}"
    parsed = urlsplit(normalized)
    valid_path = parsed.path in ({_CARD_PATH, _DETAIL_PATH} if allow_card else {_DETAIL_PATH})
    product_ids = parse_qs(parsed.query, keep_blank_values=True).get("product_id", [])
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.mi.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not valid_path
        or len(product_ids) != 1
        or not product_ids[0].isdigit()
    ):
        raise ValueError("Xiaomi product URL is not an approved numeric product path")
    return normalized


def _is_detail_url(value: str) -> bool:
    try:
        _approved_product_url(value, allow_card=False)
    except ValueError:
        return False
    return True


def _is_search_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.mi.com"
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/shop/search"
    )


def _detail_identity(url: str) -> OfficialDetailIdentity:
    normalized = _approved_product_url(url, allow_card=False)
    product_id = parse_qs(urlsplit(normalized).query)["product_id"][0]
    return OfficialDetailIdentity(normalized, product_id)


def _capacity_text(task: WebsiteTask) -> str:
    return f"{task.ram} + {task.storage}"


def _is_disabled(locator: Any) -> bool:
    if locator.get_attribute("disabled") is not None:
        return True
    if str(locator.get_attribute("aria-disabled") or "").lower() == "true":
        return True
    classes = str(locator.get_attribute("class") or "").lower().split()
    return bool(_DISABLED_CLASSES.intersection(classes))


def _is_explicitly_selected(locator: Any) -> bool:
    explicit_values = (
        locator.get_attribute("aria-selected"),
        locator.get_attribute("aria-checked"),
        locator.get_attribute("data-selected"),
        locator.get_attribute("data-state"),
    )
    normalized = {str(value).lower() for value in explicit_values if value is not None}
    if normalized.intersection(("false", "unselected", "inactive", "unchecked")):
        return False
    if normalized.intersection(("true", "selected", "active", "checked")):
        return True
    classes = set(str(locator.get_attribute("class") or "").lower().split())
    if classes.intersection(("selected", "active", "current", "checked")):
        return True
    try:
        explicit = locator.evaluate(
            """
            (element) => {
              const __xiaomiExplicitSelection = true;
              let current = element;
              while (current) {
                const classes = Array.from(current.classList || [])
                  .map(value => value.toLowerCase());
                if (classes.some(value =>
                    ["selected", "active", "current", "checked"].includes(value))) {
                  return true;
                }
                if (current.dataset) {
                  const values = [current.dataset.selected, current.dataset.state]
                    .map(value => String(value || "").toLowerCase());
                  if (values.some(value =>
                    ["true", "selected", "active", "checked"].includes(value))) {
                    return true;
                  }
                }
                current = current.parentElement;
              }
              return false;
            }
            """
        )
        return explicit is True
    except (AttributeError, RuntimeError):
        return False


def _scroll_proof_group_into_view(page: Any, locators: tuple[Any, ...]) -> bool:
    if not locators or not callable(getattr(page, "evaluate", None)):
        return False
    try:
        handles = [locator.element_handle() for locator in locators]
        if any(handle is None for handle in handles):
            return False
        result = page.evaluate(
            """
            (elements) => {
              const boxes = elements.map(element => element.getBoundingClientRect());
              const top = Math.min(...boxes.map(box => box.top + window.scrollY));
              const bottom = Math.max(...boxes.map(box => box.bottom + window.scrollY));
              window.scrollTo({top: Math.max(0, top - 100), behavior: "instant"});
              return bottom - top <= window.innerHeight - 120;
            }
            """,
            handles,
        )
        return result is True
    except (AttributeError, RuntimeError):
        return False


def _proof_group_fits_current_viewport(
    page: Any,
    locators: tuple[Any, ...],
) -> bool:
    if not locators or not callable(getattr(page, "evaluate", None)):
        return False
    try:
        handles = [locator.element_handle() for locator in locators]
        if any(handle is None for handle in handles):
            return False
        result = page.evaluate(
            """
            (elements) => {
              const __xiaomiProofsFitCurrentViewport = true;
              const boxes = elements.map(element => element.getBoundingClientRect());
              return __xiaomiProofsFitCurrentViewport && boxes.every(box =>
                box.width > 0
                && box.height > 0
                && box.top >= 0
                && box.left >= 0
                && box.bottom <= window.innerHeight
                && box.right <= window.innerWidth
              );
            }
            """,
            handles,
        )
        return result is True
    except (AttributeError, RuntimeError):
        return False


def _input_value(locator: Any) -> str:
    try:
        return locator.input_value().strip()
    except AttributeError:
        return str(locator.get_attribute("value") or "").strip()


def _css_rect(locator: Any, role: str) -> CssRect:
    box = locator.bounding_box()
    if not isinstance(box, dict):
        raise LayoutRecognitionError(f"Xiaomi {role} evidence has no geometry")
    try:
        return CssRect(
            float(box["x"]),
            float(box["y"]),
            float(box["width"]),
            float(box["height"]),
            role,
        )
    except (KeyError, TypeError, ValueError):
        raise LayoutRecognitionError(f"Xiaomi {role} evidence geometry is invalid") from None


def _decimal_amount(text: str) -> Decimal | None:
    match = _MONEY.search(text)
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _looks_red(color: str) -> bool:
    match = re.search(r"rgba?\((\d+),(\d+),(\d+)", color)
    if match is not None:
        red, green, blue = (int(value) for value in match.groups())
        return red >= 160 and red > green * 1.35 and red > blue * 1.35
    return color in {"red", "#f00", "#ff0000"} or color.startswith("#f")

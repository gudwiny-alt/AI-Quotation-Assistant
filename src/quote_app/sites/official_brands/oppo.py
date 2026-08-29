from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError
from quote_app.evidence.semantic_state import SemanticStateReader, VerifiedSemanticState
from quote_app.sites.detail_capture_view import ensure_capture_scale, restore_capture_scale
from quote_app.sites.matching import (
    capacity_matches,
    color_matches,
    model_matches,
    normalize_product_text,
)
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
from quote_app.tasks.models import BusinessOutcome, WebsiteObservationCheckpoint, WebsiteTask
from quote_app.tasks.retry import LayoutRecognitionError

_DETAIL_PATH = re.compile(r"^/cn/web/products/(?P<product_id>\d+)\.html$")
_MONEY = re.compile(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)(?!\d)")
_EXCLUDED_PRICE_WORDS = (
    "优惠券",
    "领券",
    "券后",
    "以旧换新",
    "分期",
    "月供",
    "每期",
    "保障",
    "延保",
    "碎屏",
)
_DISABLED_CLASSES = frozenset(("disabled", "disable", "unavailable"))
_NETWORK_MARKER = re.compile(r"(?<![A-Z0-9])(?:4G|5G)(?![A-Z0-9])")
_MODEL_VARIANT_PREFIXES = frozenset(
    ("PRO", "PLUS", "ULTRA", "MAX", "GT", "S", "T", "SE", "LITE", "NEO")
)

_SEARCH_OPENERS = (
    '[data-oppo-role="search-opener"]',
    ".nav-search",
)
_SEARCH_DIALOGS = (
    '[data-oppo-role="search-dialog"]',
    ".v-dialog",
)
_SEARCH_INPUTS = (
    '[data-oppo-role="search-input"]',
    'input[placeholder="点击搜索"]',
    "input.v-field__input",
    ".v-combobox input",
    ".official-oppo-search-input",
    "input",
)
_SEARCH_QUERY_EVIDENCE = (
    '[data-oppo-role="search-query"]',
    ".v-combobox__selection",
    ".v-autocomplete__selection",
    ".v-select__selection",
    ".v-field",
    '[class*="search-keyword"]',
)
_RESULT_REGIONS = (
    '[data-oppo-role="results"]',
    ".five-warp",
    ".scroll-warp-list",
    ".search-result",
    '[class*="goods-list"]',
    '[class*="search-result"]',
)
_EMPTY_RESULTS = (
    '[data-oppo-role="empty-results"]',
    ".text-caption",
    '[class*="empty-result"]',
    '[class*="no-result"]',
)
_PRODUCT_LINKS = (
    '[data-oppo-role="product-link"]',
    'a[href*="/cn/web/products/"]',
    ".official-oppo-product-link",
)
_PRODUCT_TITLES = (
    '[data-oppo-role="product-title"]',
    '[class*="product-title"]',
    '[class*="goods-name"]',
    '[class*="name"]',
)
_DETAIL_TITLES = (
    '[data-oppo-role="detail-title"]',
    "#introduce h1.tit",
    "main h1.tit",
    ".official-oppo-detail-title",
)
_PRICE_REGIONS = (
    '[data-oppo-role="price-region"]',
    "#introduce .price-tag",
    ".price-tag",
    '[data-official-price-context="销售价"]',
)
_PRICE_NODES = (
    '[data-oppo-role="price"]',
    ".g-price-original-integer",
    ".official-oppo-price",
)
_CAPACITY_GROUPS = (
    '[data-oppo-role="capacity-group"]',
    ".official-oppo-capacities",
)
_COLOR_GROUPS = (
    '[data-oppo-role="color-group"]',
    ".official-oppo-colors",
)
_RISK_MARKERS = (
    '[data-oppo-role="risk-control"]',
    '[id*="captcha"]',
    '[class*="captcha"]',
    '[class*="geetest"]',
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
      contextText = current.dataset.officialPriceContext || null;
    }
    if (current.classList && current.classList.contains("price-tag")) break;
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return {
    color,
    effectiveLineThrough: lineThrough,
    contextText,
    inViewport: rect.width > 0 && rect.height > 0 &&
      rect.top >= 0 && rect.left >= 0 &&
      rect.bottom <= window.innerHeight && rect.right <= window.innerWidth,
  };
}
"""


class _PriceUnavailable(LayoutRecognitionError):
    """Transient absence of an approved OPPO main-product price."""


class OppoSearchCardSelectionError(LayoutRecognitionError):
    """Visible OPPO search results cannot safely select a detail card."""

    oppo_search_card_failure = True


class OppoOfficialAdapter(LiveOfficialAdapterBase):
    """Live OPPO official-store adapter isolated from every frozen site."""

    approved_host_families = (
        ApprovedHostFamily("www.opposhop.cn", allow_subdomains=False),
    )

    def __init__(self, spec: Any) -> None:
        super().__init__(spec)
        self._prepared: set[tuple[int, str, str]] = set()
        self._prepared_rectangles: dict[
            tuple[int, str, str], tuple[CssRect, ...]
        ] = {}

    def _manual_action(self, page: BrowserPage) -> OfficialManualAction | None:
        browser_page = _playwright_page(page)
        if _visible(browser_page, _RISK_MARKERS):
            return OfficialManualAction.SECURITY_VERIFICATION
        return None

    def _observe_validated(self, task: WebsiteTask, page: BrowserPage) -> AdapterObservation:
        browser_page = _playwright_page(page)
        browser_page.goto(self.spec.entry_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        self.require_approved_url(browser_page.url)
        self.raise_if_manual_action(browser_page)
        self._start_search(browser_page, task)
        self._wait_for_search_results(browser_page, task)

        exact_link = self._preferred_exact_result_link(browser_page, task)
        if exact_link is None:
            return self.build_observation(task, self._no_model_state(task, browser_page))
        card_url = _approved_product_url(exact_link.get_attribute("href"))
        browser_page.goto(card_url, wait_until="domcontentloaded")
        browser_page.wait_for_load_state("domcontentloaded")
        if not _is_detail_url(browser_page.url):
            raise LayoutRecognitionError("OPPO product card did not reach a detail URL")
        self._prepare_detail_view(browser_page)
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
            if self._search_scope(browser_page) is None:
                self._start_search(browser_page, task)
            self._wait_for_search_results(browser_page, task)
            if self._preferred_exact_result_link(browser_page, task) is not None:
                raise LayoutRecognitionError("OPPO recovered no-model state changed")
            return self.build_observation(task, self._no_model_state(task, browser_page))
        if not _is_detail_url(browser_page.url):
            raise LayoutRecognitionError("OPPO recovery URL is not a product detail")
        self._prepare_detail_view(browser_page)
        return self._observe_loaded_detail(task, browser_page)

    def _prepare_detail_view(self, page: Any) -> None:
        if not _is_detail_url(page.url):
            raise LayoutRecognitionError("OPPO detail view requires a product URL")
        try:
            ensure_capture_scale(page, scale=0.8)
        except Exception:
            restore_capture_scale(page)
            raise

    def _start_search(self, page: Any, task: WebsiteTask) -> None:
        opener = _first_visible(page, _SEARCH_OPENERS)
        if opener is None:
            raise LayoutRecognitionError("OPPO search opener is unavailable")
        opener.click()

        search_input = None
        for _ in range(21):
            self.raise_if_manual_action(page)
            scope = self._search_scope(page)
            if scope is not None:
                search_input = _first_visible(scope, _SEARCH_INPUTS)
                if search_input is not None:
                    break
            page.wait_for_timeout(250)
        if search_input is None:
            raise LayoutRecognitionError("OPPO search dialog did not open")
        search_input.fill(task.model_name)
        search_input.press("Enter")

    def _observe_loaded_detail(self, task: WebsiteTask, page: Any) -> AdapterObservation:
        try:
            identity = _detail_identity(page.url)
            self._wait_for_detail_title(page, task.model_name)

            self._wait_for_option_group(page, "capacity", task)
            capacity = self._exact_option(page, "capacity", task)
            if capacity is None or _is_disabled(capacity):
                return self.build_observation(
                    task,
                    self._configuration_no_state(
                        task, page, identity, BusinessOutcome.CAPACITY_UNAVAILABLE
                    ),
                )
            self._select_exact_option(page, "capacity", task)

            self._wait_for_option_group(page, "color", task)
            color = self._exact_option(page, "color", task)
            if color is None or _is_disabled(color):
                return self.build_observation(
                    task,
                    self._configuration_no_state(
                        task, page, _detail_identity(page.url), BusinessOutcome.COLOR_UNAVAILABLE
                    ),
                )
            self._select_exact_option(page, "color", task)
            self._wait_for_selected_option(page, "capacity", task)

            snapshot = self._wait_for_stable_offer(task, page)
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
        finally:
            restore_capture_scale(page)

    def _read_business_state(self, task: WebsiteTask, page: BrowserPage) -> OfficialBusinessState:
        browser_page = _playwright_page(page)
        if not _is_detail_url(browser_page.url):
            self._wait_for_search_results(browser_page, task)
            if self._preferred_exact_result_link(browser_page, task) is not None:
                raise LayoutRecognitionError("OPPO exact model appeared after no-model result")
            return self._no_model_state(task, browser_page)

        identity = _detail_identity(browser_page.url)
        self._require_exact_detail_title(browser_page, task.model_name)
        capacity = self._exact_option(browser_page, "capacity", task)
        if capacity is None or _is_disabled(capacity):
            return self._configuration_no_state(
                task, browser_page, identity, BusinessOutcome.CAPACITY_UNAVAILABLE
            )
        self._require_unique_selected_option(browser_page, "capacity", task)
        color = self._exact_option(browser_page, "color", task)
        if color is None or _is_disabled(color):
            return self._configuration_no_state(
                task, browser_page, identity, BusinessOutcome.COLOR_UNAVAILABLE
            )
        self._require_unique_selected_option(browser_page, "color", task)
        return OfficialBusinessState.price_found(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=_capacity_text(task),
            color=task.color,
            price=self._read_current_price(browser_page),
        )

    def _offer_matches_task(self, task: WebsiteTask, snapshot: OfficialOfferSnapshot) -> bool:
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
                proofs = self._capture_proof_locators(task, browser_page)
                if not _proof_group_fits_current_viewport(browser_page, proofs):
                    if not _scroll_proof_group_into_view(browser_page, proofs):
                        raise LayoutRecognitionError(
                            "OPPO title, price, capacity and color must fit the same viewport"
                        )
                    proofs = self._capture_proof_locators(task, browser_page)
                    if not _proof_group_fits_current_viewport(browser_page, proofs):
                        raise LayoutRecognitionError(
                            "OPPO title, price, capacity and color must fit the same viewport"
                        )
                current_state = self.build_observation(
                    task, self._read_business_state(task, browser_page)
                ).semantic_state
                if current_state != expected:
                    raise LayoutRecognitionError("OPPO capture view changed the offer")
            except Exception:
                restore_capture_scale(browser_page)
                raise
        else:
            try:
                if expected.outcome is BusinessOutcome.NO_MODEL:
                    ensure_capture_scale(browser_page, scale=0.8)
                    proofs = self._no_model_capture_proof_locators(
                        task, browser_page
                    )
                    if not _proof_group_fits_current_viewport(
                        browser_page, proofs
                    ):
                        if not _scroll_proof_group_into_view(
                            browser_page, proofs
                        ):
                            raise LayoutRecognitionError(
                                "OPPO search keyword and complete result region "
                                "must fit the same viewport"
                            )
                        proofs = self._no_model_capture_proof_locators(
                            task, browser_page
                        )
                        if not _proof_group_fits_current_viewport(
                            browser_page, proofs
                        ):
                            raise LayoutRecognitionError(
                                "OPPO search keyword and complete result region "
                                "must fit the same viewport"
                            )
                if expected.outcome is BusinessOutcome.NO_MODEL:
                    current_url = self.require_approved_url(browser_page.url)
                    if current_url != expected.canonical_url:
                        raise LayoutRecognitionError(
                            "OPPO no-model capture URL changed"
                        )
                    if self._preferred_exact_result_link(browser_page, task) is not None:
                        raise LayoutRecognitionError(
                            "OPPO exact model appeared before no-model capture"
                        )
                    keyword, region = proofs
                    self._prepared_rectangles[key] = (
                        _css_rect(keyword, "search_keyword"),
                        _css_rect(region, "result_region"),
                    )
                else:
                    current_business = self._read_business_state(task, browser_page)
                    current_state = self.build_observation(
                        task, current_business
                    ).semantic_state
                    if not self._same_legal_no_business_state(current_state, expected):
                        raise LayoutRecognitionError("OPPO legal-no capture view changed")
                    self._prepared_rectangles[key] = tuple(
                        current_business.capture_view.css_rectangles
                    )
            except Exception:
                if expected.outcome is BusinessOutcome.NO_MODEL:
                    restore_capture_scale(browser_page)
                raise
        self._prepared.add(key)

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> SemanticStateReader:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        if expected.outcome is not BusinessOutcome.NO_MODEL:
            return super().verified_state_reader(task, page, expected)
        browser_page = _playwright_page(page)
        key = (id(browser_page), task.task_id, expected.current_sku)
        if key not in self._prepared_rectangles:
            raise LayoutRecognitionError("OPPO no-model capture view is not prepared")

        def reader() -> VerifiedSemanticState:
            self.raise_if_manual_action(browser_page)
            current_url = self.require_approved_url(browser_page.url)
            if current_url != expected.canonical_url:
                raise LayoutRecognitionError("OPPO no-model capture URL changed")
            self._no_model_capture_proof_locators(task, browser_page)
            if self._preferred_exact_result_link(browser_page, task) is not None:
                raise LayoutRecognitionError(
                    "OPPO exact model appeared before no-model capture"
                )
            return expected

        return reader

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> None:
        browser_page = _playwright_page(page)
        self._prepared.discard((id(browser_page), task.task_id, expected.current_sku))
        self._prepared_rectangles.pop(
            (id(browser_page), task.task_id, expected.current_sku), None
        )
        if expected.outcome in {
            BusinessOutcome.PRICE_FOUND,
            BusinessOutcome.NO_MODEL,
        }:
            restore_capture_scale(browser_page)

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        key = (id(_playwright_page(page)), task.task_id, expected.current_sku)
        prepared = self._prepared_rectangles.get(key)
        if expected.outcome is not BusinessOutcome.PRICE_FOUND and prepared is not None:
            return prepared
        current = self._read_business_state(task, page)
        current_state = self.build_observation(task, current).semantic_state
        if expected.outcome is BusinessOutcome.PRICE_FOUND:
            if current_state != expected:
                raise LayoutRecognitionError("OPPO capture state changed")
            return ()
        if not self._same_legal_no_business_state(current_state, expected):
            raise LayoutRecognitionError("OPPO legal-no capture state changed")
        return current.capture_view.css_rectangles

    def _capture_proof_locators(self, task: WebsiteTask, page: Any) -> tuple[Any, ...]:
        _amount, price = self._current_price(page)
        title = self._detail_title(page, task.model_name)
        capacity = self._require_unique_selected_option(page, "capacity", task)
        color = self._require_unique_selected_option(page, "color", task)
        if title is None:
            raise LayoutRecognitionError("OPPO product title is unavailable")
        return (title, price, capacity, color)

    def _no_model_capture_proof_locators(
        self, task: WebsiteTask, page: Any
    ) -> tuple[Any, Any]:
        keyword = self._require_search_keyword(page, task.model_name)
        scope = self._search_scope(page)
        region = _first_visible(scope, _RESULT_REGIONS) if scope is not None else None
        if region is None:
            raise LayoutRecognitionError(
                "OPPO complete no-model result region is unavailable"
            )
        return keyword, region

    def _search_scope(self, page: Any) -> Any | None:
        return _first_visible(page, _SEARCH_DIALOGS)

    def _start_signature(self, page: Any) -> tuple[tuple[str, str], ...]:
        scope = self._search_scope(page)
        if scope is None:
            return ()
        return tuple(
            (_link_title(link), str(link.get_attribute("href") or ""))
            for link in _visible(scope, _PRODUCT_LINKS)
        )

    def _wait_for_search_results(self, page: Any, task: WebsiteTask) -> None:
        previous: tuple[tuple[str, str], ...] | None = None
        stable_reads = 0
        elapsed = 0
        for _ in range(41):
            self.raise_if_manual_action(page)
            scope = self._search_scope(page)
            keyword = (
                self._search_query_evidence(scope, task.model_name)
                if scope is not None
                else None
            )
            signature = self._start_signature(page)
            if signature == previous:
                stable_reads += 1
            else:
                stable_reads = 0
            previous = signature
            region = (
                _first_visible(scope, _RESULT_REGIONS)
                if scope is not None
                else None
            )
            explicit_empty = self._explicit_empty_result(scope)
            result_complete = self._result_list_complete(scope)
            if stable_reads >= 3 and region is not None:
                if self._preferred_exact_result_link(page, task) is not None:
                    return
                if keyword is not None:
                    if explicit_empty:
                        return
                    if result_complete and signature:
                        return
                    if elapsed >= 40 and signature:
                        return
            if elapsed < 40:
                page.wait_for_timeout(250)
                elapsed += 1
        raise OppoSearchCardSelectionError("OPPO search results did not stabilize")

    def _preferred_exact_result_link(self, page: Any, task: WebsiteTask) -> Any | None:
        scope = self._search_scope(page)
        if scope is None or self._explicit_empty_result(scope):
            return None
        exact_model_seen = False
        for link in _visible(scope, _PRODUCT_LINKS):
            if not _title_matches_model(task.model_name, _link_title(link)):
                continue
            exact_model_seen = True
            try:
                approved = _approved_product_url(link.get_attribute("href"))
            except ValueError:
                continue
            if not _detail_identity(approved).product_key.isdigit():
                raise LayoutRecognitionError("OPPO product identity is invalid")
            return link
        if exact_model_seen:
            raise OppoSearchCardSelectionError(
                "OPPO exact-model cards have no approved product URL"
            )
        return None

    def _wait_for_detail_title(self, page: Any, model_name: str) -> None:
        for _ in range(41):
            self.raise_if_manual_action(page)
            if self._detail_title(page, model_name) is not None:
                return
            page.wait_for_timeout(250)
        raise LayoutRecognitionError("OPPO detail title does not match the task")

    def _detail_title(self, page: Any, model_name: str) -> Any | None:
        return next(
            (
                title
                for title in _visible(page, _DETAIL_TITLES)
                if _title_matches_model(model_name, title.inner_text())
            ),
            None,
        )

    def _require_exact_detail_title(self, page: Any, model_name: str) -> Any:
        title = self._detail_title(page, model_name)
        if title is None:
            raise LayoutRecognitionError("OPPO detail title does not match the task")
        return title

    def _wait_for_option_group(self, page: Any, kind: str, task: WebsiteTask) -> None:
        previous: tuple[str, ...] | None = None
        stable_reads = 0
        elapsed = 0
        for _ in range(21):
            self.raise_if_manual_action(page)
            options = self._option_locators(page, kind)
            signature = tuple(option.inner_text().strip() for option in options)
            if signature and signature == previous:
                stable_reads += 1
            else:
                stable_reads = 0
            previous = signature
            if stable_reads >= 3:
                if self._exact_option(page, kind, task) is not None or elapsed >= 20:
                    return
            if elapsed < 20:
                page.wait_for_timeout(250)
                elapsed += 1
        raise LayoutRecognitionError(f"OPPO {kind} options did not stabilize")

    def _option_group(self, page: Any, kind: str) -> Any | None:
        selectors = _CAPACITY_GROUPS if kind == "capacity" else _COLOR_GROUPS
        explicit = _first_visible(page, selectors)
        if explicit is not None:
            return explicit
        anchor = "版本" if kind == "capacity" else "颜色"
        for group in _visible(page, (".v-item-group",)):
            text = group.inner_text().strip()
            first_line = text.splitlines()[0].strip() if text else ""
            if first_line == anchor:
                return group
        return None

    def _option_locators(self, page: Any, kind: str) -> tuple[Any, ...]:
        group = self._option_group(page, kind)
        if group is None:
            return ()
        return _visible(group, ("button.btn", "button.official-oppo-option", "button"))

    def _exact_option(self, page: Any, kind: str, task: WebsiteTask) -> Any | None:
        exact = [
            option
            for option in self._option_locators(page, kind)
            if (
                capacity_matches(option.inner_text(), task.ram, task.storage)
                if kind == "capacity"
                else color_matches(task.color, option.inner_text())
            )
        ]
        if len(exact) > 1:
            raise LayoutRecognitionError(f"OPPO exact {kind} option is ambiguous")
        return exact[0] if exact else None

    def _wait_for_selected_option(self, page: Any, kind: str, task: WebsiteTask) -> None:
        for _ in range(20):
            try:
                self._require_unique_selected_option(page, kind, task)
                return
            except LayoutRecognitionError:
                page.wait_for_timeout(250)
        raise LayoutRecognitionError(f"OPPO selected {kind} option did not stabilize")

    def _select_exact_option(self, page: Any, kind: str, task: WebsiteTask) -> None:
        try:
            self._require_unique_selected_option(page, kind, task)
            return
        except LayoutRecognitionError:
            pass
        target = self._exact_option(page, kind, task)
        if target is None or _is_disabled(target):
            raise LayoutRecognitionError(f"OPPO target {kind} option is unavailable")
        target.click()
        self._wait_for_selected_option(page, kind, task)

    def _require_unique_selected_option(self, page: Any, kind: str, task: WebsiteTask) -> Any:
        selected = tuple(
            option for option in self._option_locators(page, kind) if _is_selected(option)
        )
        if len(selected) != 1:
            raise LayoutRecognitionError(f"OPPO selected {kind} option is not unique")
        target = selected[0]
        matches = (
            capacity_matches(target.inner_text(), task.ram, task.storage)
            if kind == "capacity"
            else color_matches(task.color, target.inner_text())
        )
        if not matches or _is_disabled(target):
            raise LayoutRecognitionError(f"OPPO selected {kind} option is unavailable")
        return target

    def _wait_for_stable_offer(self, task: WebsiteTask, page: Any) -> OfficialOfferSnapshot:
        first: OfficialOfferSnapshot | None = None
        for tick in range(41):
            try:
                first = self._instant_offer_snapshot(task, page)
            except _PriceUnavailable:
                pass
            if first is not None:
                break
            if tick < 40:
                page.wait_for_timeout(250)
        if first is None:
            raise CaptureQualityError(
                "CAPTURE_UNSTABLE", "OPPO price did not appear within ten seconds"
            )
        previous: OfficialOfferSnapshot | None = first
        stable_reads = 1
        for _ in range(40):
            page.wait_for_timeout(250)
            try:
                current = self._instant_offer_snapshot(task, page)
            except _PriceUnavailable:
                previous = None
                stable_reads = 0
                continue
            stable_reads = stable_reads + 1 if current == previous else 1
            previous = current
            if stable_reads >= 13:
                return current
        raise CaptureQualityError(
            "CAPTURE_UNSTABLE", "OPPO selected offer did not remain stable"
        )

    def _instant_offer_snapshot(self, task: WebsiteTask, page: Any) -> OfficialOfferSnapshot:
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
            price=self._read_current_price(page),
        )
        if not self._offer_matches_task(task, snapshot):
            raise CaptureQualityError("CAPTURE_UNSTABLE", "OPPO offer changed")
        return snapshot

    def _read_current_price(self, page: Any) -> Decimal:
        return self._current_price(page)[0]

    def _current_price(self, page: Any) -> tuple[Decimal, Any]:
        region = _first_visible(page, _PRICE_REGIONS)
        if region is None:
            raise _PriceUnavailable("OPPO main price region is unavailable")
        values: list[tuple[Decimal, Any]] = []
        for candidate in _visible(region, _PRICE_NODES):
            text = candidate.inner_text().strip()
            style = candidate.evaluate(_PRICE_STYLE_SCRIPT)
            if not isinstance(style, dict):
                continue
            if style.get("effectiveLineThrough") is True:
                continue
            if style.get("inViewport") is False:
                continue
            context = str(style.get("contextText") or "").upper()
            if any(word.upper() in context for word in _EXCLUDED_PRICE_WORDS):
                continue
            amount = _decimal_amount(text)
            if amount is not None:
                values.append((amount, candidate))
        if not values:
            raise _PriceUnavailable("OPPO current product price is unavailable")
        selected = min(amount for amount, _candidate in values)
        locator = next(candidate for amount, candidate in values if amount == selected)
        return selected, locator

    def _no_model_state(self, task: WebsiteTask, page: Any) -> OfficialBusinessState:
        keyword = self._require_search_keyword(page, task.model_name)
        scope = self._search_scope(page)
        if scope is None:
            raise LayoutRecognitionError("OPPO no-model search dialog is unavailable")
        region = _first_visible(scope, _RESULT_REGIONS)
        links = _visible(scope, _PRODUCT_LINKS)
        explicit_empty = self._explicit_empty_result(scope)
        result_complete = self._result_list_complete(scope)
        if region is None or (not links and not explicit_empty and not result_complete):
            raise LayoutRecognitionError("OPPO no-model evidence is incomplete")
        if self._preferred_exact_result_link(page, task) is not None:
            raise LayoutRecognitionError("OPPO exact model exists on no-model page")
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

    def _require_search_keyword(self, page: Any, model_name: str) -> Any:
        scope = self._search_scope(page)
        keyword = (
            self._search_query_evidence(scope, model_name)
            if scope is not None
            else None
        )
        if keyword is not None:
            return keyword
        raise LayoutRecognitionError("OPPO search keyword does not match the task")

    def _search_query_evidence(self, scope: Any, model_name: str) -> Any | None:
        keyword = self._search_keyword(scope, model_name)
        if keyword is not None:
            return keyword
        wanted = normalize_product_text(model_name)
        return next(
            (
                candidate
                for candidate in _visible(scope, _SEARCH_QUERY_EVIDENCE)
                if normalize_product_text(candidate.inner_text()) == wanted
            ),
            None,
        )

    def _search_keyword(self, scope: Any, model_name: str) -> Any | None:
        return next(
            (
                candidate
                for candidate in _visible(scope, _SEARCH_INPUTS)
                if _input_value(candidate) == model_name
            ),
            None,
        )

    def _explicit_empty_result(self, scope: Any | None) -> bool:
        if scope is None:
            return False
        return any(
            "未找到相关商品" in marker.inner_text().strip()
            for marker in _visible(scope, _EMPTY_RESULTS)
        )

    def _result_list_complete(self, scope: Any | None) -> bool:
        if scope is None:
            return False
        return any(
            "没有更多了" in marker.inner_text().strip()
            for marker in _visible(scope, _EMPTY_RESULTS)
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
            raise LayoutRecognitionError(f"OPPO complete {kind} options are unavailable")
        target = self._exact_option(page, kind, task)
        if target is not None and not _is_disabled(target):
            raise LayoutRecognitionError(f"OPPO target {kind} is available")
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


def _approved_product_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("OPPO product URL is missing")
    normalized = value.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    elif normalized.startswith("/"):
        normalized = f"https://www.opposhop.cn{normalized}"
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.opposhop.cn"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or _DETAIL_PATH.fullmatch(parsed.path) is None
    ):
        raise ValueError("OPPO product URL is not an approved numeric product path")
    return normalized


def _is_detail_url(value: str) -> bool:
    try:
        _approved_product_url(value)
    except ValueError:
        return False
    return True


def _detail_identity(url: str) -> OfficialDetailIdentity:
    normalized = _approved_product_url(url)
    match = _DETAIL_PATH.fullmatch(urlsplit(normalized).path)
    if match is None:
        raise ValueError("OPPO product URL is not an approved numeric product path")
    return OfficialDetailIdentity(normalized, match.group("product_id"))


def _capacity_text(task: WebsiteTask) -> str:
    return f"{task.ram} + {task.storage}"


def _title_matches_model(model_name: str, title: str) -> bool:
    """Accept an exact base model followed by OPPO SKU display fields.

    The shared matcher intentionally rejects arbitrary colour suffixes.  OPPO
    search cards and detail headings append colour/capacity to the exact base
    model, so this adapter first keeps the shared strict result and then allows
    one boundary-safe leading model followed by SKU display text.  Attached
    variants such as ``A6k`` therefore never match ``A6``.
    """

    if model_matches(model_name, title):
        return True
    actual = normalize_product_text(title)
    wanted = normalize_product_text(model_name)
    wanted_networks = set(_NETWORK_MARKER.findall(wanted))
    actual_networks = set(_NETWORK_MARKER.findall(actual))
    if wanted_networks and actual_networks and wanted_networks != actual_networks:
        return False
    without_network = " ".join(_NETWORK_MARKER.sub(" ", wanted).split())
    return any(
        _title_has_model_prefix(candidate, actual)
        for candidate in dict.fromkeys((wanted, without_network))
        if candidate
    )


def _title_has_model_prefix(wanted: str, actual: str) -> bool:
    if not actual.startswith(wanted):
        return False
    if len(actual) == len(wanted):
        return True
    if not actual[len(wanted)].isspace():
        return False
    remainder = actual[len(wanted) :].lstrip()
    first_word = re.match(r"[A-Z]+", remainder)
    return first_word is None or first_word.group() not in _MODEL_VARIANT_PREFIXES


def _is_disabled(locator: Any) -> bool:
    if locator.get_attribute("disabled") is not None:
        return True
    if str(locator.get_attribute("aria-disabled") or "").lower() == "true":
        return True
    classes = set(str(locator.get_attribute("class") or "").lower().split())
    return bool(_DISABLED_CLASSES.intersection(classes))


def _is_selected(locator: Any) -> bool:
    values = {
        str(locator.get_attribute(name) or "").lower()
        for name in ("aria-selected", "aria-checked", "data-selected", "data-state")
    }
    if values.intersection(("false", "unselected", "inactive", "unchecked")):
        return False
    if values.intersection(("true", "selected", "active", "checked")):
        return True
    classes = set(str(locator.get_attribute("class") or "").lower().split())
    if classes.intersection(("active-btn", "selected", "active", "current", "checked")):
        return True
    try:
        return locator.evaluate(
            """
            (element) => {
              const __oppoExplicitSelection = true;
              return Array.from(element.classList || []).some(value =>
                ["active-btn", "selected", "active", "current", "checked"]
                  .includes(value.toLowerCase()));
            }
            """
        ) is True
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
        raise LayoutRecognitionError(f"OPPO {role} evidence has no geometry")
    try:
        return CssRect(
            float(box["x"]),
            float(box["y"]),
            float(box["width"]),
            float(box["height"]),
            role,
        )
    except (KeyError, TypeError, ValueError):
        raise LayoutRecognitionError(f"OPPO {role} evidence geometry is invalid") from None


def _decimal_amount(text: str) -> Decimal | None:
    match = _MONEY.search(text)
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _proof_group_fits_current_viewport(page: Any, locators: tuple[Any, ...]) -> bool:
    try:
        handles = [locator.element_handle() for locator in locators]
        if any(handle is None for handle in handles):
            return False
        return page.evaluate(
            """
            (elements) => {
              const __oppoProofsFitCurrentViewport = true;
              const __oppoProofsUnoccluded = true;
              return __oppoProofsFitCurrentViewport && elements.every(element => {
                const box = element.getBoundingClientRect();
                const fits = box.width > 0 && box.height > 0 && box.top >= 0 &&
                  box.left >= 0 && box.bottom <= window.innerHeight &&
                  box.right <= window.innerWidth;
                if (!fits) return false;
                const centerX = box.left + box.width / 2;
                const centerY = box.top + box.height / 2;
                const topHit = document.elementsFromPoint(centerX, centerY).find(hit =>
                  window.getComputedStyle(hit).pointerEvents !== "none");
                return __oppoProofsUnoccluded && Boolean(topHit) &&
                  (topHit === element || element.contains(topHit) || topHit.contains(element));
              });
            }
            """,
            handles,
        ) is True
    except (AttributeError, RuntimeError):
        return False


def _scroll_proof_group_into_view(page: Any, locators: tuple[Any, ...]) -> bool:
    try:
        handles = [locator.element_handle() for locator in locators]
        if any(handle is None for handle in handles):
            return False
        state = page.evaluate(
            """
            (elements) => {
              const __oppoProofPositionState = true;
              const boxes = elements.map(element => element.getBoundingClientRect());
              const __oppoAllProofOcclusions = true;
              const occlusions = elements.flatMap((element, index) => {
                const box = boxes[index];
                const centerX = box.left + box.width / 2;
                const centerY = box.top + box.height / 2;
                const topHit = document.elementsFromPoint(centerX, centerY).find(hit =>
                  window.getComputedStyle(hit).pointerEvents !== "none");
                const unoccluded = Boolean(topHit) &&
                  (topHit === element || element.contains(topHit) || topHit.contains(element));
                if (unoccluded) return [];
                const blockerBox = topHit ? topHit.getBoundingClientRect() : null;
                return blockerBox ? [{proofBottom: box.bottom, blockerTop: blockerBox.top}] : [];
              });
              return {
                unionTop: Math.min(...boxes.map(box => box.top)),
                unionBottom: Math.max(...boxes.map(box => box.bottom)),
                occlusions,
                viewportHeight: window.innerHeight,
                scrollY: window.scrollY,
                marker: __oppoProofPositionState,
                occlusionMarker: __oppoAllProofOcclusions,
              };
            }
            """,
            handles,
        )
        if not isinstance(state, dict):
            return False
        top = float(state["unionTop"])
        bottom = float(state["unionBottom"])
        viewport_height = float(state["viewportHeight"])
        if bottom - top > viewport_height - 48:
            return False
        delta = 0.0
        occlusions = state.get("occlusions")
        if isinstance(occlusions, list) and occlusions:
            overlaps: list[float] = []
            for occlusion in occlusions:
                if not isinstance(occlusion, dict):
                    return False
                overlaps.append(
                    float(occlusion["proofBottom"])
                    - float(occlusion["blockerTop"])
                    + 24.0
                )
            delta = max(overlaps)
        elif bottom > viewport_height - 24:
            delta = bottom - (viewport_height - 24)
        elif top < 24:
            delta = top - 24
        if abs(delta) < 1 or abs(delta) > 160:
            return False
        if delta > 0 and top - delta < 24:
            return False
        return page.evaluate(
            """
            (delta) => {
              const __oppoApplyBoundedProofPosition = true;
              window.scrollTo({
                top: Math.max(0, window.scrollY + delta),
                behavior: "instant",
              });
              return __oppoApplyBoundedProofPosition;
            }
            """,
            delta,
        ) is True
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return False

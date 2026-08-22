from __future__ import annotations

import math
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError
from quote_app.evidence.semantic_state import (
    SemanticStateReader,
    VerifiedSemanticState,
)
from quote_app.sites.detail_capture_view import ensure_capture_scale, restore_capture_scale
from quote_app.sites.matching import color_matches, model_matches, normalize_product_text
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
from quote_app.tasks.retry import (
    LayoutRecognitionError,
    NonRetryableTechnicalError,
    SecurityVerificationRequired,
)

_ENTRY = "https://www.apple.com.cn/shop/buy-iphone"
_DETAIL_PATH = re.compile(
    r"^/shop/buy-iphone/iphone-[a-z0-9-]+(?:/[a-z0-9-]+){0,2}/?$",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"(?:RMB|人民币|[¥￥])\s*(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_SEARCH_INPUTS = (
    "input[data-apple-role=search-input]",
    "input[type=search]",
    "input[placeholder*=搜索]",
)
_RESULT_REGION = ("[data-apple-role=result-region]", "main")
_RESULT_LINKS = (
    "a[data-apple-role=product-link]",
    "a[href*='/shop/buy-iphone/iphone-']",
)
_RESULT_TITLE = ("[data-apple-role=product-title]", "h2", "h3")
_DETAIL_TITLE = (
    "[data-apple-role=detail-title]",
    # On the live Apple China configurator a visible summary-productName
    # placeholder can precede the populated purchase-flow heading.
    # Try the semantic heading before generic data-autom fallbacks so an empty
    # placeholder cannot stop detail-page recognition.
    "h1",
    "[data-autom*=product-name]",
    "[data-autom*=productName]",
    "[data-autom*=product-title]",
)
# The Apple China configurator pins a compact purchase header after the main
# product heading has scrolled away.  It is not used to establish the business
# state; it is only valid as title evidence for the final screenshot while it
# is actually inside the viewport.
_CAPTURE_STICKY_TITLE = (
    "a[data-autom=stickynavHeader]",
    "[data-autom=stickynavHeader]",
)
_PRICE_CANDIDATES = (
    "[data-apple-role=current-device-price]",
    "[data-autom=full-price]",
    "[data-autom=current-price]",
    "[data-autom*=price]",
)
_GROUP_SELECTORS = {
    "capacity": (
        # Apple uses label-bearing native radio controls in fieldsets on the
        # public China purchase flow.  Matching the fieldset first prevents a
        # single dimensionCapacity input from being mistaken for its group.
        "fieldset",
        "[data-apple-role=storage-group]",
        "[data-autom*=dimensionCapacity]",
        "[data-autom*=capacity]",
    ),
    "color": (
        "fieldset",
        "[data-apple-role=color-group]",
        "[data-autom*=dimensionColor]",
        "[data-autom*=color]",
    ),
}
_OPTION_SELECTORS = ("button", "[role=radio]", "input[type=radio]")
_GROUP_LABELS = {
    "capacity": ("存储", "容量", "存储容量"),
    "color": ("颜色", "外观", "外观颜色"),
}
_PRICE_EXCLUSIONS = (
    "每月",
    "分期",
    "换购",
    "折抵",
    "APPLECARE",
    "服务",
    "配件",
    "MONTH",
    "INSTALLMENT",
    "TRADE-IN",
    "SERVICE",
    "ACCESSORY",
)
# Marketing source data and the Apple China configurator do not always use the
# same customer-facing colour wording.  Keep this deliberately small and
# one-way: the aliases below are confirmed source-to-store names, not fuzzy
# matching that could choose a different colour.
_APPLE_COLOR_ALIASES: dict[str, frozenset[str]] = {
    "青雾蓝": frozenset({"雾蓝"}),
}
_CARD_AVAILABILITY_COPY = re.compile(
    r"(?:暂时缺货|暂时无货|暂无货|已售罄|缺货|无货|售罄)",
    re.IGNORECASE,
)
_SECURITY_MARKERS = (
    '[id*="captcha"]',
    '[class*="captcha"]',
    '[class*="verify"]',
    'iframe[src*="captcha"]',
    'iframe[src*="verify"]',
)
_FIT_VIEWPORT = "() => ({width: window.innerWidth, height: window.innerHeight})"
_PAINT_STATE = """
(element) => {
  // Stable marker used by the contract harness.  The returned facts are all
  // computed from this exact node; no selector is re-run after React updates.
  const __quotationApplePaintState = true;
  void __quotationApplePaintState;
  if (!element || !document.documentElement.contains(element)) {
    return { rendered: false, insideViewport: false, exposed: false };
  }
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  const rendered = style.display !== 'none'
    && style.visibility !== 'hidden'
    && Number.parseFloat(style.opacity || '1') > 0
    && rect.width > 0
    && rect.height > 0;
  const insideViewport = rendered
    && rect.left >= 0
    && rect.top >= 0
    && rect.right <= window.innerWidth
    && rect.bottom <= window.innerHeight;
  if (!insideViewport) {
    return { rendered, insideViewport, exposed: false };
  }
  const samples = [
    [0.5, 0.5],
    [0.25, 0.5],
    [0.75, 0.5],
    [0.5, 0.25],
    [0.5, 0.75],
  ];
  const exposed = samples.some(([xRatio, yRatio]) => {
    const x = rect.left + rect.width * xRatio;
    const y = rect.top + rect.height * yRatio;
    const hit = document.elementFromPoint(x, y);
    return hit === element || (hit !== null && element.contains(hit));
  });
  return { rendered, insideViewport, exposed };
}
"""
_GROUP_SCROLL = """
({ delta }) => {
  window.scrollBy({ top: delta, left: 0, behavior: 'instant' });
  return window.scrollY;
}
"""
_CAPTURE_EDGE_PX = 24.0
_MIN_CAPTURE_SCROLL_STEP_PX = 16.0
_MAX_CAPTURE_SCROLL_STEP_PX = 80.0
_STICKY_ACTIVATION_SCROLL_PX = 32.0
_SELECTED_SKU_ROUTE_WAIT_TICKS = 8
_SELECTED_SKU_ROUTE_WAIT_MS = 250
_CAPTURE_STATE_WAIT_TICKS = 8
_CAPTURE_STATE_WAIT_MS = 250
_CAPTURE_GEOMETRY_CORRECTIONS = 8
_CAPTURE_GEOMETRY_WAIT_MS = 300


class AppleOfficialAdapter(LiveOfficialAdapterBase):
    """Apple China iPhone official-store adapter.

    Apple exposes storage capacity rather than RAM in its iPhone configurator.
    The input RAM is intentionally ignored after exact storage, colour and
    full-device price have been verified.
    """

    approved_host_families = (
        ApprovedHostFamily("www.apple.com.cn", allow_subdomains=False),
    )

    def __init__(self, spec: Any) -> None:
        super().__init__(spec)
        self._prepared: set[tuple[int, str, str]] = set()

    def _manual_action(self, page: BrowserPage) -> OfficialManualAction | None:
        browser = _page(page)
        if any(_visible(browser, (selector,)) for selector in _SECURITY_MARKERS):
            raise SecurityVerificationRequired(
                "official:苹果",
                "Apple 官网触发安全验证，请完成后继续当前任务",
            )
        return None

    def _offer_matches_task(
        self,
        task: WebsiteTask,
        snapshot: OfficialOfferSnapshot,
    ) -> bool:
        return (
            snapshot.brand == task.brand
            and model_matches(task.model_name, snapshot.model_name)
            and _normalize_storage(snapshot.capacity)
            == _normalize_storage(task.storage)
            and _apple_color_matches(task.color, snapshot.color)
            and _is_apple_purchase_url(snapshot.identity.canonical_url)
            and bool(snapshot.identity.product_key)
        )

    def _observe_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        browser = _page(page)
        browser.goto(_ENTRY, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser)
        self.require_approved_url(browser.url)

        # An exact visible product card is sufficient.  Do not require the
        # optional site-search control to retain the typed keyword: that was a
        # recurring false blocker in earlier official-store implementations.
        link = self._wait_for_exact_card(browser, task)
        if link is None:
            search = _first_visible(browser, _SEARCH_INPUTS)
            if search is not None:
                search.fill(task.model_name)
                search.press("Enter")
                link = self._wait_for_exact_card(browser, task)
        if link is None:
            return self.build_observation(task, self._no_model_state(task, browser))

        target_url = _approved_purchase_url(link.get_attribute("href"))
        link.click()
        browser.wait_for_load_state("domcontentloaded")
        # Keep navigation in the current controlled page if a dynamic card
        # did not complete a normal same-page transition.
        if not _is_apple_purchase_url(browser.url):
            browser.goto(target_url, wait_until="domcontentloaded")
            browser.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser)
        if not _same_apple_purchase_model(browser.url, target_url):
            raise LayoutRecognitionError("Apple final detail identity changed")
        return self._observe_detail(task, browser)

    def _resume_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        browser = _page(page)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            if not _is_apple_search_or_entry_url(checkpoint.url):
                raise NonRetryableTechnicalError(
                    "RECOVERY_INVALID",
                    "Apple no-model checkpoint is not a search page",
                )
        elif not _is_apple_purchase_url(checkpoint.url):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "Apple detail checkpoint is not a purchase URL",
            )
        browser.goto(checkpoint.url, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            if self._wait_for_exact_card(browser, task) is not None:
                raise LayoutRecognitionError("Apple recovered no-model state changed")
            return self.build_observation(task, self._no_model_state(task, browser))
        return self._observe_detail(task, browser)

    def _observe_detail(self, task: WebsiteTask, page: Any) -> AdapterObservation:
        identity = _detail_identity(page.url)
        self._require_detail_title(page, task.model_name)
        # Apple China enables the storage cards only after colour selection.
        # Do not read a still-disabled capacity card as a final "无" before
        # executing that required first configuration step.
        color = self._wait_for_target_option(page, "color", task)
        if color is None or _disabled(color):
            return self.build_observation(
                task,
                self._configuration_no(
                    task,
                    page,
                    identity,
                    BusinessOutcome.COLOR_UNAVAILABLE,
                ),
            )
        self._select(page, "color", task)
        capacity = self._wait_for_target_option(page, "capacity", task)
        if capacity is None or _disabled(capacity):
            return self.build_observation(
                task,
                self._configuration_no(
                    task,
                    page,
                    identity,
                    BusinessOutcome.CAPACITY_UNAVAILABLE,
                ),
            )
        self._select(page, "capacity", task)
        identity = self._wait_for_selected_sku_route(page, identity)
        snapshot = self._stable_offer(task, page)
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
        browser = _page(page)
        self.raise_if_manual_action(browser)
        if not _is_apple_purchase_url(browser.url):
            return self._no_model_state(task, browser)
        identity = _detail_identity(browser.url)
        self._require_detail_title(browser, task.model_name)
        color = self._exact_option(browser, "color", task)
        if color is None or _disabled(color):
            return self._configuration_no(
                task,
                browser,
                identity,
                BusinessOutcome.COLOR_UNAVAILABLE,
            )
        self._require_selected(browser, "color", task)
        capacity = self._exact_option(browser, "capacity", task)
        if capacity is None or _disabled(capacity):
            return self._configuration_no(
                task,
                browser,
                identity,
                BusinessOutcome.CAPACITY_UNAVAILABLE,
            )
        self._require_selected(browser, "capacity", task)
        price, _locator = self._current_price(browser)
        return OfficialBusinessState.price_found(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=task.storage,
            color=task.color,
            price=price,
        )

    def _wait_for_exact_card(self, page: Any, task: WebsiteTask) -> Any | None:
        for tick in range(41):
            self.raise_if_manual_action(page)
            link = self._first_exact_card(page, task.model_name)
            if link is not None:
                return link
            if tick < 40:
                page.wait_for_timeout(250)
        return None

    def _first_exact_card(self, page: Any, model_name: str) -> Any | None:
        for link in _visible(page, _RESULT_LINKS):
            try:
                _approved_purchase_url(link.get_attribute("href"))
            except ValueError:
                continue
            title = _first_visible(link, _RESULT_TITLE)
            candidate = title.inner_text() if title is not None else link.inner_text()
            if _apple_card_model_matches(model_name, candidate):
                return link
        return None

    def _require_detail_title(self, page: Any, model_name: str) -> Any:
        for title in _visible(page, _DETAIL_TITLE):
            if _apple_model_matches(model_name, title.inner_text()):
                return title
        raise LayoutRecognitionError("Apple detail title does not match the task")

    def _group(self, page: Any, kind: str) -> Any | None:
        for selector in _GROUP_SELECTORS[kind]:
            for group in _visible(page, (selector,)):
                text = normalize_product_text(group.inner_text())
                if any(label in text for label in _GROUP_LABELS[kind]):
                    return group
        return None

    def _options(self, page: Any, kind: str) -> tuple[Any, ...]:
        group = self._group(page, kind)
        if group is None:
            return ()
        for selector in _OPTION_SELECTORS:
            if selector == "input[type=radio]":
                # Apple's real purchase flow visually hides the authoritative
                # native radio input and renders the selectable configuration
                # as its associated label/card.  Keep the input for selected
                # state and click handling, but use the visible label as the
                # visibility boundary.
                try:
                    locator = group.locator(selector)
                    candidates = tuple(
                        locator.nth(index)
                        for index in range(locator.count())
                        if (
                            locator.nth(index).is_visible()
                            or _option_evidence(page, locator.nth(index)).is_visible()
                        )
                    )
                except (AttributeError, RuntimeError):
                    candidates = ()
                if candidates:
                    return candidates
                continue
            candidates = _visible(group, (selector,))
            if candidates:
                return candidates
        return ()

    def _exact_option(self, page: Any, kind: str, task: WebsiteTask) -> Any | None:
        target = task.storage if kind == "capacity" else task.color
        matches = tuple(
            option
            for option in self._options(page, kind)
            if (
                _storage_option_matches(target, _option_text(option))
                if kind == "capacity"
                else _apple_color_matches(target, _option_text(option))
            )
        )
        if len(matches) > 1:
            raise LayoutRecognitionError(f"Apple {kind} option is ambiguous")
        return matches[0] if matches else None

    def _wait_for_target_option(
        self,
        page: Any,
        kind: str,
        task: WebsiteTask,
    ) -> Any | None:
        last_target: Any | None = None
        for tick in range(21):
            self.raise_if_manual_action(page)
            target = self._exact_option(page, kind, task)
            if target is not None:
                last_target = target
            # Apple temporarily renders the selected native controls as
            # disabled while its purchase flow hydrates.  A disabled card is
            # not legal-no evidence until the bounded configuration window
            # has elapsed; otherwise a still-blank/transitioning page gets
            # written as "容量不可用" and the browser appears to exit early.
            if target is not None and not _disabled(target):
                return target
            if tick < 20:
                page.wait_for_timeout(250)
        return last_target

    def _require_selected(self, page: Any, kind: str, task: WebsiteTask) -> Any:
        selected = tuple(
            option for option in self._options(page, kind) if _selected(option)
        )
        if len(selected) != 1:
            raise LayoutRecognitionError(
                f"Apple selected {kind} option is not unique"
            )
        expected = self._exact_option(page, kind, task)
        if (
            expected is None
            or _option_text(selected[0]).strip() != _option_text(expected).strip()
        ):
            raise LayoutRecognitionError(f"Apple selected {kind} option changed")
        if _disabled(selected[0]):
            raise LayoutRecognitionError(
                f"Apple selected {kind} option is unavailable"
            )
        return selected[0]

    def _select(self, page: Any, kind: str, task: WebsiteTask) -> None:
        try:
            self._require_selected(page, kind, task)
            return
        except LayoutRecognitionError:
            pass
        target = self._exact_option(page, kind, task)
        if target is None or _disabled(target):
            raise LayoutRecognitionError(f"Apple target {kind} option is unavailable")
        # Apple's native radio is commonly visually hidden.  Clicking that
        # hidden input times out in the live page even though its associated
        # label/card is the user-visible, actionable control.  Keep the radio
        # as the authoritative selected-state source, but perform the action
        # on its visible card.
        click_target = _option_evidence(page, target)
        click_target.click()
        for tick in range(21):
            self.raise_if_manual_action(page)
            try:
                self._require_selected(page, kind, task)
                return
            except LayoutRecognitionError:
                if tick < 20:
                    page.wait_for_timeout(250)
        raise LayoutRecognitionError(
            f"Apple selected {kind} option did not stabilize"
        )

    def _stable_offer(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> OfficialOfferSnapshot:
        identity = _detail_identity(page.url)
        previous: OfficialOfferSnapshot | None = None
        stable_intervals = 0
        for tick in range(3):
            self.raise_if_manual_action(page)
            current = self._instant_offer(task, page, identity)
            stable_intervals = stable_intervals + 1 if current == previous else 0
            previous = current
            if stable_intervals >= 1:
                return current
            if tick < 2:
                page.wait_for_timeout(250)
        raise CaptureQualityError(
            "CAPTURE_UNSTABLE",
            "Apple selected price did not stabilize",
        )

    def _wait_for_selected_sku_route(
        self,
        page: Any,
        initial_identity: OfficialDetailIdentity,
    ) -> OfficialDetailIdentity:
        """Wait briefly for Apple's post-selection SKU route to settle.

        Apple can retain the base ``iphone-17`` route while configuration
        cards are selected, then replace it with the selected-SKU route a
        moment later.  The latter is still the same product, not a detail
        page drift.  Saving the stale base route makes formal capture reject
        the otherwise valid page before the screenshot positioning runs.
        """

        if _apple_is_selected_sku_route(initial_identity.canonical_url):
            return initial_identity

        latest = initial_identity
        for tick in range(_SELECTED_SKU_ROUTE_WAIT_TICKS + 1):
            self.raise_if_manual_action(page)
            current = _detail_identity(page.url)
            if not _same_apple_purchase_model(
                current.canonical_url,
                initial_identity.canonical_url,
            ):
                raise LayoutRecognitionError(
                    "Apple selected SKU route changed product model"
                )
            latest = current
            if _apple_is_selected_sku_route(current.canonical_url):
                return current
            if tick < _SELECTED_SKU_ROUTE_WAIT_TICKS:
                page.wait_for_timeout(_SELECTED_SKU_ROUTE_WAIT_MS)
        return latest

    def _instant_offer(
        self,
        task: WebsiteTask,
        page: Any,
        expected_identity: OfficialDetailIdentity,
    ) -> OfficialOfferSnapshot:
        identity = _detail_identity(page.url)
        if not _same_apple_purchase_model(
            identity.canonical_url,
            expected_identity.canonical_url,
        ):
            raise LayoutRecognitionError("Apple price wait detail identity changed")
        self._require_detail_title(page, task.model_name)
        self._require_selected(page, "capacity", task)
        self._require_selected(page, "color", task)
        price, _locator = self._current_price(page)
        return OfficialOfferSnapshot(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=task.storage,
            color=task.color,
            price=price,
        )

    def _current_price(self, page: Any) -> tuple[Decimal, Any]:
        # Apple China places the full one-time device price inside the selected
        # storage card.  That card can also contain a monthly installment line;
        # it is still the authoritative price source for the selected SKU.
        selected_capacity = tuple(
            option
            for option in self._options(page, "capacity")
            if _selected(option) and not _disabled(option)
        )
        if len(selected_capacity) == 1:
            capacity_values = _money_values(_option_text(selected_capacity[0]))
            if capacity_values:
                return max(capacity_values), selected_capacity[0]
        if len(selected_capacity) > 1:
            raise LayoutRecognitionError("Apple selected capacity is ambiguous")

        valid: list[tuple[Decimal, Any]] = []
        for candidate in _visible(page, _PRICE_CANDIDATES):
            text = candidate.inner_text()
            if _price_text_rejected(text):
                continue
            style = candidate.evaluate(_PRICE_STYLE)
            if isinstance(style, dict) and style.get("effectiveLineThrough") is True:
                continue
            values = _money_values(text)
            if len(values) == 1:
                valid.append((values[0], candidate))
            elif len(values) > 1:
                raise LayoutRecognitionError(
                    "Apple current price candidate is ambiguous"
                )
        if not valid:
            raise LayoutRecognitionError(
                "Apple current full-device price is unavailable"
            )
        return max(valid, key=lambda item: item[0])

    def _no_model_state(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> OfficialBusinessState:
        keyword = next(
            (
                item
                for item in _visible(page, _SEARCH_INPUTS)
                if item.input_value().strip() == task.model_name
            ),
            None,
        )
        region = _first_visible(page, _RESULT_REGION)
        if (
            keyword is None
            or region is None
            or self._first_exact_card(page, task.model_name) is not None
        ):
            raise LayoutRecognitionError("Apple no-model evidence is incomplete")
        return OfficialBusinessState.legal_no(
            canonical_url=self.require_approved_url(page.url),
            brand=task.brand,
            model_name=task.model_name,
            capacity=task.storage,
            color=task.color,
            outcome=BusinessOutcome.NO_MODEL,
            capture_view=OfficialCaptureView(
                (
                    _rect(keyword, "search_keyword"),
                    _rect(region, "result_region"),
                )
            ),
            detail_identity=None,
        )

    def _configuration_no(
        self,
        task: WebsiteTask,
        page: Any,
        identity: OfficialDetailIdentity,
        outcome: BusinessOutcome,
    ) -> OfficialBusinessState:
        kind = (
            "capacity"
            if outcome is BusinessOutcome.CAPACITY_UNAVAILABLE
            else "color"
        )
        group = self._group(page, kind)
        title = self._require_detail_title(page, task.model_name)
        target = self._exact_option(page, kind, task)
        if group is None or (target is not None and not _disabled(target)):
            raise LayoutRecognitionError(
                f"Apple {kind} legal-no evidence is invalid"
            )
        if kind == "color":
            self._require_selected(page, "capacity", task)
        return OfficialBusinessState.legal_no(
            canonical_url=identity.canonical_url,
            brand=task.brand,
            model_name=task.model_name,
            capacity=task.storage,
            color=task.color,
            outcome=outcome,
            capture_view=OfficialCaptureView(
                (_rect(title, "title"), _rect(group, f"{kind}_group"))
            ),
            detail_identity=identity.product_key,
        )

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> None:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)
        key = (id(browser), task.task_id, expected.current_sku)
        if key in self._prepared:
            return
        if expected.outcome is not BusinessOutcome.PRICE_FOUND:
            current = self.build_observation(
                task,
                self._read_business_state(task, browser),
            )
            if not self._same_legal_no_business_state(
                current.semantic_state,
                expected,
            ):
                raise LayoutRecognitionError("Apple legal-no capture state changed")
            self._prepared.add(key)
            return
        scaled = False
        try:
            proofs = self._capture_proofs(task, browser)
            if not _proofs_fit(browser, proofs):
                ensure_capture_scale(browser, scale=0.8)
                scaled = True
            self._final_capture_state(task, browser, expected)
        except Exception:
            if scaled:
                restore_capture_scale(browser)
            raise
        self._prepared.add(key)

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> None:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)
        self._prepared.discard((id(browser), task.task_id, expected.current_sku))
        if expected.outcome is BusinessOutcome.PRICE_FOUND:
            restore_capture_scale(browser)

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)
        if expected.outcome is not BusinessOutcome.PRICE_FOUND:
            current = self.build_observation(
                task,
                self._read_business_state(task, browser),
            )
            if not self._same_legal_no_business_state(
                current.semantic_state,
                expected,
            ):
                raise LayoutRecognitionError("Apple legal-no capture state changed")
            return current.css_rectangles
        return self._final_capture_state(
            task,
            browser,
            expected,
        ).css_rectangles

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> SemanticStateReader:
        """Return Apple's one authoritative final-frame reader.

        Detail identity, selected SKU and price were established before the
        formal-capture handoff.  Re-running the entire React configurator here
        rejects a visibly complete frame during harmless rerenders.  Instead,
        the same live title/price/capacity/colour snapshot is used for final
        positioning, semantic stability and capture geometry.
        """

        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)

        def reader() -> VerifiedSemanticState:
            if expected.outcome is not BusinessOutcome.PRICE_FOUND:
                return super(AppleOfficialAdapter, self).verified_state_reader(
                    task,
                    page,
                    expected,
                )()
            return self._final_capture_state(task, browser, expected)

        return reader

    def _final_capture_state(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> VerifiedSemanticState:
        """Build the stable four-proof state used immediately before capture."""

        self.raise_if_manual_action(page)
        identity = _detail_identity(page.url)
        expected_identity = self._expected_detail_identity(
            expected,
            "official-detail:",
        )
        if identity != expected_identity:
            raise LayoutRecognitionError("Apple final detail identity changed")

        proofs: tuple[Any, Any, Any, Any] | None = None
        for tick in range(_CAPTURE_STATE_WAIT_TICKS + 1):
            try:
                proofs = self._capture_proofs(task, page)
            except LayoutRecognitionError:
                proofs = None
            if proofs is not None:
                break
            if tick < _CAPTURE_STATE_WAIT_TICKS:
                page.wait_for_timeout(_CAPTURE_STATE_WAIT_MS)
        if proofs is None:
            raise LayoutRecognitionError("Apple final four-proof state is unavailable")

        proofs = self._reframe_capture_proofs(task, page, proofs)
        if not _proofs_fit(page, proofs):
            raise LayoutRecognitionError(
                "Apple final four-proof capture frame cannot be established"
            )
        price, _price_locator = self._current_price(page)
        if price != expected.price:
            raise LayoutRecognitionError("Apple final price changed")
        rectangles = tuple(
            _rect(locator, role)
            for locator, role in zip(
                proofs,
                ("title", "price", "capacity", "color"),
                strict=True,
            )
        )
        return replace(expected, css_rectangles=rectangles)

    def _capture_proofs(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> tuple[Any, Any, Any, Any]:
        title = self._capture_title(page, task.model_name)
        _price, price = self._current_price(page)
        capacity = self._require_selected(page, "capacity", task)
        color = self._require_selected(page, "color", task)
        return (
            title,
            _option_evidence(page, price),
            _option_evidence(page, capacity),
            self._selected_color_capture_evidence(page, color),
        )

    def _selected_color_capture_evidence(self, page: Any, color: Any) -> Any:
        """Use the exact card when possible, otherwise its verified group.

        Apple's React chooser can leave the selected colour label with a
        short-lived inconclusive hit-test even while the complete colour
        fieldset is visibly painted.  Selection still comes exclusively from
        the exact native radio verified by ``_require_selected`` above.  The
        containing group is only a visual screenshot proof, and remains
        subject to the normal strict exposure check and bounded reframing.
        """

        evidence = _option_evidence(page, color)
        state = _paint_state(evidence)
        if state is None or state[2] or not state[1]:
            return evidence
        group = self._group(page, "color")
        if group is None:
            return evidence
        group_state = _paint_state(group)
        if group_state is None or not group_state[0]:
            return evidence
        return group

    def _reframe_capture_proofs(
        self,
        task: WebsiteTask,
        page: Any,
        proofs: tuple[Any, Any, Any, Any],
    ) -> tuple[Any, Any, Any, Any]:
        """Stabilize Apple evidence with a small bounded correction loop.

        Apple puts colour above storage.  Using storage alone as an anchor
        produces a technically successful native screenshot that can exclude
        the selected colour.  Prefer the normal all-proof adjustment, then
        frame the colour/storage pair as one interval when the full purchase
        heading is too tall for the same viewport.  Apple can auto-scroll a
        second time after the first correction while its selected cards finish
        rendering, so a one-shot correction is insufficient.  Re-evaluate the
        real four proofs and permit the opposite bounded correction (including
        upward scroll) within a fixed budget.  Even an initially valid frame
        gets one 300ms confirmation sample before it is accepted.
        """

        if _proofs_fit(page, proofs):
            page.wait_for_timeout(_CAPTURE_GEOMETRY_WAIT_MS)
            settled = self._capture_proofs(task, page)
            if _proofs_fit(page, settled):
                return settled
            proofs = settled
        current = proofs
        for _correction in range(_CAPTURE_GEOMETRY_CORRECTIONS):
            delta = _proof_group_scroll_delta(page, current)
            if delta is None:
                delta = _apple_configuration_frame_scroll_delta(page, current)
            if delta is None:
                return current
            page.evaluate(_GROUP_SCROLL, {"delta": delta})
            page.wait_for_timeout(_CAPTURE_GEOMETRY_WAIT_MS)
            current = self._capture_proofs(task, page)
            if not _proofs_fit(page, current):
                continue
            # One more sample catches Apple's delayed auto-scroll.  If it
            # moves colour above the viewport, the next loop iteration applies
            # a negative delta instead of failing or saving a capacity-only
            # screenshot.
            page.wait_for_timeout(_CAPTURE_GEOMETRY_WAIT_MS)
            settled = self._capture_proofs(task, page)
            if _proofs_fit(page, settled):
                return settled
            current = settled
        return current

    def _capture_title(self, page: Any, model_name: str) -> Any:
        """Return the visible title proof without weakening detail identity.

        The large purchase heading remains the authoritative detail-page title
        used by ``_read_business_state``.  During screenshot framing, however,
        Apple keeps a shorter model title fixed at the top once the capacity
        cards become visible.  Accept it only when it names the same model and
        its rendered box is actually on screen.
        """

        for candidate in _visible(page, _CAPTURE_STICKY_TITLE):
            try:
                title = candidate.inner_text()
            except (AttributeError, RuntimeError):
                continue
            if _apple_model_matches(model_name, title) and _locator_exposed(
                page,
                candidate,
            ):
                return candidate
        return self._require_detail_title(page, model_name)


def _page(page: BrowserPage) -> Any:
    if not callable(getattr(page, "locator", None)) or not callable(
        getattr(page, "goto", None)
    ):
        raise TypeError("page must expose synchronous Playwright methods")
    return page


def _visible(scope: Any, selectors: tuple[str, ...]) -> tuple[Any, ...]:
    if scope is None:
        return ()
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            found = tuple(
                locator.nth(index)
                for index in range(locator.count())
                if locator.nth(index).is_visible()
            )
        except (AttributeError, RuntimeError):
            continue
        if found:
            return found
    return ()


def _first_visible(scope: Any, selectors: tuple[str, ...]) -> Any | None:
    found = _visible(scope, selectors)
    return found[0] if found else None


def _approved_purchase_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Apple product URL is missing")
    url = urljoin(_ENTRY, value.strip())
    if not _is_apple_purchase_url(url):
        raise ValueError("Apple product URL is not an approved purchase route")
    return url


def _is_apple_purchase_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.apple.com.cn"
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and _DETAIL_PATH.fullmatch(parsed.path) is not None
    )


def _is_apple_search_or_entry_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.apple.com.cn"
        and parsed.port in {None, 443}
        and parsed.path.startswith("/shop/buy-iphone")
    )


def _detail_identity(url: str) -> OfficialDetailIdentity:
    normalized = _approved_purchase_url(url)
    pieces = tuple(
        part for part in urlsplit(normalized).path.split("/") if part
    )
    return OfficialDetailIdentity(normalized, "/".join(pieces[-3:]))


def _same_apple_purchase_model(left: str, right: str) -> bool:
    """Whether two approved Apple purchase paths name the same iPhone model."""

    try:
        return _apple_purchase_model_key(left) == _apple_purchase_model_key(right)
    except ValueError:
        return False


def _apple_purchase_model_key(url: str) -> str:
    normalized = _approved_purchase_url(url)
    parts = tuple(part for part in urlsplit(normalized).path.split("/") if part)
    try:
        marker = parts.index("buy-iphone")
        return parts[marker + 1].casefold()
    except (ValueError, IndexError):
        raise ValueError("Apple purchase route has no iPhone model") from None


def _apple_is_selected_sku_route(url: str) -> bool:
    normalized = _approved_purchase_url(url)
    parts = tuple(part for part in urlsplit(normalized).path.split("/") if part)
    try:
        marker = parts.index("buy-iphone")
    except ValueError:
        return False
    return len(parts) > marker + 2


def _normalize_storage(value: str) -> str:
    return re.sub(r"\s+", "", normalize_product_text(value))


def _storage_option_matches(target: str, candidate: str) -> bool:
    """Match an Apple capacity card whose text continues with its price."""

    wanted = _normalize_storage(target)
    actual = _normalize_storage(candidate)
    if actual == wanted:
        return True
    # Apple appends a superscript footnote immediately after the capacity;
    # normalization turns that glyph into a digit (``256GB¹`` -> ``256GB1``).
    # A storage card starts with its uniquely selected capacity, followed by
    # that footnote and the RMB price copy.
    return actual.startswith(wanted)


def _apple_color_matches(target: str, candidate: str) -> bool:
    """Match a confirmed Apple China colour alias without fuzzy selection."""

    if color_matches(target, candidate):
        return True
    wanted = _apple_color_key(target)
    actual = _apple_color_key(candidate)
    return bool(
        wanted
        and actual
        and actual in _APPLE_COLOR_ALIASES.get(wanted, frozenset())
    )


def _apple_color_key(value: str) -> str:
    normalized = normalize_product_text(value)
    return normalized[:-1] if normalized.endswith("色") else normalized


def _apple_model_matches(target: str, candidate: str) -> bool:
    """Match a device title after Apple China's non-model purchase prefix.

    ``购买 iPhone 17`` is the same phone as ``iPhone 17``.  The prefix is a
    storefront action, not part of the model name, so it must not block a
    detail page which was already reached from an exact iPhone product card.
    """

    stripped = re.sub(r"^(?:购买|选购|BUY)\s*", "", candidate, flags=re.IGNORECASE)
    return model_matches(target, stripped)


def _apple_card_model_matches(target: str, candidate: str) -> bool:
    """Match an exact device card without treating availability as a variant.

    Apple can show an exact model card as temporarily unavailable.  Inventory
    text is not a model suffix and must not prevent entry to the detail page,
    where the requested storage and colour are still verified independently.
    """

    return _apple_model_matches(
        target,
        _CARD_AVAILABILITY_COPY.sub(" ", candidate),
    )


def _disabled(locator: Any) -> bool:
    classes = str(locator.get_attribute("class") or "").lower().split()
    return (
        locator.get_attribute("disabled") is not None
        or str(locator.get_attribute("aria-disabled") or "").lower() == "true"
        or any(marker in classes for marker in ("disabled", "unavailable"))
    )


def _selected(locator: Any) -> bool:
    if locator.get_attribute("checked") is not None:
        return True
    try:
        native_checked = locator.evaluate("(element) => Boolean(element.checked)")
    except (AttributeError, RuntimeError, TypeError):
        native_checked = False
    if native_checked is True:
        return True
    return any(
        str(locator.get_attribute(name) or "").lower() == "true"
        for name in ("aria-selected", "aria-pressed", "aria-checked")
    )


def _option_text(locator: Any) -> str:
    """Return visible option text, with Apple swatch accessibility fallback."""

    pieces = [locator.inner_text()]
    for attribute in ("aria-label", "title"):
        value = locator.get_attribute(attribute)
        if isinstance(value, str):
            pieces.append(value)
    try:
        label_text = locator.evaluate(
            """(element) => Array.from(element.labels || [])
                .map((label) => label.innerText || label.textContent || '')
                .join(' ')"""
        )
    except (AttributeError, RuntimeError, TypeError):
        label_text = None
    if isinstance(label_text, str):
        pieces.append(_collapse_repeated_label_text(label_text))
    return " ".join(piece.strip() for piece in pieces if piece and piece.strip())


def _collapse_repeated_label_text(value: str) -> str:
    """Collapse two identical React labels associated with one native radio."""

    tokens = value.split()
    midpoint = len(tokens) // 2
    if len(tokens) >= 2 and len(tokens) % 2 == 0:
        if tokens[:midpoint] == tokens[midpoint:]:
            return " ".join(tokens[:midpoint])
    return value


def _option_evidence(page: Any, option: Any) -> Any:
    """Return the visible card/label for a native Apple radio option.

    Apple renders the interactive radio input separately from its visible
    configuration card.  The input is still authoritative for selection, but
    using it as screenshot evidence can yield a zero-sized or invisible box.
    Its associated label contains the required colour or capacity text (and,
    for capacity, the selected full-device price).
    """

    option_type = str(option.get_attribute("type") or "").lower()
    if option_type != "radio":
        return option

    option_id = option.get_attribute("id")
    if not isinstance(option_id, str) or not option_id:
        raise LayoutRecognitionError(
            "Apple selected configuration has no visible evidence card"
        )

    # Apple assigns React-generated IDs to some native radios (for example
    # ``:r0:``).  Do not turn that ID into a CSS selector: CSS escaping varies
    # across browser versions and would fall back to the hidden radio itself.
    # Compare the DOM relationship directly instead, so the formal screenshot
    # always proves the visible colour/capacity card.
    matching_labels = tuple(
        label
        for label in _visible(page, ("label",))
        if label.get_attribute("for") == option_id
    )
    if not matching_labels:
        raise LayoutRecognitionError(
            "Apple selected configuration has no visible evidence card"
        )

    states = tuple((label, _paint_state(label)) for label in matching_labels)
    for label, state in states:
        if state is not None and state[2]:
            return label
    # When React leaves an old label in the DOM, prefer the real rendered card
    # that is merely outside the viewport.  The bounded capture reframe can
    # then scroll that exact card into view.  An in-viewport but covered stale
    # label must never win only because it still owns a rectangle.
    for label, state in states:
        if state is not None and state[0] and not state[1]:
            return label
    # Older deterministic fixtures do not implement browser hit testing.
    # Preserve their established geometry-only behavior without weakening the
    # live browser path, where _paint_state always returns structured booleans.
    for label, state in states:
        if state is None:
            return label
    # Keep the real covered node so _proofs_fit rejects the frame and the
    # caller can retry/fail closed; this path is also usable before a click.
    return matching_labels[0]


def _money_values(text: str) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for match in _MONEY.finditer(text):
        try:
            value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        if value.is_finite() and value >= 0:
            values.append(value)
    return tuple(values)


def _price_text_rejected(value: str) -> bool:
    normalized = normalize_product_text(value)
    return any(marker in normalized for marker in _PRICE_EXCLUSIONS)


def _rect(locator: Any, role: str) -> CssRect:
    box = locator.bounding_box()
    if not isinstance(box, dict):
        raise LayoutRecognitionError(f"Apple {role} evidence has no geometry")
    try:
        return CssRect(
            float(box["x"]),
            float(box["y"]),
            float(box["width"]),
            float(box["height"]),
            role,
        )
    except (KeyError, TypeError, ValueError):
        raise LayoutRecognitionError(
            f"Apple {role} evidence geometry is invalid"
        ) from None


def _proofs_fit(page: Any, proofs: tuple[Any, Any, Any, Any]) -> bool:
    return all(_locator_exposed(page, locator) for locator in proofs)


def _paint_state(locator: Any) -> tuple[bool, bool, bool] | None:
    """Return rendered/in-viewport/exposed facts for one exact proof node.

    ``None`` means the deterministic page double does not implement the live
    hit-test contract.  A malformed structured response is never accepted as
    visibility evidence.
    """

    try:
        raw = locator.evaluate(_PAINT_STATE)
    except (AttributeError, RuntimeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    values = (raw.get("rendered"), raw.get("insideViewport"), raw.get("exposed"))
    if not all(isinstance(value, bool) for value in values):
        return None
    rendered, inside, exposed = values
    return bool(rendered), bool(inside), bool(exposed)


def _locator_exposed(page: Any, locator: Any) -> bool:
    """Whether a proof is actually painted, with legacy fixture fallback."""

    state = _paint_state(locator)
    if state is not None:
        return state[2]
    return _locator_in_viewport(page, locator)


def _locator_in_viewport(page: Any, locator: Any) -> bool:
    """Whether one rendered proof is wholly inside the current viewport."""

    try:
        viewport = page.evaluate(_FIT_VIEWPORT)
        width = float(viewport["width"])
        height = float(viewport["height"])
        box = locator.bounding_box()
        if not isinstance(box, dict):
            return False
        left, top = float(box["x"]), float(box["y"])
        right = left + float(box["width"])
        bottom = top + float(box["height"])
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return False
    return left >= 0 and top >= 0 and right <= width and bottom <= height


def _proof_group_scroll_delta(
    page: Any,
    proofs: tuple[Any, Any, Any, Any],
) -> float | None:
    """Return one bounded scroll that keeps all four Apple proofs on screen."""

    try:
        viewport = page.evaluate(_FIT_VIEWPORT)
        height = float(viewport["height"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    boxes: list[tuple[float, float]] = []
    for locator in proofs:
        box = locator.bounding_box()
        if not isinstance(box, dict):
            return None
        try:
            top = float(box["y"])
            bottom = top + float(box["height"])
        except (KeyError, TypeError, ValueError):
            return None
        boxes.append((top, bottom))
    group_top = min(top for top, _bottom in boxes)
    group_bottom = max(bottom for _top, bottom in boxes)
    safe_top = _CAPTURE_EDGE_PX
    safe_bottom = height - _CAPTURE_EDGE_PX
    if group_bottom - group_top > safe_bottom - safe_top:
        return None
    if group_bottom > safe_bottom:
        delta = group_bottom - safe_bottom
    elif group_top < safe_top:
        delta = group_top - safe_top
    else:
        return None
    return _bounded_capture_scroll(delta)


def _apple_configuration_frame_scroll_delta(
    page: Any,
    proofs: tuple[Any, Any, Any, Any],
) -> float | None:
    """Frame Apple's selected colour and storage in the same safe viewport.

    This fallback is only used after the all-proof group is taller than the
    viewport (normally because the full heading has not yet become sticky).
    It must never treat the selected storage card as the sole scroll anchor:
    that can push colour out of the resulting screenshot.
    """

    try:
        viewport = page.evaluate(_FIT_VIEWPORT)
        height = float(viewport["height"])
        boxes = []
        for proof in (proofs[2], proofs[3]):
            box = proof.bounding_box()
            if not isinstance(box, dict):
                return None
            top = float(box["y"])
            boxes.append((top, top + float(box["height"])))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    safe_top = _CAPTURE_EDGE_PX
    safe_bottom = height - _CAPTURE_EDGE_PX
    # A positive delta moves page content up.  Each proof must satisfy:
    # ``proof_bottom - safe_bottom <= delta <= proof_top - safe_top``.
    lower = max(bottom - safe_bottom for _top, bottom in boxes)
    upper = min(top - safe_top for top, _bottom in boxes)
    if lower > upper:
        return None
    if lower <= 0 <= upper:
        # The configuration already fits.  If the long heading is the only
        # missing proof, advance in small repeatable steps until Apple's
        # compact sticky title actually appears.  A one-pixel synthetic nudge
        # is insufficient on the live purchase page.
        return (
            _STICKY_ACTIVATION_SCROLL_PX
            if not _locator_in_viewport(page, proofs[0])
            else None
        )
    delta = lower if lower > 0 else upper
    return _bounded_capture_scroll(delta)


def _bounded_capture_scroll(delta: float) -> float | None:
    """Clamp one Apple positioning correction to a small signed step."""

    if not math.isfinite(delta) or delta == 0:
        return None
    magnitude = min(
        _MAX_CAPTURE_SCROLL_STEP_PX,
        max(_MIN_CAPTURE_SCROLL_STEP_PX, abs(delta)),
    )
    return math.copysign(magnitude, delta)


_PRICE_STYLE = """
(element) => {
  let current = element;
  let effectiveLineThrough = false;
  while (current) {
    const style = window.getComputedStyle(current);
    if ((style.textDecorationLine || style.textDecoration || '')
        .includes('line-through')) effectiveLineThrough = true;
    current = current.parentElement;
  }
  return {effectiveLineThrough};
}
"""

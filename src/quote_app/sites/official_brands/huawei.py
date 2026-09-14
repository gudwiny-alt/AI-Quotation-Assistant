from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError
from quote_app.evidence.semantic_state import SemanticStateReader, VerifiedSemanticState
from quote_app.sites.matching import color_matches, normalize_product_text
from quote_app.sites.official_brands.base import LiveOfficialAdapterBase
from quote_app.sites.official_brands.models import (
    ApprovedHostFamily,
    OfficialBusinessState,
    OfficialCaptureView,
    OfficialDetailIdentity,
    OfficialManualAction,
    OfficialOfferSnapshot,
)
from quote_app.sites.protocol import (
    AdapterObservation,
    BrowserPage,
    CaptureReadyObservation,
)
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import LayoutRecognitionError, NonRetryableTechnicalError

_ENTRY = "https://www.vmall.com/"
_CAPTURE_PENDING_REGION = "excluded-from-live-official-stability:region"
_CAPTURE_PENDING_STOCK = "excluded-from-live-official-stability:stock"
_APPROVED_HOSTS = frozenset(("www.vmall.com", "item.vmall.com"))
_DIRECT_DETAIL = re.compile(r"^/product/(?P<product_id>\d+)\.html$")
_COM_DETAIL = "/product/comdetail/index.html"
_MONEY = re.compile(r"(?<!\d)(?:[¥￥]\s*)?(\d[\d,]*(?:\.\d{1,2})?)(?!\d)")
_ACCESSORY_MARKERS = (
    "手机壳",
    "保护壳",
    "保护套",
    "手机套",
    "钢化膜",
    "保护膜",
    "贴膜",
    "充电器",
    "数据线",
    "耳机",
    "配件",
    "适用",
    "支架",
    "CASE",
    "COVER",
    "PROTECTOR",
    "FILM",
    "CHARGER",
    "CABLE",
    "HEADPHONES",
    "EARBUDS",
    "ACCESSORY",
)
_DERIVED_PREFIXES = (
    "PRO",
    "+",
    "PLUS",
    "ULTRA",
    "MAX",
    "MINI",
    "LITE",
    "AIR",
    "EDGE",
    "FE",
    "GT",
    "青春版",
    "优享版",
    "活力版",
    "竞速版",
    "至尊版",
)
_LEGAL_STATUS_TAILS = (
    "暂时缺货",
    "暂无现货",
    "缺货",
    "无货",
    "有货",
    "现货",
)
_COLOR_TAIL = re.compile(
    r"(?:[\u3400-\u9fff]{1,8}(?:黑|白|红|蓝|绿|紫|金|银|灰|橙|粉|青|棕|色)"
    r"|BLACK|WHITE|RED|BLUE|GREEN|PURPLE|GOLD|SILVER|GRAY|GREY|ORANGE|PINK)"
)
_CAPACITY_TAIL = re.compile(r"\d+(?:\.\d+)?(?:GB|TB)(?:\+\d+(?:\.\d+)?(?:GB|TB))?")
_EXACT_CURRENCY_AMOUNT = re.compile(
    r"^\s*[¥￥]\s*\d[\d,]*(?:\.\d{1,2})?\s*(?:元)?\s*$"
)
_EXACT_PLAIN_AMOUNT = re.compile(r"^\s*\d[\d,]*(?:\.\d{1,2})?\s*$")
_SEARCH_INPUT = "input#search-kw"
_RESULT_REGION = ".search-result"
_RESULT_CARD = "li[data-product-id]"
_RESULT_TITLE = ".product-name"
_RESULT_LINK = "a.product-link"
_PORTAL_RESULT_REGIONS = ("#react-root", 'main[data-testid="search-result-root"]', "main")
_PORTAL_RESULT_CARD = '[data-testid$="-searchProduct"]'
_PORTAL_TEXT = '[data-testid="vui_text_container"]'
_PORTAL_CONFIRMATION_TICKS = 40
_PORTAL_CONFIRMATION_WAIT_MS = 250
_CURRENT_RESULT_TITLES = (
    "a",
    "button",
    '[role="link"]',
    "h2",
    "h3",
    "h4",
    "p",
    "span",
    "div",
)
_DETAIL_TITLE = "div#prd-detail-name[data-testid=prd-detail-name]"
_PRICE_CANDIDATE = (
    "[data-prdid] .summary-price .current-price "
    "[data-testid=vui_text_container]"
)
_PRICE_TEXT_CANDIDATE = '[data-prdid] [data-testid=price_text]'
_PRICE_CANDIDATES = (
    _PRICE_TEXT_CANDIDATE,
    _PRICE_CANDIDATE,
    '[data-prdid] [class*="sku-price-current"][data-testid=vui_text_container]',
    '[data-prdid] [class*="skuPriceCurrent"][data-testid=vui_text_container]',
    '[data-prdid] [class*="price"] [data-testid=vui_text_container]',
    '[data-prdid] [class*="Price"] [data-testid=vui_text_container]',
)
_CONFIGURATION_SETTLE_MS = 800
_CAPTURE_SETTLE_MS = 1200
_SELECTED_CANDIDATE = "[style]"
_OPTION_SOURCES = ('div[tabindex="0"]', "button")
_RISK_MARKERS = (
    '[id*="captcha"]',
    '[class*="captcha"]',
    '[class*="geetest"]',
    'iframe[src*="captcha"]',
    'iframe[src*="verify"]',
)
_LOGIN_MARKERS = (
    'input[type="password"]',
    'input[placeholder*="登录密码"]',
    'form[action*="login"]',
    '[class*="login-form"]',
)
_PRICE_EXCLUSION_MARKERS = (
    "补贴",
    "券后",
    "优惠券",
    "满减",
    "直降",
    "以旧换新",
    "分期",
    "保险",
    "延保",
    "延长服务",
    "服务价",
    "配件",
    "REFERENCE",
    "LINE-THROUGH",
    "SUBSIDY",
    "COUPON",
    "INSTALLMENT",
    "TRADE-IN",
    "INSURANCE",
    "WARRANTY",
    "SERVICE-PRICE",
    "ACCESSORY",
)
_CAPTURE_SELECTORS = {
    "title": {"role": "title", "selector": _DETAIL_TITLE},
    "capacity": {
        "role": "capacity",
        "selector": _SELECTED_CANDIDATE,
        "group_label": "版本",
    },
    "color": {
        "role": "color",
        "selector": _SELECTED_CANDIDATE,
        "group_label": "颜色",
    },
}
_PRICE_STYLE = """
(element) => {
  let current = element;
  let lineThrough = false;
  const contextText = (element.parentElement?.textContent || '').trim();
  const ancestorClasses = [];
  while (current) {
    const style = window.getComputedStyle(current);
    if ((style.textDecorationLine || style.textDecoration || '')
        .includes('line-through')) lineThrough = true;
    ancestorClasses.push(String(current.className || ''));
    current = current.parentElement;
  }
  const productRoot = element.closest('[data-prdid]');
  const primaryDetailRoot = Boolean(productRoot?.querySelector(
    '#prd-detail-name[data-testid="prd-detail-name"]'
  ));
  return {
    color: window.getComputedStyle(element).color,
    effectiveLineThrough: lineThrough,
    contextText,
    ancestorClasses,
    primaryDetailRoot,
    tagName: element.tagName,
    childElementCount: element.childElementCount,
  };
}
"""
_OPTION_INDEXES = r"""
(spec) => {
  // VMALL_CONFIG_OPTION_INDEXES: resolve only the requested configuration area.
  const selector = String(spec?.selector || '');
  const label = String(spec?.label || '');
  if (!selector || !label) return [];
  const visible = (element) => {
    const style = window.getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      box.width > 0 && box.height > 0;
  };
  const belongsToGroup = (element) => {
    let current = element.parentElement;
    while (current) {
      const hasDirectLabel = Array.from(current.children).some(child =>
        (child.textContent || '').trim() === label
      );
      if (hasDirectLabel) return true;
      current = current.parentElement;
    }
    return false;
  };
  return Array.from(document.querySelectorAll(selector))
    .map((element, index) => ({element, index}))
    .filter(({element}) => visible(element) && belongsToGroup(element))
    .map(({index}) => index);
}
"""
_OPTION_SELECTED = r"""
(element) => {
  // VMALL_CONFIG_OPTION_SELECTED: the red state is split between tile and text.
  const nodes = [element, ...element.querySelectorAll('*')];
  const red = (value) => String(value || '').replace(/\s/g, '').toLowerCase() ===
    'rgb(207,10,44)';
  const hasBorder = nodes.some(node => {
    const style = window.getComputedStyle(node);
    return red(style.borderTopColor) || red(style.borderRightColor) ||
      red(style.borderBottomColor) || red(style.borderLeftColor);
  });
  const hasTextColor = nodes.some(node => red(window.getComputedStyle(node).color));
  return hasBorder && hasTextColor;
}
"""
_FIT = r"""
(proofs) => {
  const resolve = (proof) => {
    let candidates = Array.from(document.querySelectorAll(proof.selector));
    if (proof.primary_detail_root === 'true') {
      candidates = candidates.filter(element => {
        const productRoot = element.closest('[data-prdid]');
        return Boolean(productRoot?.querySelector(
          '#prd-detail-name[data-testid="prd-detail-name"]'
        ));
      });
    }
    if (!proof.group_label) {
      const exact = proof.expected_text === undefined ? candidates : candidates.filter(
        element => (element.textContent || '').trim() === proof.expected_text
      );
      return exact.length === 1 ? exact[0] : null;
    }
    const selected = candidates.filter(element => {
      const nodes = [element, ...element.querySelectorAll('*')];
      const red = (value) => String(value || '').replace(/\s/g, '').toLowerCase() ===
        'rgb(207,10,44)';
      const hasBorder = nodes.some(node => {
        const style = window.getComputedStyle(node);
        return red(style.borderTopColor) || red(style.borderRightColor) ||
          red(style.borderBottomColor) || red(style.borderLeftColor);
      });
      const hasTextColor = nodes.some(node => red(window.getComputedStyle(node).color));
      if (!hasBorder || !hasTextColor) return false;
      if ((element.textContent || '').trim() !== proof.expected_text) return false;
      let current = element.parentElement;
      while (current) {
        const text = (current.textContent || '').trim();
        if (text.startsWith(proof.group_label)) return true;
        current = current.parentElement;
      }
      return false;
    });
    return selected.length === 1 ? selected[0] : null;
  };
  const nodes = Object.values(proofs).map(resolve);
  return nodes.every(element => {
    const box = element?.getBoundingClientRect();
    if (!box || box.width <= 0 || box.height <= 0 || box.top < 0 || box.left < 0 ||
        box.bottom > window.innerHeight || box.right > window.innerWidth) return false;
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return Boolean(hit) && (hit === element || element.contains(hit) || hit.contains(element));
  });
}
"""
_GEOMETRY = r"""
(proofs) => {
  const resolve = (proof) => {
    let candidates = Array.from(document.querySelectorAll(proof.selector));
    if (proof.primary_detail_root === 'true') {
      candidates = candidates.filter(element => {
        const productRoot = element.closest('[data-prdid]');
        return Boolean(productRoot?.querySelector(
          '#prd-detail-name[data-testid="prd-detail-name"]'
        ));
      });
    }
    if (!proof.group_label) {
      const exact = proof.expected_text === undefined ? candidates : candidates.filter(
        element => (element.textContent || '').trim() === proof.expected_text
      );
      return exact.length === 1 ? exact[0] : null;
    }
    const selected = candidates.filter(element => {
      const nodes = [element, ...element.querySelectorAll('*')];
      const red = (value) => String(value || '').replace(/\s/g, '').toLowerCase() ===
        'rgb(207,10,44)';
      const hasBorder = nodes.some(node => {
        const style = window.getComputedStyle(node);
        return red(style.borderTopColor) || red(style.borderRightColor) ||
          red(style.borderBottomColor) || red(style.borderLeftColor);
      });
      const hasTextColor = nodes.some(node => red(window.getComputedStyle(node).color));
      if (!hasBorder || !hasTextColor) return false;
      if ((element.textContent || '').trim() !== proof.expected_text) return false;
      let current = element.parentElement;
      while (current) {
        if ((current.textContent || '').trim().startsWith(proof.group_label)) return true;
        current = current.parentElement;
      }
      return false;
    });
    return selected.length === 1 ? selected[0] : null;
  };
  const elements = Object.values(proofs).map(resolve);
  if (elements.some(element => element === null)) return {missing: true};
  const boxes = elements.map(element => element.getBoundingClientRect());
  const occlusions = elements.flatMap((element, index) => {
    const box = boxes[index];
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    const clear = Boolean(hit) &&
      (hit === element || element.contains(hit) || hit.contains(element));
    if (clear || !hit) return [];
    const blocker = hit.getBoundingClientRect();
    return [{proofBottom: box.bottom, blockerTop: blocker.top}];
  });
  return {
    unionTop: Math.min(...boxes.map(box => box.top)),
    unionBottom: Math.max(...boxes.map(box => box.bottom)),
    occlusions,
    viewportHeight: window.innerHeight,
    scrollY: window.scrollY,
  };
}
"""
_SCROLL = """
(state) => {
  if (!state || !Number.isFinite(state.delta)) return false;
  window.scrollBy(0, state.delta);
  return true;
}
"""



class _PriceUnavailable(LayoutRecognitionError):
    """The approved current-price region has not completed yet."""


@dataclass(frozen=True, slots=True)
class _ExactSearchTarget:
    locator: Any
    source: Literal["legacy_link", "portal_card", "current_text"]
    prevalidated_url: str | None


class HuaweiOfficialAdapter(LiveOfficialAdapterBase):
    """Independent VMALL adapter for canonical Huawei official tasks."""

    approved_host_families = (
        ApprovedHostFamily("www.vmall.com", allow_subdomains=False),
        ApprovedHostFamily("item.vmall.com", allow_subdomains=False),
    )

    def __init__(self, spec: Any) -> None:
        super().__init__(spec)
        self._prepared: set[tuple[int, str, str]] = set()

    def _validate_task(self, task: WebsiteTask) -> None:
        if (
            not isinstance(task, WebsiteTask)
            or task.brand != "华为"
            or task.channel is not WebsiteChannel.OFFICIAL
        ):
            raise NonRetryableTechnicalError(
                "OFFICIAL_TASK_MISMATCH",
                "Huawei adapter only accepts 华为 OFFICIAL tasks",
            )

    def _manual_action(self, page: BrowserPage) -> OfficialManualAction | None:
        browser = _page(page)
        parsed = urlsplit(str(getattr(browser, "url", "")))
        path = parsed.path.lower()
        host = (parsed.hostname or "").lower()
        if (
            any(marker in path for marker in ("captcha", "risk", "verify", "security"))
            or _visible(browser, _RISK_MARKERS)
        ):
            return OfficialManualAction.SECURITY_VERIFICATION
        if (
            (host.endswith("cloud.huawei.com") and any(marker in path for marker in ("cas", "login")))
            or any(marker in path for marker in ("/login", "/signin", "/account/login"))
            or _visible(browser, _LOGIN_MARKERS)
        ):
            return OfficialManualAction.LOGIN
        return None

    def _observe_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        observation = self._observe_validated_mode(task, page, defer_price=False)
        if not isinstance(observation, AdapterObservation):
            raise AssertionError("normal VMALL observation cannot defer price")
        return observation

    def observe_for_capture(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation | CaptureReadyObservation:
        self._validate_task(task)
        self.raise_if_manual_action(page)
        return self._observe_validated_mode(task, page, defer_price=True)

    def _observe_validated_mode(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        *,
        defer_price: bool,
    ) -> AdapterObservation | CaptureReadyObservation:
        browser = _page(page)
        search_url = (
            "https://www.vmall.com/portal/search/index.html?"
            f"targetRoute=searchresult&searchWord={quote(task.model_name)}"
        )
        browser.goto(search_url, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser)
        self.require_approved_url(browser.url)
        if not _is_search_url(browser.url):
            raise LayoutRecognitionError("VMALL direct search did not reach search results")
        target = self._wait_for_exact_result(browser, task)
        if target is None:
            return self.build_observation(task, self._no_model_state(task, browser))
        before_url = str(browser.url)
        controlled_pages = _context_pages(browser)
        if target.source == "portal_card":
            _press_portal_card(browser, target.locator)
            self._wait_for_portal_detail(
                browser,
                before_url=before_url,
                controlled_pages=controlled_pages,
            )
            return self._observe_detail(task, browser, defer_price=defer_price)
        target.locator.click()
        if target.source == "current_text":
            # The hydrated title can be visible before the portal card selector.
            # Its click also opens a new tab; wait for the approved URL to commit.
            self._wait_for_portal_detail(browser, before_url=before_url, controlled_pages=controlled_pages)
        browser.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser)
        if target.prevalidated_url is not None:
            if (
                _detail_identity(browser.url).product_key
                != _detail_identity(target.prevalidated_url).product_key
            ):
                raise LayoutRecognitionError("VMALL final detail identity changed")
        elif not _is_detail_url(browser.url):
            raise LayoutRecognitionError("VMALL exact-model card did not open a detail page")
        return self._observe_detail(task, browser, defer_price=defer_price)

    def _wait_for_portal_detail(
        self,
        page: Any,
        *,
        before_url: str,
        controlled_pages: tuple[Any, ...] | None,
    ) -> None:
        opened_pages: tuple[Any, ...] = ()
        deadline = monotonic() + (
            _PORTAL_CONFIRMATION_TICKS * _PORTAL_CONFIRMATION_WAIT_MS / 1000
        )
        try:
            for tick in range(_PORTAL_CONFIRMATION_TICKS + 1):
                self.raise_if_manual_action(page)
                opened_pages = _merge_pages(
                    opened_pages,
                    _new_context_pages(controlled_pages, _context_pages(page)),
                )
                if len(opened_pages) > 1:
                    raise LayoutRecognitionError(
                        "VMALL portal card opened multiple new pages"
                    )
                current_url = str(getattr(page, "url", ""))
                if current_url != before_url:
                    if not _is_detail_url(current_url):
                        raise LayoutRecognitionError(
                            "VMALL portal card did not reach approved numeric detail"
                        )
                    page.wait_for_load_state("domcontentloaded")
                    self.raise_if_manual_action(page)
                    return
                if len(opened_pages) == 1:
                    popup = opened_pages[0]
                    popup_url = str(getattr(popup, "url", ""))
                    # Native Chrome can expose an empty URL before the popup's
                    # first navigation commits. Keep the existing bounded wait;
                    # validate the destination as soon as it becomes available.
                    if popup_url not in {"", "about:blank"}:
                        if not _is_detail_url(popup_url):
                            raise LayoutRecognitionError(
                                "VMALL portal popup did not reach approved detail"
                            )
                        page.goto(popup_url, wait_until="domcontentloaded")
                        page.wait_for_load_state("domcontentloaded")
                        self.raise_if_manual_action(page)
                        return
                if tick < _PORTAL_CONFIRMATION_TICKS:
                    remaining_ms = _remaining_portal_wait_ms(deadline)
                    if remaining_ms > 0:
                        page.wait_for_timeout(remaining_ms)
            raise LayoutRecognitionError(
                "VMALL portal card bounded detail confirmation timed out"
            )
        finally:
            _close_pages(opened_pages)

    def _resume_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        browser = _page(page)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            if not _is_search_url(checkpoint.url):
                raise NonRetryableTechnicalError(
                    "RECOVERY_INVALID", "VMALL no-model checkpoint is not a search URL"
                )
        elif not _is_detail_url(checkpoint.url):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID", "VMALL detail checkpoint is not a numeric product URL"
            )
        browser.goto(checkpoint.url, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        self.raise_if_manual_action(browser)
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            if self._wait_for_exact_result(browser, task) is not None:
                raise LayoutRecognitionError("VMALL recovered no-model state changed")
            return self.build_observation(task, self._no_model_state(task, browser))
        observation = self._observe_detail(task, browser)
        if not isinstance(observation, AdapterObservation):
            raise AssertionError("resumed VMALL observation cannot defer price")
        return observation

    def _observe_detail(
        self,
        task: WebsiteTask,
        page: Any,
        *,
        defer_price: bool = False,
    ) -> AdapterObservation | CaptureReadyObservation:
        self.raise_if_manual_action(page)
        identity = _detail_identity(page.url)
        self._require_detail_title(page, task.model_name)
        capacity = self._wait_for_target_option(page, "capacity", task)
        if capacity is None or _disabled(capacity):
            return self.build_observation(
                task,
                self._configuration_no(
                    task, page, identity, BusinessOutcome.CAPACITY_UNAVAILABLE
                ),
            )
        self._select(page, "capacity", task, force_click=True)
        color = self._wait_for_target_option(page, "color", task)
        if color is None or _disabled(color):
            return self.build_observation(
                task,
                self._configuration_no(
                    task,
                    page,
                    _detail_identity(page.url),
                    BusinessOutcome.COLOR_UNAVAILABLE,
                ),
            )
        self._select(page, "color", task)
        if defer_price:
            pending = self._capture_ready_observation(task, page)
            page.wait_for_timeout(_CAPTURE_SETTLE_MS)
            return pending
        page.wait_for_timeout(_CONFIGURATION_SETTLE_MS)
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

    def _capture_ready_observation(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> CaptureReadyObservation:
        identity = _detail_identity(page.url)
        self._require_detail_title(page, task.model_name)
        self._require_selected(page, "capacity", task)
        self._require_selected(page, "color", task)
        url = self.require_approved_url(identity.canonical_url)
        semantic_state = VerifiedSemanticState(
            canonical_url=url,
            brand=task.brand,
            model_name=task.model_name,
            capacity=self._capacity_value(page, task),
            color=task.color,
            current_sku=f"official-detail:{identity.product_key}",
            region=_CAPTURE_PENDING_REGION,
            stock_state=_CAPTURE_PENDING_STOCK,
            price=None,
            outcome=BusinessOutcome.PRICE_FOUND,
            css_rectangles=(),
            price_pending=True,
        )
        return CaptureReadyObservation(
            url=url,
            css_rectangles=(),
            semantic_state=semantic_state,
        )

    def finalize_observation(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        observation: CaptureReadyObservation,
    ) -> AdapterObservation:
        self._validate_task(task)
        if not isinstance(observation, CaptureReadyObservation):
            raise LayoutRecognitionError("VMALL capture-ready state is unavailable")
        self._validate_expected_state(task, observation.semantic_state)
        browser = _page(page)
        identity = _detail_identity(observation.url)
        set_default_timeout = getattr(browser, "set_default_timeout", None)
        try:
            if callable(set_default_timeout):
                set_default_timeout(2_000)
            price, _node = self._current_price(browser)
        except Exception as error:
            raise NonRetryableTechnicalError(
                "PRICE_UNAVAILABLE_AFTER_CAPTURE",
                "华为官网截图成功，但当前价格未能读取",
            ) from error
        finally:
            if callable(set_default_timeout):
                try:
                    set_default_timeout(30_000)
                except Exception:
                    pass
        return self.build_observation(
            task,
            OfficialBusinessState.price_found(
                identity=identity,
                brand=task.brand,
                model_name=task.model_name,
                capacity=observation.semantic_state.capacity,
                color=observation.semantic_state.color,
                price=price,
            ),
        )

    def _read_business_state(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> OfficialBusinessState:
        browser = _page(page)
        if not _is_detail_url(browser.url):
            return self._no_model_state(task, browser)
        identity = _detail_identity(browser.url)
        self._require_detail_title(browser, task.model_name)
        capacity = self._exact_option(browser, "capacity", task)
        if capacity is None or _disabled(capacity):
            return self._configuration_no(
                task, browser, identity, BusinessOutcome.CAPACITY_UNAVAILABLE
            )
        self._require_selected(browser, "capacity", task)
        color = self._exact_option(browser, "color", task)
        if color is None or _disabled(color):
            return self._configuration_no(
                task, browser, identity, BusinessOutcome.COLOR_UNAVAILABLE
            )
        self._require_selected(browser, "color", task)
        return OfficialBusinessState.price_found(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=self._capacity_value(browser, task),
            color=task.color,
            price=self._current_price(browser)[0],
        )

    def _offer_matches_task(
        self,
        task: WebsiteTask,
        snapshot: OfficialOfferSnapshot,
    ) -> bool:
        expected_full = _normalize_capacity(f"{task.ram}+{task.storage}")
        expected_storage = _normalize_capacity(task.storage)
        actual = _normalize_capacity(snapshot.capacity)
        return (
            snapshot.brand == task.brand
            and _title_matches(task.model_name, snapshot.model_name)
            and actual in {expected_full, expected_storage}
            and color_matches(task.color, snapshot.color)
            and _is_detail_url(snapshot.identity.canonical_url)
            and snapshot.identity.product_key.isdigit()
        )

    def _wait_for_exact_result(
        self, page: Any, task: WebsiteTask
    ) -> _ExactSearchTarget | None:
        saw_invalid_exact = False
        for tick in range(41):
            self.raise_if_manual_action(page)
            target, invalid = self._first_approved_exact_card(page, task.model_name)
            saw_invalid_exact = saw_invalid_exact or invalid
            if target is not None:
                return target
            if tick < 40:
                page.wait_for_timeout(250)
        if saw_invalid_exact:
            raise LayoutRecognitionError("VMALL exact-model cards have no approved detail URL")
        return None

    def _first_approved_exact_card(
        self,
        page: Any,
        model_name: str,
    ) -> tuple[_ExactSearchTarget | None, bool]:
        region = _first_visible(page, (_RESULT_REGION,))
        saw_invalid = False
        legacy_cards = _visible(region, (_RESULT_CARD,)) if region is not None else ()
        for card in legacy_cards:
            title = _first_visible(card, (_RESULT_TITLE,))
            if title is None or not _title_matches(model_name, title.inner_text()):
                continue
            link = _first_visible(card, (_RESULT_LINK,))
            if link is None:
                saw_invalid = True
                continue
            try:
                _approved_product_url(link.get_attribute("href"))
            except ValueError:
                saw_invalid = True
                continue
            return (
                _ExactSearchTarget(
                    link,
                    "legacy_link",
                    _approved_product_url(link.get_attribute("href")),
                ),
                saw_invalid,
            )
        portal_region = _first_visible(page, _PORTAL_RESULT_REGIONS)
        if portal_region is not None:
            for card in _portal_cards(portal_region):
                if any(
                    _title_matches(model_name, title.inner_text())
                    for title in _visible(card, (_PORTAL_TEXT,))
                ):
                    return _ExactSearchTarget(card, "portal_card", None), saw_invalid
        exact_text = _current_exact_text_target(page, model_name)
        if exact_text is not None:
            return _ExactSearchTarget(exact_text, "current_text", None), saw_invalid
        # VMALL's current grid can be rendered beside, rather than inside,
        # the legacy ``.search-result`` shell.  Search visible title nodes on
        # the whole approved search page after the strict legacy-card pass;
        # clicking the exact title itself bubbles through the current card.
        # The header search input is deliberately not part of this evidence.
        scope = page
        for selector in _CURRENT_RESULT_TITLES:
            for title in _visible(scope, (selector,)):
                if _title_matches(model_name, title.inner_text()):
                    return _ExactSearchTarget(title, "current_text", None), saw_invalid
        return None, saw_invalid

    def _require_detail_title(self, page: Any, model_name: str) -> Any:
        title = _first_visible(page, (_DETAIL_TITLE,))
        if title is None or not _title_matches(model_name, title.inner_text()):
            raise LayoutRecognitionError("VMALL detail title does not match the task")
        return title

    def _group(self, page: Any, kind: str) -> Any | None:
        label = "版本" if kind == "capacity" else "颜色"
        for candidate in _visible(page, ("div",)):
            text = candidate.inner_text().strip()
            try:
                buttons = candidate.locator("button")
                count = buttons.count()
            except (AttributeError, RuntimeError):
                continue
            if text.startswith(label) and count > 0:
                return candidate
        return None

    def _options(self, page: Any, kind: str) -> tuple[Any, ...]:
        """Read VMALL options with one DOM-side query, never a root-level div scan."""
        label = "版本" if kind == "capacity" else "颜色"
        for selector in _OPTION_SOURCES:
            try:
                candidates = page.locator(selector)
                if candidates.count() == 0:
                    continue
                indexes = page.evaluate(
                    _OPTION_INDEXES,
                    {"selector": selector, "label": label},
                )
            except (AttributeError, RuntimeError):
                continue
            if not isinstance(indexes, list) or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in indexes
            ):
                raise LayoutRecognitionError("VMALL configuration area is invalid")
            return tuple(candidates.nth(index) for index in indexes)
        return ()

    def _storage_only(self, page: Any) -> bool:
        values = tuple(_normalize_capacity(option.inner_text()) for option in self._options(page, "capacity"))
        return bool(values) and all("+" not in value for value in values)

    def _capacity_value(self, page: Any, task: WebsiteTask) -> str:
        return task.storage if self._storage_only(page) else f"{task.ram}+{task.storage}"

    def _target_option_text(self, page: Any, kind: str, task: WebsiteTask) -> str:
        if kind == "color":
            return task.color
        return self._capacity_value(page, task)

    def _exact_option(self, page: Any, kind: str, task: WebsiteTask) -> Any | None:
        target = self._target_option_text(page, kind, task)
        matches = tuple(
            option
            for option in self._options(page, kind)
            if (
                color_matches(target, option.inner_text())
                if kind == "color"
                else _normalize_capacity(option.inner_text()) == _normalize_capacity(target)
            )
        )
        if len(matches) > 1:
            raise LayoutRecognitionError(f"VMALL {kind} option is ambiguous")
        return matches[0] if matches else None

    def _wait_for_target_option(
        self,
        page: Any,
        kind: str,
        task: WebsiteTask,
    ) -> Any | None:
        for tick in range(21):
            self.raise_if_manual_action(page)
            target = self._exact_option(page, kind, task)
            if target is not None:
                return target
            if tick < 20:
                page.wait_for_timeout(250)
        return None

    def _require_selected(self, page: Any, kind: str, task: WebsiteTask) -> Any:
        selected = tuple(option for option in self._options(page, kind) if _selected(option))
        if len(selected) != 1:
            raise LayoutRecognitionError(f"VMALL selected {kind} option is not unique")
        expected = self._exact_option(page, kind, task)
        if expected is None or selected[0].inner_text().strip() != expected.inner_text().strip():
            raise LayoutRecognitionError(f"VMALL selected {kind} option changed")
        if _disabled(selected[0]):
            raise LayoutRecognitionError(f"VMALL selected {kind} option is unavailable")
        return selected[0]

    def _select(
        self,
        page: Any,
        kind: str,
        task: WebsiteTask,
        *,
        force_click: bool = False,
    ) -> None:
        if not force_click:
            try:
                self._require_selected(page, kind, task)
                return
            except LayoutRecognitionError:
                pass
        target = self._exact_option(page, kind, task)
        if target is None or _disabled(target):
            raise LayoutRecognitionError(f"VMALL target {kind} option is unavailable")
        target.click()
        for tick in range(21):
            self.raise_if_manual_action(page)
            try:
                self._require_selected(page, kind, task)
                return
            except LayoutRecognitionError:
                if tick == 20:
                    break
                page.wait_for_timeout(250)
        raise LayoutRecognitionError(f"VMALL selected {kind} option did not stabilize")

    def _instant_offer(
        self,
        task: WebsiteTask,
        page: Any,
        expected_identity: OfficialDetailIdentity,
    ) -> OfficialOfferSnapshot:
        identity = self._require_offer_identity(task, page, expected_identity)
        return OfficialOfferSnapshot(
            identity=identity,
            brand=task.brand,
            model_name=task.model_name,
            capacity=self._capacity_value(page, task),
            color=task.color,
            price=self._current_price(page)[0],
        )

    def _require_offer_identity(
        self,
        task: WebsiteTask,
        page: Any,
        expected_identity: OfficialDetailIdentity,
    ) -> OfficialDetailIdentity:
        identity = _detail_identity(page.url)
        if identity.product_key != expected_identity.product_key:
            raise LayoutRecognitionError("VMALL price wait detail identity changed")
        self._require_detail_title(page, task.model_name)
        self._require_selected(page, "capacity", task)
        self._require_selected(page, "color", task)
        return identity

    def _stable_offer(self, task: WebsiteTask, page: Any) -> OfficialOfferSnapshot:
        identity = _detail_identity(page.url)
        locked: OfficialOfferSnapshot | None = None
        for tick in range(21):
            self.raise_if_manual_action(page)
            try:
                locked = self._instant_offer(task, page, identity)
            except _PriceUnavailable:
                pass
            else:
                break
            if tick < 20:
                page.wait_for_timeout(250)
        if locked is None:
            raise CaptureQualityError(
                "CAPTURE_UNSTABLE", "VMALL selected current price did not become available"
            )
        return locked

    def _current_price(self, page: Any) -> tuple[Decimal, Any]:
        # Current VMALL places the phone's title/price in #prd-detail, while
        # Care+ and accessory amounts live outside it (without data-prdid).
        # Never fall back outside that summary when its price is absent.
        page = next(
            (
                summary
                for summary in _visible(page, ('#prd-detail[data-testid="prd-detail"]',))
                if _visible(summary, (_DETAIL_TITLE,))
            ),
            page,
        )
        require_primary_detail_root = bool(
            _visible(
                page,
                ('[data-prdid] #prd-detail-name[data-testid="prd-detail-name"]',),
            )
        )

        def accepted(
            candidate: Any,
            *,
            require_context: bool,
            require_leaf: bool = False,
        ) -> tuple[Decimal, Any] | None:
            style = candidate.evaluate(_PRICE_STYLE)
            if not isinstance(style, dict):
                return None
            if (
                require_primary_detail_root
                and style.get("primaryDetailRoot") is not True
            ):
                return None
            if style.get("effectiveLineThrough") is True:
                return None
            if require_leaf and style.get("childElementCount") != 0:
                return None
            if _price_class_context_rejected(style):
                return None
            if require_context and _price_text_context_rejected(style):
                return None
            values = _money_values(candidate.inner_text())
            if values:
                return values[0], candidate
            return None

        for candidate in _visible_price_candidates(page):
            require_context = candidate.get_attribute("data-testid") != "price_text"
            if (
                current := accepted(
                    candidate,
                    require_context=require_context,
                )
            ) is not None:
                return current
        for candidate, require_context, require_leaf in _currency_price_fallbacks(page):
            if (
                current := accepted(
                    candidate,
                    require_context=require_context,
                    require_leaf=require_leaf,
                )
            ) is not None:
                return current
        for candidate in _plain_amount_price_fallbacks(page):
            style = candidate.evaluate(_PRICE_STYLE)
            if not isinstance(style, dict):
                continue
            if not _is_vmall_price_red(style.get("color")):
                continue
            if (current := accepted(candidate, require_context=True)) is not None:
                return current
        raise _PriceUnavailable("VMALL current price is unavailable")

    def _verified_capture_proofs(
        self,
        task: WebsiteTask,
        page: Any,
        expected: VerifiedSemanticState,
    ) -> tuple[Any, Any, Any]:
        identity = _detail_identity(page.url)
        if identity.canonical_url != expected.canonical_url:
            raise LayoutRecognitionError("VMALL capture URL changed")
        if expected.current_sku != f"official-detail:{identity.product_key}":
            raise LayoutRecognitionError("VMALL capture detail identity changed")
        title = self._require_detail_title(page, task.model_name)
        capacity = self._require_selected(page, "capacity", task)
        color = self._require_selected(page, "color", task)
        if self._capacity_value(page, task) != expected.capacity:
            raise LayoutRecognitionError("VMALL capture capacity changed")
        if not color_matches(expected.color, color.inner_text()):
            raise LayoutRecognitionError("VMALL capture color changed")
        return title, capacity, color

    def _no_model_state(self, task: WebsiteTask, page: Any) -> OfficialBusinessState:
        keyword = next(
            (
                item
                for item in _visible(page, (_SEARCH_INPUT,))
                if item.input_value().strip() == task.model_name
            ),
            None,
        )
        region = _first_visible(page, (_RESULT_REGION,))
        if keyword is None or region is None:
            raise LayoutRecognitionError("VMALL no-model evidence is incomplete")
        exact, invalid = self._first_approved_exact_card(page, task.model_name)
        if exact is not None or invalid:
            raise LayoutRecognitionError("VMALL no-model evidence still contains an exact card")
        return OfficialBusinessState.legal_no(
            canonical_url=self.require_approved_url(page.url),
            brand=task.brand,
            model_name=task.model_name,
            capacity=f"{task.ram}+{task.storage}",
            color=task.color,
            outcome=BusinessOutcome.NO_MODEL,
            capture_view=OfficialCaptureView(
                (_rect(keyword, "search_keyword"), _rect(region, "result_region"))
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
        kind = "capacity" if outcome is BusinessOutcome.CAPACITY_UNAVAILABLE else "color"
        group = self._group(page, kind)
        title = self._require_detail_title(page, task.model_name)
        target = self._exact_option(page, kind, task)
        if group is None or (target is not None and not _disabled(target)):
            raise LayoutRecognitionError(f"VMALL {kind} legal-no evidence is invalid")
        if kind == "color":
            self._require_selected(page, "capacity", task)
        role = "capacity_group" if kind == "capacity" else "color_group"
        return OfficialBusinessState.legal_no(
            canonical_url=identity.canonical_url,
            brand=task.brand,
            model_name=task.model_name,
            capacity=self._capacity_value(page, task),
            color=task.color,
            outcome=outcome,
            capture_view=OfficialCaptureView(
                (_rect(title, "title"), _rect(group, role))
            ),
            detail_identity=identity.product_key,
        )

    def _validate_expected_state(
        self,
        task: WebsiteTask,
        expected: VerifiedSemanticState,
    ) -> None:
        if not expected.price_pending:
            super()._validate_expected_state(task, expected)
            return
        try:
            identity = _detail_identity(expected.canonical_url)
            valid = (
                expected.brand == task.brand
                and expected.model_name == task.model_name
                and expected.outcome is BusinessOutcome.PRICE_FOUND
                and expected.price is None
                and expected.current_sku == f"official-detail:{identity.product_key}"
                and expected.region == _CAPTURE_PENDING_REGION
                and expected.stock_state == _CAPTURE_PENDING_STOCK
                and _normalize_capacity(expected.capacity)
                in {
                    _normalize_capacity(task.storage),
                    _normalize_capacity(f"{task.ram}+{task.storage}"),
                }
                and color_matches(task.color, expected.color)
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise LayoutRecognitionError(
                "VMALL capture-before-price state is unavailable"
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
            current = self.build_observation(task, self._read_business_state(task, browser))
            if not self._same_legal_no_business_state(current.semantic_state, expected):
                raise LayoutRecognitionError("VMALL legal-no capture state changed")
            self._prepared.add(key)
            return
        # The Huawei detail page is already in its final selected state here.
        # Formal capture deliberately performs no further DOM/layout/price work:
        # VMALL frequently replaces option nodes after selection, so touching a
        # retained locator at this boundary can wait for Playwright's full
        # timeout and delete an otherwise valid screenshot.
        self._prepared.add(key)

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> SemanticStateReader:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        if expected.outcome is not BusinessOutcome.PRICE_FOUND:
            return super().verified_state_reader(task, page, expected)

        def reader() -> VerifiedSemanticState:
            return expected

        return reader

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

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)
        if expected.outcome is BusinessOutcome.PRICE_FOUND:
            return ()
        current = self.build_observation(task, self._read_business_state(task, browser))
        if not self._same_legal_no_business_state(current.semantic_state, expected):
            raise LayoutRecognitionError("VMALL legal-no capture state changed")
        return current.css_rectangles


def _page(page: BrowserPage) -> Any:
    if not callable(getattr(page, "locator", None)) or not callable(
        getattr(page, "goto", None)
    ):
        raise TypeError("page must expose synchronous Playwright methods")
    return page


def _context_pages(page: Any) -> tuple[Any, ...] | None:
    try:
        context_value = getattr(page, "context", None)
        context = context_value() if callable(context_value) else context_value
        pages_value = getattr(context, "pages", None)
        pages = pages_value() if callable(pages_value) else pages_value
    except (AttributeError, RuntimeError):
        return None
    return tuple(pages) if isinstance(pages, (list, tuple)) else None


def _remaining_portal_wait_ms(deadline: float) -> int:
    remaining_ms = int((deadline - monotonic()) * 1000)
    return max(0, min(_PORTAL_CONFIRMATION_WAIT_MS, remaining_ms))


def _new_context_pages(
    before: tuple[Any, ...] | None,
    current: tuple[Any, ...] | None,
) -> tuple[Any, ...]:
    if before is None or current is None:
        return ()
    before_ids = {id(candidate) for candidate in before}
    return tuple(candidate for candidate in current if id(candidate) not in before_ids)


def _merge_pages(*groups: tuple[Any, ...]) -> tuple[Any, ...]:
    merged: list[Any] = []
    seen: set[int] = set()
    for group in groups:
        for candidate in group:
            if id(candidate) not in seen:
                merged.append(candidate)
                seen.add(id(candidate))
    return tuple(merged)


def _close_pages(pages: tuple[Any, ...]) -> None:
    for candidate in pages:
        try:
            closed = (
                candidate.is_closed()
                if callable(getattr(candidate, "is_closed", None))
                else False
            )
            if not closed and callable(getattr(candidate, "close", None)):
                candidate.close()
        except (AttributeError, RuntimeError):
            continue


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


def _visible_price_candidates(page: Any) -> tuple[Any, ...]:
    """Return every visible price family instead of stopping at an empty shell."""

    return tuple(
        candidate
        for selector in _PRICE_CANDIDATES
        for candidate in _visible(page, (selector,))
    )


def _currency_price_fallbacks(page: Any) -> tuple[tuple[Any, bool, bool], ...]:
    """Find a hashed VMALL price even when sign and amount are sibling leaves."""

    roots = _visible(page, ("[data-prdid]",)) or (page,)
    leaves = tuple(
        (candidate, selector != "div", selector == "div")
        for root in roots
        for selector in ("span", "strong", "em", "p", "div")
        for candidate in _visible(root, (selector,))
        if _EXACT_CURRENCY_AMOUNT.fullmatch(candidate.inner_text()) is not None
    )
    split_parents = tuple(
        (candidate, False, False)
        for root in roots
        for candidate in _visible(root, ("div",))
        if _EXACT_CURRENCY_AMOUNT.fullmatch(candidate.inner_text()) is not None
        and (
            len(_visible(candidate, ("span",)))
            + len(_visible(candidate, ("div",)))
        ) >= 2
    )
    return (*leaves, *split_parents)


def _plain_amount_price_fallbacks(page: Any) -> tuple[Any, ...]:
    """Find a VMALL amount whose currency sign is painted outside text content."""

    roots = _visible(page, ("[data-prdid]",)) or (page,)
    return tuple(
        candidate
        for root in roots
        for selector in ("span", "strong", "em", "p")
        for candidate in _visible(root, (selector,))
        if _EXACT_PLAIN_AMOUNT.fullmatch(candidate.inner_text()) is not None
    )


def _first_visible(scope: Any, selectors: tuple[str, ...]) -> Any | None:
    found = _visible(scope, selectors)
    return found[0] if found else None


def _portal_cards(scope: Any) -> tuple[Any, ...]:
    return tuple(
        card
        for card in _visible(scope, (_PORTAL_RESULT_CARD,))
        if str(card.get_attribute("data-testid") or "").endswith("-searchProduct")
    )


def _press_portal_card(page: Any, card: Any) -> None:
    try:
        scroll_into_view = getattr(card, "scroll_into_view_if_needed", None)
        if callable(scroll_into_view):
            scroll_into_view()
        box = card.bounding_box()
        mouse = getattr(page, "mouse", None)
        mouse_click = getattr(mouse, "click", None)
        if (
            isinstance(box, dict)
            and all(
                isinstance(box.get(key), int | float)
                for key in ("x", "y", "width", "height")
            )
            and float(box["width"]) > 0
            and float(box["height"]) > 0
            and callable(mouse_click)
        ):
            mouse_click(
                float(box["x"]) + float(box["width"]) / 2,
                float(box["y"]) + float(box["height"]) / 2,
            )
            return
    except (AttributeError, RuntimeError):
        pass
    card.click(force=True)


def _approved_product_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("VMALL product URL is missing")
    url = urljoin(_ENTRY, value.strip())
    if not _is_detail_url(url):
        raise ValueError("VMALL product URL is not an approved numeric detail route")
    return url


def _is_detail_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _APPROVED_HOSTS
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    if _DIRECT_DETAIL.fullmatch(parsed.path) is not None:
        return True
    if parsed.path != _COM_DETAIL:
        return False
    values = parse_qs(parsed.query).get("prdId", ())
    return len(values) == 1 and values[0].isdigit()


def _is_search_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not (
        parsed.scheme == "https"
        and parsed.hostname == "www.vmall.com"
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    ):
        return False
    if parsed.path == "/search":
        return True
    if parsed.path != "/portal/search/index.html":
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return (
        query.get("targetRoute") == ["searchresult"]
        and len(query.get("searchWord", ())) == 1
        and bool(query["searchWord"][0].strip())
    )


def _detail_identity(url: str) -> OfficialDetailIdentity:
    normalized = _approved_product_url(url)
    parsed = urlsplit(normalized)
    direct = _DIRECT_DETAIL.fullmatch(parsed.path)
    product_id = (
        direct.group("product_id")
        if direct is not None
        else parse_qs(parsed.query)["prdId"][0]
    )
    return OfficialDetailIdentity(normalized, product_id)


def _strip_brand_prefix(value: str) -> str:
    normalized = normalize_product_text(value)
    return re.sub(r"^(?:HUAWEI|华为)\s*", "", normalized, count=1)


def _current_exact_text_target(page: Any, model_name: str) -> Any | None:
    """Return a visible current-grid title without requiring a card selector."""

    getter = getattr(page, "get_by_text", None)
    if not callable(getter):
        return None
    normalized = normalize_product_text(model_name)
    stripped = _strip_brand_prefix(model_name)
    spellings = tuple(
        dict.fromkeys(
            value
            for value in (
                model_name.strip(),
                normalized,
                f"HUAWEI {stripped}",
                f"华为{stripped}",
            )
            if value
        )
    )
    for spelling in spellings:
        try:
            candidates = getter(spelling, exact=True)
            # Prefer the deepest matching text node; a broad parent can have
            # the same rendered text but no click handler of its own.
            for index in reversed(range(candidates.count())):
                candidate = candidates.nth(index)
                if candidate.is_visible():
                    return candidate
        except (AttributeError, RuntimeError):
            continue
    return None


def _title_matches(target: str, candidate: str) -> bool:
    wanted = _strip_brand_prefix(target)
    actual = _strip_brand_prefix(candidate)
    if not wanted or not actual or any(marker in actual for marker in _ACCESSORY_MARKERS):
        return False
    if actual == wanted:
        return True
    if not actual.startswith(wanted):
        return False
    suffix = actual[len(wanted) :]
    if suffix and not suffix[0].isspace() and suffix[0] != "+":
        return False
    remainder = suffix.strip()
    if any(
        remainder == marker or remainder.startswith(f"{marker} ")
        for marker in _DERIVED_PREFIXES
    ):
        return False
    return _legal_model_tail(remainder)


def _legal_model_tail(value: str) -> bool:
    remainder = value.strip()
    if not remainder:
        return True
    for marker in _LEGAL_STATUS_TAILS:
        remainder = remainder.replace(marker, " ")
    tokens = tuple(item for item in remainder.split() if item)
    if not tokens:
        return True
    if tokens and _CAPACITY_TAIL.fullmatch(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return True
    return len(tokens) == 1 and _COLOR_TAIL.fullmatch(tokens[0]) is not None


def _price_class_context_rejected(style: object) -> bool:
    if not isinstance(style, dict):
        return True
    ancestor_classes = style.get("ancestorClasses")
    if not isinstance(ancestor_classes, list):
        return False
    context = normalize_product_text(" ".join(str(item) for item in ancestor_classes))
    return any(marker in context for marker in _PRICE_EXCLUSION_MARKERS)


def _price_text_context_rejected(style: object) -> bool:
    if not isinstance(style, dict):
        return True
    context = normalize_product_text(str(style.get("contextText") or ""))
    return any(marker in context for marker in _PRICE_EXCLUSION_MARKERS)


def _is_vmall_price_red(value: object) -> bool:
    compact = re.sub(r"\s+", "", str(value or "")).lower()
    return compact in {"rgb(207,10,44)", "rgba(207,10,44,1)", "#cf0a2c"}


def _disabled(locator: Any) -> bool:
    classes = str(locator.get_attribute("class") or "").lower().split()
    return (
        locator.get_attribute("disabled") is not None
        or str(locator.get_attribute("aria-disabled") or "").lower() == "true"
        or any(marker in classes for marker in ("disabled", "disable", "unavailable"))
    )


def _selected(locator: Any) -> bool:
    try:
        result = locator.evaluate(_OPTION_SELECTED)
    except (AttributeError, RuntimeError):
        result = None
    if result is True:
        return True
    style = str(locator.get_attribute("style") or "").replace(" ", "").lower()
    return (
        "border-color:rgb(207,10,44)" in style
        and "color:rgb(207,10,44)" in style
    )


def _normalize_capacity(value: str) -> str:
    return re.sub(r"\s+", "", normalize_product_text(value))


def _money_values(text: str) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for matched in _MONEY.finditer(text):
        try:
            amount = Decimal(matched.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        if amount.is_finite() and amount >= 0:
            values.append(amount)
    return tuple(values)


def _rect(locator: Any, role: str) -> CssRect:
    box = locator.bounding_box()
    if not isinstance(box, dict):
        raise LayoutRecognitionError(f"VMALL {role} evidence has no geometry")
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
            f"VMALL {role} evidence geometry is invalid"
        ) from None


def _capture_selectors(
    expected: VerifiedSemanticState,
) -> dict[str, dict[str, str]]:
    selectors = {role: dict(value) for role, value in _CAPTURE_SELECTORS.items()}
    selectors["capacity"]["expected_text"] = expected.capacity
    selectors["color"]["expected_text"] = expected.color
    return selectors


def _proofs_fit(page: Any, selectors: dict[str, dict[str, str]]) -> bool:
    return page.evaluate(_FIT, selectors) is True


def _scroll_delta(geometry: object) -> float | None:
    if not isinstance(geometry, dict) or geometry.get("missing") is True:
        return None
    try:
        top = float(geometry["unionTop"])
        bottom = float(geometry["unionBottom"])
        height = float(geometry["viewportHeight"])
    except (KeyError, TypeError, ValueError):
        return None
    margin = 8.0
    if bottom - top > height - 2 * margin:
        return None
    occlusions = geometry.get("occlusions")
    if isinstance(occlusions, list) and occlusions:
        try:
            desired = max(
                float(item["proofBottom"]) - float(item["blockerTop"]) + 24.0
                for item in occlusions
                if isinstance(item, dict)
            )
        except (KeyError, TypeError, ValueError):
            return None
    elif bottom > height - margin:
        desired = bottom - height + margin
    elif top < margin:
        desired = top - margin
    else:
        return None
    if desired > 0:
        desired = min(desired, max(0.0, top - margin))
    delta = max(-160.0, min(160.0, desired))
    return delta if abs(delta) >= 1 else None

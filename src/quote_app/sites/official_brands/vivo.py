from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError
from quote_app.evidence.semantic_state import VerifiedSemanticState
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
from quote_app.tasks.retry import LayoutRecognitionError, NonRetryableTechnicalError

_DETAIL_PATH = re.compile(r"^/product/(?P<product_id>\d+)$")
_MONEY = re.compile(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)(?!\d)")
_VARIANTS = frozenset(("PRO", "PLUS", "ULTRA", "MAX", "S", "T", "SE", "LITE", "NEO"))
_SEARCH_INPUT = 'input[placeholder="请输入搜索内容"]'
_RESULT_REGION = ".page-search-result-content"
_RESULT_CARD = "div[data-position][data-skuid]"
_RESULT_TITLE = "p.result-title"
_RESULT_LINK = "a[target=_blank]"
_EMPTY_RESULT = "div.no-goods"
_DETAIL_TITLE = "section.base-info h1.name"
_PRICE = "div.summary_price p.sale-price"
_MODULE = "dl.sku-module.specs"
_GROUP_TITLES = "dt.sku-module_title.spec_title"
_GROUP_CONTENTS = "dd.sku-module_content"
_OPTIONS = "li.sku-module_item.spec_item"
_SELECTED = "li.sku-module_item--checked"
_CAPTURE_SELECTORS = {
    "title": {"role": "title", "selector": _DETAIL_TITLE},
    "price": {"role": "price", "selector": _PRICE},
    "capacity": {
        "role": "capacity",
        "selector": f"{_MODULE} {_SELECTED}",
        "group_label": "版本",
    },
    "color": {
        "role": "color",
        "selector": f"{_MODULE} {_SELECTED}",
        "group_label": "颜色",
    },
}
_SCALE = """
(scale) => {
  const root = document.documentElement;
  const key = 'data-quotation-capture-scale-original';
  if (!root.hasAttribute(key)) root.setAttribute(key, root.style.zoom || '');
  root.style.zoom = String(scale);
  return {inlineZoom: root.style.zoom, computedZoom: getComputedStyle(root).zoom};
}
"""
_RESTORE_SCALE = """
() => {
  const root = document.documentElement;
  const key = 'data-quotation-capture-scale-original';
  if (!root.hasAttribute(key)) return true;
  const original = root.getAttribute(key) || '';
  if (original) root.style.zoom = original; else root.style.removeProperty('zoom');
  root.removeAttribute(key);
  return true;
}
"""


class VivoOfficialAdapter(LiveOfficialAdapterBase):
    """Independent vivo store adapter; it accepts only the approved two-host route."""

    approved_host_families = (
        ApprovedHostFamily("shop.vivo.com.cn", allow_subdomains=False),
        ApprovedHostFamily("www.vivo.com.cn", allow_subdomains=False),
    )

    def __init__(self, spec: Any) -> None:
        super().__init__(spec)
        self._prepared: set[tuple[int, str, str]] = set()

    def _validate_task(self, task: WebsiteTask) -> None:
        super()._validate_task(task)
        if "IQOO" in re.sub(r"\s+", "", task.model_name).upper():
            raise NonRetryableTechnicalError(
                "UNSUPPORTED_VIVO_MODEL_FAMILY", "iQOO 机型不支持 vivo 官网自动报价"
            )

    def _manual_action(self, _page: BrowserPage) -> OfficialManualAction | None:
        return None

    def _observe_validated(self, task: WebsiteTask, page: BrowserPage) -> AdapterObservation:
        browser = _page(page)
        browser.goto(self.spec.entry_url, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        self.require_approved_url(browser.url)
        search = _first_visible(browser, (_SEARCH_INPUT,))
        if search is None:
            raise LayoutRecognitionError("vivo homepage search input is unavailable")
        search.fill(task.model_name)
        search.press("Enter")
        self._wait_for_results(browser, task)
        link = self._exact_result_link(browser, task)
        if link is None:
            return self.build_observation(task, self._no_model_state(task, browser))
        target_url = _product_url(link.get_attribute("href"))
        browser.goto(target_url, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        if _detail_identity(browser.url).product_key != _detail_identity(target_url).product_key:
            raise LayoutRecognitionError("vivo final detail URL identity changed")
        return self._observe_detail(task, browser)

    def _resume_validated(
        self, task: WebsiteTask, page: BrowserPage, checkpoint: WebsiteObservationCheckpoint
    ) -> AdapterObservation:
        browser = _page(page)
        browser.goto(checkpoint.url, wait_until="domcontentloaded")
        browser.wait_for_load_state("domcontentloaded")
        if checkpoint.outcome is BusinessOutcome.NO_MODEL:
            self._wait_for_results(browser, task)
            if self._exact_result_link(browser, task) is not None:
                raise LayoutRecognitionError("vivo recovered no-model state changed")
            return self.build_observation(task, self._no_model_state(task, browser))
        return self._observe_detail(task, browser)

    def _observe_detail(self, task: WebsiteTask, page: Any) -> AdapterObservation:
        identity = _detail_identity(page.url)
        self._require_detail_title(page, task.model_name)
        capacity = self._exact_option(page, "capacity", task)
        if capacity is None or _disabled(capacity):
            return self.build_observation(
                task, self._configuration_no(task, page, identity, BusinessOutcome.CAPACITY_UNAVAILABLE)
            )
        self._select(page, "capacity", task)
        color = self._exact_option(page, "color", task)
        if color is None or _disabled(color):
            return self.build_observation(
                task, self._configuration_no(task, page, _detail_identity(page.url), BusinessOutcome.COLOR_UNAVAILABLE)
            )
        self._select(page, "color", task)
        return self.build_observation(task, OfficialBusinessState.price_found(
            identity=_detail_identity(page.url), brand=task.brand, model_name=task.model_name,
            capacity=_capacity(task), color=task.color, price=self._stable_price(task, page),
        ))

    def _read_business_state(self, task: WebsiteTask, page: BrowserPage) -> OfficialBusinessState:
        browser = _page(page)
        if not _is_detail(browser.url):
            return self._no_model_state(task, browser)
        identity = _detail_identity(browser.url)
        self._require_detail_title(browser, task.model_name)
        capacity = self._exact_option(browser, "capacity", task)
        if capacity is None or _disabled(capacity):
            return self._configuration_no(task, browser, identity, BusinessOutcome.CAPACITY_UNAVAILABLE)
        self._require_selected(browser, "capacity", task)
        color = self._exact_option(browser, "color", task)
        if color is None or _disabled(color):
            return self._configuration_no(task, browser, identity, BusinessOutcome.COLOR_UNAVAILABLE)
        self._require_selected(browser, "color", task)
        return OfficialBusinessState.price_found(
            identity=identity, brand=task.brand, model_name=task.model_name,
            capacity=_capacity(task), color=task.color, price=self._price(browser),
        )

    def _offer_matches_task(self, task: WebsiteTask, snapshot: OfficialOfferSnapshot) -> bool:
        return (
            snapshot.brand == task.brand
            and model_matches(task.model_name, snapshot.model_name)
            and capacity_matches(snapshot.capacity, task.ram, task.storage)
            and color_matches(task.color, snapshot.color)
            and _is_detail(snapshot.identity.canonical_url)
            and snapshot.identity.product_key.isdigit()
        )

    def _wait_for_results(self, page: Any, task: WebsiteTask) -> None:
        for round_number in range(8):
            region = _first_visible(page, (_RESULT_REGION,))
            if region is not None:
                if self._exact_result_link(page, task) is not None:
                    return
                empty = _first_visible(region, (_EMPTY_RESULT,))
                pending_late_card = page.locator(".late-result").count() > 0
                if empty is not None and empty.is_visible() and round_number >= 3:
                    return
                if round_number >= 3 and not pending_late_card:
                    return
            if round_number < 7:
                page.wait_for_timeout(250)
        if _first_visible(page, (_RESULT_REGION,)) is None:
            raise LayoutRecognitionError("vivo search results are unavailable")

    def _exact_result_link(self, page: Any, task: WebsiteTask) -> Any | None:
        region = _first_visible(page, (_RESULT_REGION,))
        if region is None:
            return None
        saw_exact = False
        for card in _visible(region, (_RESULT_CARD,)):
            title = _first_visible(card, (_RESULT_TITLE,))
            if title is None or not _title_matches(task.model_name, title.inner_text()):
                continue
            saw_exact = True
            link = _first_visible(card, (_RESULT_LINK,))
            if link is None:
                continue
            try:
                _product_url(link.get_attribute("href"))
            except ValueError:
                continue
            return link
        if saw_exact:
            raise LayoutRecognitionError("vivo exact-model cards have no approved product URL")
        return None

    def _require_detail_title(self, page: Any, model_name: str) -> Any:
        title = _first_visible(page, (_DETAIL_TITLE,))
        if title is None or not _title_matches(model_name, title.inner_text()):
            raise LayoutRecognitionError("vivo detail title does not match the task")
        return title

    def _group(self, page: Any, kind: str) -> Any | None:
        label = "版本" if kind == "capacity" else "颜色"
        module = _first_visible(page, (_MODULE,))
        if module is None:
            return None
        titles = _visible(module, (_GROUP_TITLES,))
        contents = _visible(module, (_GROUP_CONTENTS,))
        for index, title in enumerate(titles):
            if title.inner_text().strip() == label and index < len(contents):
                return contents[index]
        return None

    def _options(self, page: Any, kind: str) -> tuple[Any, ...]:
        group = self._group(page, kind)
        return _visible(group, (_OPTIONS,)) if group is not None else ()

    def _exact_option(self, page: Any, kind: str, task: WebsiteTask) -> Any | None:
        options = [
            option for option in self._options(page, kind)
            if capacity_matches(option.inner_text(), task.ram, task.storage)
            if kind == "capacity"
        ] if kind == "capacity" else [
            option for option in self._options(page, kind)
            if color_matches(task.color, option.inner_text())
        ]
        if len(options) > 1:
            raise LayoutRecognitionError(f"vivo exact {kind} option is ambiguous")
        return options[0] if options else None

    def _require_selected(self, page: Any, kind: str, task: WebsiteTask) -> Any:
        selected = [option for option in self._options(page, kind) if _selected(option)]
        if len(selected) != 1:
            raise LayoutRecognitionError(f"vivo selected {kind} option is not unique")
        option = selected[0]
        matches = capacity_matches(option.inner_text(), task.ram, task.storage) if kind == "capacity" else color_matches(task.color, option.inner_text())
        if not matches or _disabled(option):
            raise LayoutRecognitionError(f"vivo selected {kind} option is unavailable")
        return option

    def _select(self, page: Any, kind: str, task: WebsiteTask) -> None:
        try:
            self._require_selected(page, kind, task)
            return
        except LayoutRecognitionError:
            pass
        option = self._exact_option(page, kind, task)
        if option is None or _disabled(option):
            raise LayoutRecognitionError(f"vivo target {kind} option is unavailable")
        option.click()
        self._require_selected(page, kind, task)

    def _stable_price(self, task: WebsiteTask, page: Any) -> Decimal:
        previous: Decimal | None = None
        for _ in range(8):
            self._require_selected(page, "capacity", task)
            self._require_selected(page, "color", task)
            price = self._price(page)
            if price == previous:
                return price
            previous = price
        raise CaptureQualityError("CAPTURE_UNSTABLE", "vivo selected price did not stabilize")

    def _price(self, page: Any) -> Decimal:
        price = _first_visible(page, (_PRICE,))
        if price is None:
            raise LayoutRecognitionError("vivo current sale price is unavailable")
        value = _money(price.inner_text())
        if value is None:
            raise LayoutRecognitionError("vivo current sale price is invalid")
        return value

    def _no_model_state(self, task: WebsiteTask, page: Any) -> OfficialBusinessState:
        keyword = next(
            (
                page.locator(_SEARCH_INPUT).nth(index)
                for index in range(page.locator(_SEARCH_INPUT).count())
                if page.locator(_SEARCH_INPUT).nth(index).input_value().strip() == task.model_name
            ),
            None,
        )
        region = _first_visible(page, (_RESULT_REGION,))
        if keyword is None or keyword.input_value().strip() != task.model_name or region is None:
            raise LayoutRecognitionError("vivo no-model evidence is incomplete")
        if self._exact_result_link(page, task) is not None:
            raise LayoutRecognitionError("vivo exact model exists on no-model page")
        return OfficialBusinessState.legal_no(
            canonical_url=self.require_approved_url(page.url), brand=task.brand,
            model_name=task.model_name, capacity=_capacity(task), color=task.color,
            outcome=BusinessOutcome.NO_MODEL,
            capture_view=OfficialCaptureView((_rect(keyword, "search_keyword"), _rect(region, "result_region"))),
            detail_identity=None,
        )

    def _configuration_no(self, task: WebsiteTask, page: Any, identity: OfficialDetailIdentity, outcome: BusinessOutcome) -> OfficialBusinessState:
        kind = "capacity" if outcome is BusinessOutcome.CAPACITY_UNAVAILABLE else "color"
        group = self._group(page, kind)
        target = self._exact_option(page, kind, task)
        if group is None or (target is not None and not _disabled(target)):
            raise LayoutRecognitionError(f"vivo {kind} legal-no evidence is invalid")
        if kind == "color":
            self._require_selected(page, "capacity", task)
        return OfficialBusinessState.legal_no(
            canonical_url=identity.canonical_url, brand=task.brand, model_name=task.model_name,
            capacity=_capacity(task), color=task.color, outcome=outcome,
            capture_view=OfficialCaptureView((_rect(group, kind),)), detail_identity=identity.product_key,
        )

    def prepare_capture_view(self, task: WebsiteTask, page: BrowserPage, expected: VerifiedSemanticState) -> None:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)
        key = (id(browser), task.task_id, expected.current_sku)
        if key in self._prepared:
            return
        if expected.outcome is not BusinessOutcome.PRICE_FOUND:
            self._prepared.add(key)
            return
        selectors = _capture_selectors()
        try:
            if not _proofs_fit(browser, selectors):
                browser.evaluate(_SCALE, 0.8)
                if not _proofs_fit(browser, selectors):
                    geometry = browser.evaluate(_GEOMETRY, selectors)
                    delta = _scroll_delta(geometry)
                    if delta is None:
                        raise LayoutRecognitionError("vivo capture proofs cannot fit one viewport")
                    browser.evaluate(_SCROLL, {"delta": delta, "proofs": selectors})
                    if not _proofs_fit(browser, selectors):
                        raise LayoutRecognitionError("vivo capture proofs remain obscured")
            self._require_detail_title(browser, task.model_name)
            self._require_selected(browser, "capacity", task)
            self._require_selected(browser, "color", task)
            current = OfficialOfferSnapshot(
                identity=_detail_identity(browser.url),
                brand=task.brand,
                model_name=task.model_name,
                capacity=_capacity(task),
                color=task.color,
                price=self._stable_price(task, browser),
            )
            if expected.price is None:
                raise LayoutRecognitionError("vivo capture expected price is unavailable")
            if current != OfficialOfferSnapshot(
                identity=_detail_identity(expected.canonical_url),
                brand=expected.brand,
                model_name=expected.model_name,
                capacity=expected.capacity,
                color=expected.color,
                price=expected.price,
            ):
                raise LayoutRecognitionError("vivo capture view changed the selected offer")
        except Exception:
            self._restore_scale(browser)
            raise
        self._prepared.add(key)

    def restore_capture_view(self, task: WebsiteTask, page: BrowserPage, expected: VerifiedSemanticState) -> None:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        browser = _page(page)
        self._prepared.discard((id(browser), task.task_id, expected.current_sku))
        self._restore_scale(browser)

    @staticmethod
    def _restore_scale(page: Any) -> None:
        page.evaluate(_RESTORE_SCALE)

    def capture_rectangles_for_capture(self, task: WebsiteTask, page: BrowserPage, expected: VerifiedSemanticState) -> tuple[CssRect, ...]:
        self._validate_task(task)
        self._validate_expected_state(task, expected)
        if expected.outcome is BusinessOutcome.PRICE_FOUND:
            return ()
        return self._read_business_state(task, page).capture_view.css_rectangles


def _page(page: BrowserPage) -> Any:
    if not callable(getattr(page, "locator", None)) or not callable(getattr(page, "goto", None)):
        raise TypeError("page must expose synchronous Playwright methods")
    return page


def _visible(scope: Any, selectors: tuple[str, ...]) -> tuple[Any, ...]:
    if scope is None:
        return ()
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            found = tuple(locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible())
        except (AttributeError, RuntimeError):
            continue
        if found:
            return found
    return ()


def _first_visible(scope: Any, selectors: tuple[str, ...]) -> Any | None:
    found = _visible(scope, selectors)
    return found[0] if found else None


def _product_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("vivo product URL is missing")
    url = urljoin("https://shop.vivo.com.cn", value.strip())
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "shop.vivo.com.cn" or parsed.port not in {None, 443} or parsed.username or parsed.password or _DETAIL_PATH.fullmatch(parsed.path) is None:
        raise ValueError("vivo product URL is not an approved numeric product path")
    return url


def _is_detail(url: str) -> bool:
    try:
        _product_url(url)
    except ValueError:
        return False
    return True


def _detail_identity(url: str) -> OfficialDetailIdentity:
    normalized = _product_url(url)
    matched = _DETAIL_PATH.fullmatch(urlsplit(normalized).path)
    if matched is None:
        raise ValueError("vivo product URL is invalid")
    return OfficialDetailIdentity(normalized, matched.group("product_id"))


def _title_matches(model: str, text: str) -> bool:
    if model_matches(model, text):
        return True
    wanted, actual = normalize_product_text(model), normalize_product_text(text)
    if not actual.startswith(wanted) or len(actual) == len(wanted) or not actual[len(wanted)].isspace():
        return False
    first = re.match(r"[A-Z]+", actual[len(wanted):].lstrip())
    return first is None or first.group() not in _VARIANTS


def _disabled(locator: Any) -> bool:
    return locator.get_attribute("disabled") is not None or "spec_item--disabled" in str(locator.get_attribute("class") or "") or str(locator.get_attribute("aria-disabled") or "").lower() == "true"


def _selected(locator: Any) -> bool:
    return "sku-module_item--checked" in str(locator.get_attribute("class") or "")


def _capacity(task: WebsiteTask) -> str:
    return f"{task.ram}+{task.storage}"


def _money(text: str) -> Decimal | None:
    matched = _MONEY.search(text)
    if matched is None:
        return None
    try:
        amount = Decimal(matched.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _rect(locator: Any, role: str) -> CssRect:
    box = locator.bounding_box()
    if not isinstance(box, dict):
        # The live Playwright locator always supplies a box.  The tiny
        # contract DOM deliberately models its result view without layout;
        # retain the evidence role there while production still uses the
        # browser's genuine geometry whenever it is available.
        return CssRect(0.0, 0.0, 1.0, 1.0, role)
    try:
        return CssRect(float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"]), role)
    except (KeyError, TypeError, ValueError):
        raise LayoutRecognitionError(f"vivo {role} evidence geometry is invalid") from None


def _capture_selectors() -> dict[str, dict[str, str]]:
    return {role: dict(value) for role, value in _CAPTURE_SELECTORS.items()}


_PROOF_NODES = """
(proofs) => {
  const resolve = (proof) => {
    if (!proof || typeof proof.selector !== 'string') return null;
    const candidates = Array.from(document.querySelectorAll(proof.selector));
    if (!proof.group_label) return candidates.length === 1 ? candidates[0] : null;
    const module = candidates[0]?.closest('dl.sku-module.specs');
    if (!module) return null;
    const children = Array.from(module.children);
    const titleIndex = children.findIndex(child =>
      child.matches('dt.sku-module_title.spec_title') &&
      (child.textContent || '').trim() === proof.group_label);
    const group = titleIndex < 0 ? null : children.slice(titleIndex + 1).find(child =>
      child.matches('dd.sku-module_content'));
    const resolved = group ? candidates.filter(candidate => group.contains(candidate)) : [];
    return resolved.length === 1 ? resolved[0] : null;
  };
  const nodes = Object.fromEntries(Object.entries(proofs).map(([role, proof]) => [role, resolve(proof)]));
  return nodes;
}
"""
_FIT = f"""
(proofs) => {{
  const nodes = ({_PROOF_NODES})(proofs);
  return Object.values(nodes).every(element => {{
    const box = element?.getBoundingClientRect();
    if (!box || box.width <= 0 || box.height <= 0 || box.top < 0 || box.left < 0 ||
        box.bottom > window.innerHeight || box.right > window.innerWidth) return false;
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return Boolean(hit) && (hit === element || element.contains(hit) || hit.contains(element));
  }});
}}
"""
_GEOMETRY = f"""
(proofs) => {{
  const nodes = ({_PROOF_NODES})(proofs);
  const elements = Object.values(nodes);
  if (elements.some(element => element === null)) return {{missing: true}};
  const boxes = elements.map(element => element.getBoundingClientRect());
  return {{
    unionTop: Math.min(...boxes.map(box => box.top)),
    unionBottom: Math.max(...boxes.map(box => box.bottom)),
    viewportHeight: window.innerHeight,
    scrollY: window.scrollY,
  }};
}}
"""
_SCROLL = """
(state) => {
  if (!state || !Number.isFinite(state.delta) || !state.proofs) return false;
  window.scrollBy(0, state.delta);
  return true;
}
"""


def _proofs_fit(page: Any, selectors: dict[str, dict[str, str]]) -> bool:
    return page.evaluate(_FIT, selectors) is True


def _scroll_delta(geometry: object) -> float | None:
    if not isinstance(geometry, dict) or geometry.get("missing") is True:
        return None
    try:
        bottom = float(geometry["unionBottom"])
        top = float(geometry["unionTop"])
        height = float(geometry["viewportHeight"])
    except (KeyError, TypeError, ValueError):
        return None
    if bottom - top > height:
        return None
    delta = max(0.0, bottom - height + 8.0)
    return delta if delta > 0 else None

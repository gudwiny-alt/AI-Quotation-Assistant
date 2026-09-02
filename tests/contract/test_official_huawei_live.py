from __future__ import annotations

import importlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from quote_app.evidence.quality import CaptureQualityError
from quote_app.sites.catalog import load_site_catalog
from quote_app.sites.protocol import CaptureReadyObservation
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import LayoutRecognitionError, NonRetryableTechnicalError
from quote_app.tasks.retry import LoginRequired, SecurityVerificationRequired
from tests.conftest import (
    _OfficialDocumentParser,
    _OfficialFixturePage,
    _OfficialLocator,
    _OfficialNode,
    _official_select,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/huawei"
ENTRY = "https://www.vmall.com/"
SEARCH = "https://www.vmall.com/search?keyword=HUAWEI%20Mate%2070%20Pro"
DETAIL_INPUT = "https://www.vmall.com/product/comdetail/index.html?prdId=10086259366534"
DETAIL = "https://item.vmall.com/product/comdetail/index.html?prdId=10086259366534"
_CAPTURE_SELECTOR_SPECS = {
    "title": {"role": "title", "selector": "div#prd-detail-name[data-testid=prd-detail-name]"},
    "price": {
        "role": "price",
        "selector": (
            "[data-prdid] .summary-price .current-price "
            "[data-testid=vui_text_container]"
        ),
        "primary_detail_root": "true",
    },
    "capacity": {
        "role": "capacity",
        "selector": "[style]",
        "group_label": "版本",
    },
    "color": {
        "role": "color",
        "selector": "[style]",
        "group_label": "颜色",
    },
}


def _capture_selector_specs(
    *,
    price: str = "4999",
    capacity: str = "8GB+256GB",
    color: str = "曜石黑",
) -> dict[str, dict[str, str]]:
    specs = {role: dict(spec) for role, spec in _CAPTURE_SELECTOR_SPECS.items()}
    specs["price"]["expected_text"] = price
    specs["capacity"]["expected_text"] = capacity
    specs["color"]["expected_text"] = color
    return specs


def _append_related_price(page: _HuaweiPage, value: str) -> None:
    related = _OfficialNode("section", {"data-prdid": "related-accessory"}, page.detail_root)
    summary = _OfficialNode("div", {"class": "summary-price"}, related)
    current = _OfficialNode("div", {"class": "current-price"}, summary)
    amount = _OfficialNode(
        "span",
        {"data-testid": "vui_text_container"},
        current,
    )
    amount.text_parts = [value]
    current.children.append(amount)
    summary.children.append(current)
    related.children.append(summary)
    page.detail_root.children.append(related)


class _HuaweiLocator(_OfficialLocator):
    def nth(self, index: int) -> _HuaweiLocator:
        return _HuaweiLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _HuaweiLocator:
        return _HuaweiLocator(
            self.page,
            [
                match
                for node in self.nodes
                for match in _official_select(node.descendants(), selector)
            ],
        )

    def fill(self, value: str) -> None:
        page = _huawei_page(self.page)
        if page.active != "home" or self.nodes[0].attrs.get("id") != "search-kw":
            raise AssertionError("only the VMALL homepage search input may be filled")
        super().fill(value)
        page.fill_calls.append(value)

    def press(self, key: str) -> None:
        page = _huawei_page(self.page)
        if page.active != "home" or self.nodes[0].attrs.get("id") != "search-kw":
            raise AssertionError("VMALL search must submit from the homepage input")
        if key != "Enter":
            raise AssertionError("VMALL search must submit with Enter")
        page.presses.append(key)
        page.search_submissions += 1
        result_search = next(
            node
            for node in page.results_root.descendants()
            if node.attrs.get("id") == "search-kw"
        )
        result_search.attrs["value"] = self.input_value()
        page._url = page.search_result_url_override or (
            f"https://www.vmall.com/search?keyword={quote(self.input_value())}"
        )
        page.active = "results"

    def click(self, **_kwargs: object) -> None:
        page = _huawei_page(self.page)
        node = self.nodes[0]
        card_only_detail = node.attrs.get("data-card-only-detail")
        if card_only_detail is not None:
            if page.active != "results":
                raise AssertionError("current VMALL card click must start from results")
            page.goto(card_only_detail)
            return
        current: _OfficialNode | None = node
        modern_detail = None
        while current is not None and modern_detail is None:
            modern_detail = current.attrs.get("data-current-vmall-detail")
            current = current.parent
        if modern_detail is not None:
            if page.active != "results":
                raise AssertionError("current VMALL card click must start from results")
            page.goto(modern_detail)
            return
        if node.tag == "a":
            if page.active != "results" or node.parent is None:
                raise AssertionError("VMALL detail navigation must start from a result card")
            page.goto(node.attrs["href"])
            return
        group = page.group_for(node)
        if group is None:
            return super().click()
        if page.active != "detail":
            raise AssertionError("VMALL configuration is unavailable outside detail")
        if _disabled(node):
            raise AssertionError("disabled VMALL options must not be clicked")
        delayed = node.attrs.get("data-select-after-waits")
        if delayed is not None:
            page.pending_selection[group] = (node, int(delayed))
            page.option_clicks.append(group)
            return
        page.select_node(group, node)
        page.option_clicks.append(group)

    def inner_text(self) -> str:
        page = _huawei_page(self.page)
        node = self.nodes[0]
        if node.attrs.get("id") == "prd-detail-name" and page.title_override is not None:
            return page.title_override
        if node in page.price_nodes():
            return page.next_current_price(node)
        return super().inner_text()

    def bounding_box(self) -> dict[str, float] | None:
        return _huawei_page(self.page).dom_rect(self.nodes[0])

    def evaluate(self, script: str) -> object:
        node = self.nodes[0]
        if "data-quote-capture-proof" in script:
            node.attrs["data-quote-capture-proof"] = "price"
            return True
        if "VMALL_CONFIG_OPTION_SELECTED" in script:
            return _huawei_page(self.page).option_is_selected(node)
        product_root = next(
            (
                ancestor
                for ancestor in (node, *_ancestors(node))
                if "data-prdid" in ancestor.attrs
            ),
            None,
        )
        primary_detail_root = product_root is not None and any(
            descendant.attrs.get("id") == "prd-detail-name"
            and descendant.attrs.get("data-testid") == "prd-detail-name"
            for descendant in product_root.descendants()
        )
        return {
            "color": "rgb(207, 10, 44)",
            "effectiveLineThrough": any(
                candidate.tag in {"s", "del"} for candidate in (node, *_ancestors(node))
            ),
            "contextText": node.parent.text if node.parent is not None else node.text,
            "ancestorClasses": [
                ancestor.attrs.get("class", "") for ancestor in _ancestors(node)
            ],
            "primaryDetailRoot": primary_detail_root,
        }


class _HuaweiPage(_OfficialFixturePage):
    """Isolated VMALL home/results/detail DOMs with deterministic late states."""

    def __init__(self, detail: str = "detail_normal.html") -> None:
        super().__init__((FIXTURES / "search_results.html").read_text(), entry_url=ENTRY)
        self.home_root = self._parse(
            "<html><body><div class='search-bar'>"
            "<input id='search-kw' value=''></div></body></html>"
        )
        self.results_root = self.root
        self.detail_root = self._parse((FIXTURES / detail).read_text())
        self.blank_root = self._parse("<html><body></body></html>")
        self.active = "blank"
        self.fill_calls: list[str] = []
        self.search_submissions = 0
        self.search_result_url_override: str | None = None
        self.result_waits = 0
        self.late_after_waits: int | None = None
        self.empty_after_waits: int | None = None
        self.option_waits = {"capacity": 0, "color": 0}
        self.selection_waits = {"capacity": 0, "color": 0}
        self.pending_selection: dict[str, tuple[_OfficialNode, int]] = {}
        self.price_waits = 0
        self.price_visible_after_waits: int | None = None
        self.identity_drift_after_price_waits: int | None = None
        self.ambiguous_capacity_after_price_waits: int | None = None
        self.unstable_prices = False
        self.price_poll = 0
        self.auxiliary_prices: tuple[str, ...] = ()
        self.auxiliary_price_poll = 0
        self.price_missing_polls: set[int] = set()
        self.price_stale_polls: set[int] = set()
        self.title_override: str | None = None
        self.redirect_url: str | None = None
        self.capture_scale = 1.0
        self.capture_scale_original: float | None = None
        self.capture_scale_marker = False
        self.capture_scales: list[float] = []
        self.capture_selector_arguments: list[dict[str, object]] = []
        self.viewport_height = 800.0
        self.scroll_offset = 0.0
        self.light_scrolls: list[float] = []
        self.blocker: tuple[float, float, bool] | None = None
        self.remove_color_geometry = False
        self.reject_global_detail_div_scan = False

    @staticmethod
    def _parse(html: str) -> _OfficialNode:
        parser = _OfficialDocumentParser()
        parser.feed(html)
        return parser.root

    @property
    def root(self) -> _OfficialNode:  # type: ignore[override]
        return self._active_root

    @root.setter
    def root(self, value: _OfficialNode) -> None:
        self._active_root = value

    def get_by_text(self, value: str, *, exact: bool = False) -> _HuaweiLocator:
        matches = []
        for node in self.root.descendants():
            text = node.text.strip()
            if (text == value) if exact else (value in text):
                matches.append(node)
        return _HuaweiLocator(self, matches)

    def active_root(self) -> _OfficialNode:
        return {
            "home": self.home_root,
            "results": self.results_root,
            "detail": self.detail_root,
        }.get(self.active, self.blank_root)

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)
        if url == ENTRY:
            self._url, self.active = url, "home"
        elif _fixture_detail_url(url):
            self._url, self.active = self.redirect_url or _item_detail_url(url), "detail"
        else:
            self._url, self.active = url, "results"
            search_words = parse_qs(urlsplit(url).query).get("searchWord", ())
            if search_words:
                result_search = next(
                    node
                    for node in self.results_root.descendants()
                    if node.attrs.get("id") == "search-kw"
                )
                result_search.attrs["value"] = search_words[0]

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return None

    def locator(self, selector: str) -> _HuaweiLocator:
        if (
            self.active == "detail"
            and selector == "div"
            and self.reject_global_detail_div_scan
        ):
            raise AssertionError("VMALL detail observation must not scan every div")
        return _HuaweiLocator(
            self,
            _official_select(self.active_root().descendants(), selector),
        )

    def wait_for_timeout(self, milliseconds: float) -> None:
        self.wait_timeout_milliseconds.append(milliseconds)
        if self.active == "results":
            self.result_waits += 1
            if self.late_after_waits is not None and self.result_waits >= self.late_after_waits:
                self._class_node(self.results_root, "late-card").attrs.pop("hidden", None)
            if self.empty_after_waits is not None and self.result_waits >= self.empty_after_waits:
                self._class_node(self.results_root, "search-empty").attrs.pop("hidden", None)
            return
        if self.active != "detail":
            return
        if milliseconds == 300:
            return
        if self.pending_selection:
            group, (node, reveal_after) = next(iter(self.pending_selection.items()))
            self.selection_waits[group] += 1
            if self.selection_waits[group] >= reveal_after:
                self.select_node(group, node)
                del self.pending_selection[group]
            return
        if self.selected("capacity") == self.target_capacity() and self.selected("color") == "曜石黑":
            self.price_waits += 1
            if (
                self.price_visible_after_waits is not None
                and self.price_waits >= self.price_visible_after_waits
            ):
                for node in self.price_nodes():
                    node.attrs.pop("hidden", None)
            if self.identity_drift_after_price_waits == self.price_waits:
                self._url = "https://www.vmall.com/product/10080000000000.html"
            if self.ambiguous_capacity_after_price_waits == self.price_waits:
                other = next(
                    option
                    for option in self.options("capacity")
                    if option.text != self.target_capacity()
                )
                other.attrs["style"] = (
                    "border-color:rgb(207,10,44);color:rgb(207,10,44)"
                )
            return
        group = "capacity" if self.selected("capacity") != self.target_capacity() else "color"
        self.option_waits[group] += 1
        for node in self.options(group):
            reveal_after = node.attrs.get("data-reveal-after-option-waits")
            if reveal_after is not None and self.option_waits[group] >= int(reveal_after):
                node.attrs.pop("hidden", None)
                node.attrs.pop("data-reveal-after-option-waits", None)

    @staticmethod
    def _class_node(root: _OfficialNode, class_name: str) -> _OfficialNode:
        return next(
            node
            for node in root.descendants()
            if class_name in node.attrs.get("class", "").split()
        )

    def group_for(self, node: _OfficialNode) -> str | None:
        for group in ("capacity", "color"):
            if node in self.options(group):
                return group
        return None

    def group(self, group: str) -> _OfficialNode:
        label = "版本" if group == "capacity" else "颜色"
        return next(
            node
            for node in self.detail_root.descendants()
            if "sku-row" in node.attrs.get("class", "").split()
            and any(
                child.text == label
                for child in node.children
                if "sku-label" in child.attrs.get("class", "").split()
            )
        )

    def options(self, group: str) -> list[_OfficialNode]:
        return [
            node
            for node in self.group(group).descendants()
            if node.tag == "button"
            and "sku-option" in node.attrs.get("class", "").split()
        ]

    def selected(self, group: str) -> str | None:
        return next(
            (
                node.text
                for node in self.options(group)
                if self.option_is_selected(node)
            ),
            None,
        )

    def select_node(self, group: str, node: _OfficialNode) -> None:
        for option in self.options(group):
            option.attrs.pop("style", None)
        node.attrs["style"] = "border-color:rgb(207,10,44);color:rgb(207,10,44)"
        summaries = [
            item
            for item in self.detail_root.descendants()
            if "selected-summary" in item.attrs.get("class", "").split()
        ]
        capacity = node.text if group == "capacity" else self.selected("capacity")
        color = node.text if group == "color" else self.selected("color")
        if summaries and capacity and color:
            summaries[0].text_parts = [f"已选：{color}·{capacity}"]

    def target_capacity(self) -> str:
        return "256GB" if self.storage_only() else "8GB+256GB"

    def storage_only(self) -> bool:
        values = [node.text for node in self.options("capacity")]
        return bool(values) and all("+" not in value for value in values)

    def price_nodes(self) -> list[_OfficialNode]:
        return _official_select(
            self.detail_root.descendants(),
            _CAPTURE_SELECTOR_SPECS["price"]["selector"],
        )

    @staticmethod
    def option_is_selected(node: _OfficialNode) -> bool:
        style = node.attrs.get("style", "").replace(" ", "").lower()
        return (
            "border-color:rgb(207,10,44)" in style
            and "color:rgb(207,10,44)" in style
        )

    def next_current_price(self, node: _OfficialNode) -> str:
        if node.attrs.get("hidden") is not None:
            return ""
        if node is not self.price_nodes()[0]:
            if self.auxiliary_prices:
                value = self.auxiliary_prices[
                    self.auxiliary_price_poll % len(self.auxiliary_prices)
                ]
                self.auxiliary_price_poll += 1
                return value
            return node.text
        if self.price_poll in self.price_missing_polls:
            self.price_poll += 1
            return ""
        if self.price_poll in self.price_stale_polls:
            self.price_poll += 1
            return "¥4899"
        if not self.unstable_prices:
            if self.price_missing_polls or self.price_stale_polls:
                self.price_poll += 1
            return node.text
        values = ("¥4999", "¥5099", "¥4899", "¥5099")
        value = values[self.price_poll % len(values)]
        self.price_poll += 1
        return value

    def dom_rect(self, node: _OfficialNode) -> dict[str, float] | None:
        if self.active == "results":
            if node.attrs.get("id") == "search-kw":
                return {"x": 30.0, "y": 70.0, "width": 380.0, "height": 42.0}
            if "search-result" in node.attrs.get("class", "").split():
                return {"x": 20.0, "y": 130.0, "width": 900.0, "height": 560.0}
        if self.active != "detail":
            return None
        if self.remove_color_geometry and node is self.selected_node("color"):
            return None
        group_role = next(
            (role for role in ("capacity", "color") if node is self.group(role)),
            None,
        )
        raw_y = (
            110.0
            if node.attrs.get("id") == "prd-detail-name"
            else 174.0
            if node in self.price_nodes()
            else 280.0
            if group_role == "capacity"
            else 372.0
            if group_role == "color"
            else 288.0
            if node is self.selected_node("capacity")
            else 380.0
        )
        return {
            "x": 30.0 * self.capture_scale,
            "y": raw_y * self.capture_scale - self.scroll_offset,
            "width": (650.0 if group_role else 520.0) * self.capture_scale,
            "height": (80.0 if group_role else 42.0) * self.capture_scale,
        }

    def selected_node(self, group: str) -> _OfficialNode | None:
        return next(
            (
                node
                for node in self.options(group)
                if self.option_is_selected(node)
            ),
            None,
        )

    def evaluate(self, script: str, argument: object = None) -> object:
        if "removeAttribute(attribute)" in script:
            if self.capture_scale_marker:
                assert self.capture_scale_original is not None
                self.capture_scale = self.capture_scale_original
                self.capture_scale_original = None
                self.capture_scale_marker = False
            return True
        if "data-quotation-capture-scale-original" in script:
            if argument is not None and self.capture_scale != float(argument):
                if not self.capture_scale_marker:
                    self.capture_scale_original = self.capture_scale
                    self.capture_scale_marker = True
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "inlineZoom" in script and "computedZoom" in script:
            return {"inlineZoom": self.capture_scale, "computedZoom": self.capture_scale}
        if "window.scrollBy" in script:
            value = argument.get("delta") if isinstance(argument, dict) else argument
            delta = float(value)
            if abs(delta) > 160:
                raise AssertionError("VMALL capture scroll exceeded 160 CSS pixels")
            self.light_scrolls.append(delta)
            self.scroll_offset += delta
            return True
        if "VMALL_CONFIG_OPTION_INDEXES" in script:
            if not isinstance(argument, dict):
                raise AssertionError("VMALL option query requires a selector and label")
            selector = argument.get("selector")
            label = argument.get("label")
            if selector != "button" or label not in {"版本", "颜色"}:
                return []
            group = "capacity" if label == "版本" else "color"
            return [
                index
                for index, node in enumerate(_official_select(self.detail_root.descendants(), selector))
                if node in self.options(group) and node.attrs.get("hidden") is None
            ]
        if not isinstance(argument, dict) or tuple(argument) != (
            "title",
            "capacity",
            "color",
        ):
            raise AssertionError("VMALL capture must pass three explicit visual proof selectors")
        self.capture_selector_arguments.append(argument)
        if "unionTop" in script and "getBoundingClientRect" in script:
            return self.capture_geometry(argument)
        if "elementFromPoint" in script:
            return all(
                self.visible_and_unobscured(node)
                for node in self.proofs_from_selectors(argument).values()
            )
        raise AssertionError(f"unexpected VMALL capture evaluation: {script[:90]}")

    def proofs_from_selectors(self, selectors: object) -> dict[str, _OfficialNode]:
        if not isinstance(selectors, dict) or tuple(selectors) not in {
            ("title", "capacity", "color"),
            ("title", "price", "capacity", "color"),
        }:
            raise AssertionError("VMALL capture requires named visual selectors")
        proofs: dict[str, _OfficialNode] = {}
        for role in selectors:
            declaration = selectors[role]
            if not isinstance(declaration, dict) or declaration.get("role") != role:
                raise AssertionError(f"{role} selector has no matching role")
            selector = declaration.get("selector")
            if not isinstance(selector, str):
                raise AssertionError(f"{role} selector is not CSS")
            matches = _official_select(self.detail_root.descendants(), selector)
            if role == "price":
                expected_text = declaration.get("expected_text")
                if expected_text is not None:
                    if not isinstance(expected_text, str):
                        raise AssertionError("price selector text is invalid")
                    matches = [
                        node
                        for node in matches
                        if node.text.strip() == expected_text
                    ]
            elif role in {"capacity", "color"}:
                expected_text = declaration.get("expected_text")
                if not isinstance(expected_text, str):
                    raise AssertionError(f"{role} selector has no selected business text")
                expected_group = "版本" if role == "capacity" else "颜色"
                if declaration.get("group_label") != expected_group:
                    raise AssertionError(f"{role} selector has the wrong VMALL group")
                group = "capacity" if role == "capacity" else "color"
                matches = [
                    node
                    for node in matches
                    if node in self.options(group)
                    and self.option_is_selected(node)
                    and node.text == expected_text
                ]
            elif role == "title":
                if set(declaration) != {"role", "selector"}:
                    raise AssertionError("title selector has unsupported metadata")
            if role in {"capacity", "color"} and "expected_text" not in declaration:
                raise AssertionError(f"{role} selector lacks selected business text")
            if len(matches) != 1:
                raise AssertionError(f"{role} selector must resolve one live VMALL node")
            proofs[role] = matches[0]
        if "price" in selectors:
            price_selector = selectors["price"]["selector"]
            if (
                "data-quote-capture-proof" in price_selector
                and proofs["price"].attrs.get("data-quote-capture-proof") != "price"
            ):
                raise AssertionError("price selector did not resolve the observed candidate")
        if proofs["capacity"].text != self.target_capacity():
            raise AssertionError("capacity selector did not resolve the selected version")
        if proofs["color"].text != "曜石黑":
            raise AssertionError("color selector did not resolve the selected color")
        return proofs

    def capture_geometry(self, selectors: object) -> dict[str, object]:
        boxes = [self.dom_rect(node) for node in self.proofs_from_selectors(selectors).values()]
        if any(box is None for box in boxes):
            return {"missing": True}
        concrete = [box for box in boxes if box is not None]
        occlusions: list[dict[str, float]] = []
        for box in concrete:
            blocker_top = self.blocker_top_at_center(box)
            if blocker_top is not None:
                occlusions.append(
                    {"proofBottom": box["y"] + box["height"], "blockerTop": blocker_top}
                )
        return {
            "unionTop": min(box["y"] for box in concrete),
            "unionBottom": max(box["y"] + box["height"] for box in concrete),
            "occlusions": occlusions,
            "viewportHeight": self.viewport_height,
            "scrollY": self.scroll_offset,
        }

    def blocker_top_at_center(self, box: dict[str, float]) -> float | None:
        if self.blocker is None:
            return None
        top, bottom, fixed = self.blocker
        if not fixed:
            top -= self.scroll_offset
            bottom -= self.scroll_offset
        center = box["y"] + box["height"] / 2
        return top if top <= center <= bottom else None

    def visible_and_unobscured(self, node: _OfficialNode) -> bool:
        box = self.dom_rect(node)
        return bool(
            box is not None
            and box["y"] >= 0
            and box["y"] + box["height"] <= self.viewport_height
            and self.blocker_top_at_center(box) is None
        )


def _huawei_page(page: _OfficialFixturePage) -> _HuaweiPage:
    if not isinstance(page, _HuaweiPage):
        raise TypeError("expected _HuaweiPage")
    return page


def _ancestors(node: _OfficialNode) -> list[_OfficialNode]:
    result: list[_OfficialNode] = []
    current = node.parent
    while current is not None:
        result.append(current)
        current = current.parent
    return result


def _disabled(node: _OfficialNode) -> bool:
    return (
        node.attrs.get("disabled") is not None
        or node.attrs.get("aria-disabled") == "true"
        or "disabled" in node.attrs.get("class", "").split()
    )


def _fixture_detail_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in {"www.vmall.com", "item.vmall.com"} and parsed.path.startswith(
        "/product/"
    )


def _item_detail_url(url: str) -> str:
    parsed = urlsplit(url)
    return parsed._replace(netloc="item.vmall.com").geturl()


def _task(
    model: str = "HUAWEI Mate 70 Pro",
    *,
    ram: str = "8GB",
    storage: str = "256GB",
    color: str = "曜石黑",
) -> WebsiteTask:
    return WebsiteTask(
        task_id="huawei",
        run_id="huawei-contract",
        source_row_number=2,
        output_row_number=2,
        material_code="HUAWEI",
        brand="华为",
        model_name=model,
        ram=ram,
        storage=storage,
        color=color,
        channel=WebsiteChannel.OFFICIAL,
    )


def _adapter() -> Any:
    spec = next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "华为" and spec.channel is WebsiteChannel.OFFICIAL
    )
    module = importlib.import_module("quote_app.sites.official_brands.huawei")
    return module.HuaweiOfficialAdapter(spec)


def _detail_checkpoint(
    task: WebsiteTask,
    *,
    outcome: BusinessOutcome = BusinessOutcome.PRICE_FOUND,
    url: str = DETAIL,
) -> WebsiteObservationCheckpoint:
    return WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=outcome,
        price=Decimal("4999") if outcome is BusinessOutcome.PRICE_FOUND else None,
        url=url,
        observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )


def _set_huawei_cards(
    page: _HuaweiPage,
    cards: tuple[tuple[str, str], ...] | None = None,
    *,
    exact_href: str | None = None,
    retain_late: bool = False,
) -> None:
    region = _HuaweiPage._class_node(page.results_root, "search-result")
    product_list = next(node for node in region.children if node.tag == "ul")
    late = next(
        node
        for node in product_list.children
        if "late-card" in node.attrs.get("class", "").split()
    )
    product_list.children = [late] if retain_late else []
    if cards is None:
        cards = (("HUAWEI Mate 70 Pro 8GB+256GB 曜石黑", exact_href or DETAIL),)
    for index, (title, href) in enumerate(cards):
        card = _OfficialNode("li", {"data-product-id": str(9000 + index)}, product_list)
        link = _OfficialNode("a", {"class": "product-link", "href": href}, card)
        label = _OfficialNode("span", {"class": "product-name"}, link)
        label.text_parts = [title]
        link.children.append(label)
        card.children.append(link)
        product_list.children.append(card)


def _remove_option(page: _HuaweiPage, group: str, target: str) -> None:
    option = next(node for node in page.options(group) if node.text == target)
    assert option.parent is not None
    option.parent.children.remove(option)


def _delay_option(page: _HuaweiPage, group: str, target: str, waits: int) -> None:
    option = next(node for node in page.options(group) if node.text == target)
    option.attrs.pop("style", None)
    option.attrs["hidden"] = ""
    option.attrs["data-reveal-after-option-waits"] = str(waits)


def _preselect_targets(page: _HuaweiPage) -> None:
    for group, target in (("capacity", page.target_capacity()), ("color", "曜石黑")):
        page.select_node(group, next(node for node in page.options(group) if node.text == target))


def _detail_page_with_selected_nodes() -> _HuaweiPage:
    page = _HuaweiPage()
    page.goto(DETAIL)
    _preselect_targets(page)
    return page


def test_huawei_fixture_preserves_public_vmall_semantics_and_isolated_dom_states() -> None:
    source = (FIXTURES / "search_results.html").read_text()
    assert "official-huawei-" not in source
    assert "data-product-id" in source and "product-name" in source
    page = _HuaweiPage()
    assert page.locator("input").count() == 0
    page.goto(ENTRY)
    assert page.locator("input#search-kw").count() == 1
    assert page.locator("div#prd-detail-name[data-testid=prd-detail-name]").count() == 0
    search = page.locator("input#search-kw")
    search.fill("HUAWEI Mate 70 Pro")
    search.press("Enter")
    assert page.locator("li[data-product-id] a.product-link").count() > 0
    assert page.locator("div#prd-detail-name[data-testid=prd-detail-name]").count() == 0
    page.goto(DETAIL)
    assert page.locator("div#prd-detail-name[data-testid=prd-detail-name]").count() == 1
    assert page.locator("li[data-product-id]").count() == 0
    fresh = _HuaweiPage()
    assert fresh.locator("input").count() == 0
    assert fresh.locator("li[data-product-id]").count() == 0
    assert fresh.locator("div#prd-detail-name[data-testid=prd-detail-name]").count() == 0


def test_huawei_fixture_uses_reviewed_vmall_detail_nodes_without_test_markers() -> None:
    sources = [path.read_text() for path in sorted(FIXTURES.glob("*.html"))]
    source = "\n".join(sources)
    page = _HuaweiPage()
    page.goto(DETAIL)

    assert page.locator("div#prd-detail-name[data-testid=prd-detail-name]").count() == 1
    assert page.locator("[data-testid=vui_text_container]").count() >= 3
    assert all(
        marker not in source
        for marker in (
            "adopted",
            "data-group",
            "main class=\"product-detail\"",
            "id=\"pro-name\"",
            "id=\"pro-price\"",
            "price-now",
        )
    )
    assert page.url.startswith("https://item.vmall.com/product/")

    fixture_only_markers = (
        "purchase-summary",
        "price-line",
        "sku-row",
        "sku-label",
        "sku-option",
        "late-card",
        "search-empty",
        "data-reveal-after",
        "data-select-after",
    )
    serialized_specs = repr(_CAPTURE_SELECTOR_SPECS)
    assert all(marker not in serialized_specs for marker in fixture_only_markers)
    production = (
        Path(__file__).parents[2]
        / "src/quote_app/sites/official_brands/huawei.py"
    )
    if production.exists():
        production_source = production.read_text()
        assert all(marker not in production_source for marker in fixture_only_markers)


def test_huawei_fixture_contains_current_reference_and_excluded_promotional_amounts() -> None:
    page = _HuaweiPage()
    text = page.detail_root.text
    assert all(
        value in text
        for value in ("4999", "5199", "6499", "750", "208.29", "1000", "699", "99")
    )
    assert all(label in text for label in ("颜色", "版本", "暂时缺货"))


def test_huawei_capture_harness_resolves_exactly_four_real_detail_nodes() -> None:
    page = _detail_page_with_selected_nodes()
    proofs = page.proofs_from_selectors(_capture_selector_specs())
    assert tuple(proofs) == ("title", "price", "capacity", "color")
    assert proofs["title"].text.startswith("HUAWEI Mate 70 Pro")
    assert proofs["price"].text == "4999"
    assert proofs["capacity"].text == "8GB+256GB"
    assert proofs["color"].text == "曜石黑"


def test_huawei_capture_harness_selects_the_requested_live_price_candidate() -> None:
    page = _detail_page_with_selected_nodes()
    proofs = page.proofs_from_selectors(_capture_selector_specs(price="5199"))
    assert proofs["price"].text == "5199"


def test_huawei_capture_price_selector_excludes_promotional_and_sticky_duplicates() -> None:
    page = _detail_page_with_selected_nodes()
    matches = _official_select(
        page.detail_root.descendants(),
        _CAPTURE_SELECTOR_SPECS["price"]["selector"],
    )
    assert [node.text for node in matches] == ["4999", "5199"]
    proofs = page.proofs_from_selectors(_capture_selector_specs(price="4999"))
    assert proofs["price"] is matches[0]


def test_huawei_capture_harness_rejects_wrong_real_selector() -> None:
    page = _detail_page_with_selected_nodes()
    wrong = _capture_selector_specs()
    wrong["price"]["selector"] = "s.reference-price [data-testid=vui_text_container]"
    with pytest.raises(AssertionError, match="price selector"):
        page.proofs_from_selectors(wrong)


def test_huawei_uses_direct_search_route_and_quotes_selected_offer() -> None:
    page = _HuaweiPage()
    result = _adapter().observe(_task(), page)
    assert page.goto_calls[0] == (
        "https://www.vmall.com/portal/search/index.html?"
        "targetRoute=searchresult&searchWord=HUAWEI%20Mate%2070%20Pro"
    )
    assert page.fill_calls == []
    assert page.presses == [] and page.search_submissions == 0
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == Decimal("4999")


def test_huawei_current_exact_text_card_enters_numeric_detail_without_legacy_selectors() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(page, ())
    region = _HuaweiPage._class_node(page.results_root, "search-result")
    title = _OfficialNode(
        "span",
        {"data-current-vmall-detail": DETAIL},
        region,
    )
    title.text_parts = ["HUAWEI Mate 70 Pro"]
    region.children.append(title)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == Decimal("4999")
    assert result.url == DETAIL


def test_huawei_current_portal_card_enters_detail_by_clicking_the_whole_card() -> None:
    """The live React grid binds navigation to the card, not the title child."""

    page = _HuaweiPage()
    _set_huawei_cards(page, ())
    react_root = _OfficialNode("main", {"id": "react-root"}, page.results_root)
    card = _OfficialNode(
        "div",
        {
            "data-testid": "0-searchProduct",
            "data-card-only-detail": DETAIL,
        },
        react_root,
    )
    title = _OfficialNode(
        "div",
        {"data-testid": "vui_text_container"},
        card,
    )
    title.text_parts = ["HUAWEI Mate 70 Pro"]
    card.children.append(title)
    react_root.children.append(card)
    page.results_root.children.append(react_root)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == Decimal("4999")
    assert result.url == DETAIL


def test_huawei_current_exact_anchor_title_enters_detail_without_legacy_card_classes() -> None:
    """The current VMALL grid exposes the exact title on its clickable anchor."""

    page = _HuaweiPage()
    _set_huawei_cards(page, ())
    region = _HuaweiPage._class_node(page.results_root, "search-result")
    title_link = _OfficialNode(
        "a",
        {"data-current-vmall-detail": DETAIL},
        region,
    )
    title_link.text_parts = ["HUAWEI Mate 70 Pro"]
    region.children.append(title_link)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == Decimal("4999")
    assert result.url == DETAIL


def test_huawei_current_card_outside_legacy_region_ignores_stale_top_search_text() -> None:
    """The visible exact card is authoritative even when VMALL's header is stale."""

    class _CurrentGridPage(_HuaweiPage):
        def goto(self, url: str, **kwargs: object) -> None:
            super().goto(url, **kwargs)
            if self.active != "results":
                return
            search = next(
                node
                for node in self.results_root.descendants()
                if node.attrs.get("id") == "search-kw"
            )
            search.attrs["value"] = "WATCH GT 7"

    page = _CurrentGridPage()
    _set_huawei_cards(page, ())
    current_grid = _OfficialNode("section", {"class": "goods-grid-current"}, page.results_root)
    exact_title = _OfficialNode(
        "h2",
        {"data-current-vmall-detail": DETAIL},
        current_grid,
    )
    exact_title.text_parts = ["HUAWEI Mate 70 Pro"]
    current_grid.children.append(exact_title)
    page.results_root.children.append(current_grid)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.price == Decimal("4999")
    assert result.url == DETAIL


def test_huawei_current_exact_text_click_bubbles_through_non_anchor_card() -> None:
    """The current VMALL card is clickable even when its title has no href."""

    page = _HuaweiPage()
    _set_huawei_cards(page, ())
    card = _OfficialNode(
        "article",
        {"data-current-vmall-detail": DETAIL},
        page.results_root,
    )
    exact_title = _OfficialNode("em", {}, card)
    exact_title.text_parts = ["HUAWEI Mate 70 Pro"]
    card.children.append(exact_title)
    page.results_root.children.append(card)

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.url == DETAIL


@pytest.mark.parametrize("reveal_after", [18, 39])
def test_huawei_waits_for_late_exact_card_through_full_search_window(reveal_after: int) -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        (("HUAWEI Mate 70 Pro+ 16GB+512GB 羽衣白", "https://www.vmall.com/product/1001.html"),),
        retain_late=True,
    )
    page.late_after_waits = reveal_after
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.result_waits == reveal_after


def test_huawei_early_empty_signal_does_not_hide_late_exact_card() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(page, (), retain_late=True)
    page.empty_after_waits = 2
    page.late_after_waits = 19
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.result_waits == 19


def test_huawei_legal_no_model_requires_complete_forty_tick_window() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        (("HUAWEI Mate 70 Pro+", "https://www.vmall.com/product/10086259366531.html"),),
    )
    page.empty_after_waits = 1
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.NO_MODEL
    assert page.result_waits == 40
    assert tuple(rect.role for rect in result.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


def test_huawei_sold_out_exact_card_still_enters_detail() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        (("HUAWEI Mate 70 Pro 8GB+256GB 曜石黑 暂时缺货", DETAIL),),
    )
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND


def test_huawei_skips_first_invalid_exact_url_for_later_valid_card() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        (
            ("HUAWEI Mate 70 Pro 8GB+256GB", "https://www.vmall.com/product/not-a-number.html"),
            ("HUAWEI Mate 70 Pro 12GB+512GB", DETAIL),
        ),
    )
    assert _adapter().observe(_task(), page).url == DETAIL


def test_huawei_all_exact_cards_with_invalid_urls_are_technical_not_no_model() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        (("HUAWEI Mate 70 Pro 8GB+256GB", "https://evil.example/product/1001.html"),),
    )
    with pytest.raises((LayoutRecognitionError, NonRetryableTechnicalError, ValueError)):
        _adapter().observe(_task(), page)


@pytest.mark.parametrize(
    "name",
    [
        "HUAWEI Mate 70 Pro+",
        "HUAWEI Mate 70 Pro Plus",
        "HUAWEI Mate 70 Pro Ultra",
        "HUAWEI Mate 70 Pro Max",
        "HUAWEI Mate 70 Pro 青春版",
        "HUAWEI Mate 70 Pro 优享版",
        "HUAWEI Mate 70 Pro 手机壳",
        "HUAWEI Mate 70 Pro 保护膜",
        "HUAWEI Mate 70 Pro 充电器",
        "适用 HUAWEI Mate 70 Pro 支架",
    ],
)
def test_huawei_rejects_derived_models_and_accessories(name: str) -> None:
    page = _HuaweiPage()
    _set_huawei_cards(page, ((name, "https://www.vmall.com/product/10086259366531.html"),))
    page.empty_after_waits = 1
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.NO_MODEL


@pytest.mark.parametrize(
    "derived",
    ("Pro", "Pro+", "Plus", "Ultra", "Max"),
)
def test_huawei_base_model_rejects_derived_variant_cards(derived: str) -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        ((f"HUAWEI 畅享 90 {derived} 8GB+256GB 曜石黑", DETAIL),),
    )
    page.empty_after_waits = 1
    result = _adapter().observe(_task("华为畅享 90"), page)
    assert result.outcome is BusinessOutcome.NO_MODEL


def test_huawei_target_that_contains_pro_accepts_legal_capacity_color_tail() -> None:
    page = _HuaweiPage()
    _set_huawei_cards(
        page,
        (("HUAWEI 畅享 90 Pro 8GB+256GB 曜石黑", DETAIL),),
    )
    page.title_override = "HUAWEI 畅享 90 Pro 8GB+256GB 曜石黑"
    result = _adapter().observe(_task("华为畅享 90 Pro"), page)
    assert result.outcome is BusinessOutcome.PRICE_FOUND


@pytest.mark.parametrize(
    "detail_url",
    [
        "https://www.vmall.com/product/10086259366534.html",
        "https://www.vmall.com/product/comdetail/index.html?prdId=10086259366534",
        "https://item.vmall.com/product/10086259366534.html",
        "https://item.vmall.com/product/comdetail/index.html?prdId=10086259366534",
    ],
)
def test_huawei_accepts_both_numeric_vmall_detail_routes(detail_url: str) -> None:
    page = _HuaweiPage()
    _set_huawei_cards(page, exact_href=detail_url)
    assert _adapter().observe(_task(), page).url == _item_detail_url(detail_url)


@pytest.mark.parametrize(
    "detail_url",
    [
        "http://www.vmall.com/product/10086259366534.html",
        "https://m.vmall.com/product/10086259366534.html",
        "https://www.vmall.com/product/not-a-number.html",
        "https://www.vmall.com/product/comdetail/index.html?prdId=abc",
        "https://www.vmall.com/product/comdetail/index.html",
    ],
)
def test_huawei_rejects_unapproved_or_non_numeric_detail_routes(detail_url: str) -> None:
    page = _HuaweiPage()
    _set_huawei_cards(page, exact_href=detail_url)
    with pytest.raises((LayoutRecognitionError, NonRetryableTechnicalError, ValueError)):
        _adapter().observe(_task(), page)


def test_huawei_selects_exact_full_capacity_then_exact_color() -> None:
    page = _HuaweiPage()
    for group, target in (("capacity", page.target_capacity()), ("color", "曜石黑")):
        option = next(node for node in page.options(group) if node.text == target)
        option.attrs.pop("style", None)
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_clicks == ["capacity", "color"]
    assert page.selected("capacity") == "8GB+256GB"
    assert page.selected("color") == "曜石黑"


def test_huawei_reads_configuration_from_its_interactive_area_not_every_detail_div() -> None:
    """A live VMALL detail page has thousands of divs; global scans stall the run."""
    page = _HuaweiPage()
    page.reject_global_detail_div_scan = True

    result = _adapter().observe(_task(), page)

    assert result.outcome is BusinessOutcome.PRICE_FOUND


def test_huawei_falls_back_to_storage_only_only_when_page_has_no_ram_dimension() -> None:
    page = _HuaweiPage("detail_storage_only.html")
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert result.semantic_state.capacity == "256GB"
    assert page.selected("capacity") == "256GB"


def test_huawei_never_uses_storage_only_when_full_versions_exist() -> None:
    page = _HuaweiPage()
    _remove_option(page, "capacity", "8GB+256GB")
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE
    assert page.option_waits["capacity"] == 20


@pytest.mark.parametrize(
    ("fixture", "outcome", "group", "group_role"),
    [
        (
            "detail_missing_capacity.html",
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            "capacity",
            "capacity_group",
        ),
        (
            "detail_missing_color.html",
            BusinessOutcome.COLOR_UNAVAILABLE,
            "color",
            "color_group",
        ),
    ],
)
def test_huawei_complete_missing_configuration_is_legal_no(
    fixture: str,
    outcome: BusinessOutcome,
    group: str,
    group_role: str,
) -> None:
    page = _HuaweiPage(fixture)
    result = _adapter().observe(_task(), page)
    assert result.outcome is outcome
    assert tuple(rect.role for rect in result.css_rectangles) == ("title", group_role)
    title_node = page.locator(
        "div#prd-detail-name[data-testid=prd-detail-name]"
    ).nodes[0]
    expected_boxes = (page.dom_rect(title_node), page.dom_rect(page.group(group)))
    assert all(box is not None for box in expected_boxes)
    for rectangle, expected in zip(result.css_rectangles, expected_boxes, strict=True):
        assert expected is not None
        assert (rectangle.x, rectangle.y, rectangle.width, rectangle.height) == (
            expected["x"],
            expected["y"],
            expected["width"],
            expected["height"],
        )
    assert max(rect.y + rect.height for rect in result.css_rectangles) <= page.viewport_height
    assert min(rect.y for rect in result.css_rectangles) >= 0


@pytest.mark.parametrize(
    ("fixture", "role", "label", "expected_options"),
    [
        (
            "detail_missing_capacity.html",
            "capacity",
            "版本",
            ("8GB+128GB", "12GB+512GB"),
        ),
        (
            "detail_missing_color.html",
            "color",
            "颜色",
            ("雪域白", "云杉绿"),
        ),
    ],
)
def test_huawei_legal_no_fixture_keeps_product_identity_and_complete_option_group(
    fixture: str,
    role: str,
    label: str,
    expected_options: tuple[str, ...],
) -> None:
    page = _HuaweiPage(fixture)
    page.goto(DETAIL_INPUT)
    title = page.locator("div#prd-detail-name[data-testid=prd-detail-name]")
    assert title.count() == 1 and "HUAWEI Mate 70 Pro" in title.inner_text()
    group = page.group(role)
    assert any(child.text == label for child in group.children)
    assert tuple(option.text for option in page.options(role)) == expected_options
    title_box = page.dom_rect(title.nodes[0])
    group_box = page.dom_rect(group)
    assert title_box is not None and group_box is not None
    assert min(title_box["y"], group_box["y"]) >= 0
    assert max(
        title_box["y"] + title_box["height"],
        group_box["y"] + group_box["height"],
    ) <= page.viewport_height


@pytest.mark.parametrize("group", ["capacity", "color"])
def test_huawei_waits_for_target_option_arriving_at_tick_nineteen(group: str) -> None:
    page = _HuaweiPage()
    target = "8GB+256GB" if group == "capacity" else "曜石黑"
    _delay_option(page, group, target, 19)
    result = _adapter().observe(_task(), page)
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_waits[group] == 19


def test_huawei_explicitly_reconfirms_preselected_capacity() -> None:
    page = _HuaweiPage()
    _preselect_targets(page)
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_clicks == ["capacity"]


def test_huawei_product_sold_out_copy_does_not_make_selectable_option_legal_no() -> None:
    page = _HuaweiPage()
    assert "暂时缺货" in page.detail_root.text
    assert _adapter().observe(_task(), page).outcome is BusinessOutcome.PRICE_FOUND


def test_huawei_uses_lowest_current_price_and_rejects_promotional_numbers() -> None:
    result = _adapter().observe(_task(), _HuaweiPage())
    assert result.price == Decimal("4999")


def test_huawei_promotion_with_same_public_text_testid_does_not_pollute_price() -> None:
    page = _HuaweiPage()
    promotional = next(
        node
        for node in page.detail_root.descendants()
        if "coupon-offer" in node.attrs.get("class", "").split()
    )
    assert any(
        child.attrs.get("data-testid") == "vui_text_container"
        for child in promotional.children
    )
    assert _adapter().observe(_task(), page).price == Decimal("4999")


def test_huawei_related_product_price_does_not_pollute_primary_detail_offer() -> None:
    """Only the product root containing the live detail title owns the offer."""

    page = _HuaweiPage()
    _append_related_price(page, "99")

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4999")


def test_huawei_capture_ignores_same_price_from_related_product_root() -> None:
    page = _HuaweiPage()
    _append_related_price(page, "4999")
    adapter = _adapter()

    result = adapter.observe(_task(), page)
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        _task(),
        page,
        result.semantic_state,
    )

    assert rectangles == ()


@pytest.mark.parametrize(
    ("url", "marker", "error"),
    [
        ("https://id1.cloud.huawei.com/CAS/portal/login.html", None, LoginRequired),
        ("https://www.vmall.com/account/login", "login", LoginRequired),
        ("https://www.vmall.com/risk/verify", None, SecurityVerificationRequired),
        ("https://www.vmall.com/", "captcha", SecurityVerificationRequired),
    ],
)
def test_huawei_explicitly_pauses_for_login_and_security_states(
    url: str,
    marker: str | None,
    error: type[Exception],
) -> None:
    page = _HuaweiPage()
    page._url = url
    page.active = "home"
    if marker == "login":
        node = _OfficialNode("input", {"type": "password"}, page.home_root)
        page.home_root.children.append(node)
    elif marker == "captcha":
        node = _OfficialNode("div", {"class": "geetest-panel"}, page.home_root)
        page.home_root.children.append(node)
    with pytest.raises(error):
        _adapter().raise_if_manual_action(page)


def test_huawei_price_may_appear_late_then_locks_immediately() -> None:
    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    page.price_visible_after_waits = 6
    result = _adapter().observe(_task(), page)
    assert result.price == Decimal("4999")
    assert page.price_waits == 6


def test_huawei_does_not_repeat_checks_after_first_visible_price() -> None:
    page = _HuaweiPage()
    result = _adapter().observe(_task(), page)
    assert result.price == Decimal("4999")
    assert page.price_waits == 1


def test_huawei_same_price_stabilizes_across_transient_react_price_gaps() -> None:
    """A re-rendered price node may disappear briefly without changing the offer."""

    page = _HuaweiPage()
    page.price_missing_polls = {2, 5, 8, 11}
    for node in page.price_nodes()[1:]:
        node.attrs["hidden"] = ""

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4999")


def test_huawei_same_offer_stabilizes_across_transient_stale_react_prices() -> None:
    """A stale prior-SKU price must not restart otherwise stable offer evidence."""

    page = _HuaweiPage()
    page.price_stale_polls = {2, 5, 8}
    for node in page.price_nodes()[1:]:
        node.attrs["hidden"] = ""

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4999")


def test_huawei_authoritative_price_ignores_dynamic_auxiliary_current_nodes() -> None:
    """Only the first scoped current-price node describes the selected SKU."""

    page = _HuaweiPage()
    page.auxiliary_prices = ("¥4899", "¥4799", "¥4699", "¥4599")

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4999")


def test_huawei_accepts_current_price_from_the_primary_product_price_region() -> None:
    """VMALL may rename the price wrappers without changing their semantics."""

    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    price = page.price_nodes()[0]
    assert price.parent is not None
    assert price.parent.parent is not None
    price.parent.attrs["class"] = "sku-offer-price-current"
    price.parent.parent.attrs["class"] = "sku-offer-price-panel"

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert observation.price == Decimal("4999")
    assert adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    ) == ()


def test_huawei_hidden_canonical_price_does_not_block_visible_price_fallback() -> None:
    """A stale hidden canonical node must not veto a visible selected-SKU price."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    wrapper = _OfficialNode("div", {"class": "sku-price-panel"}, product_root)
    amount = _OfficialNode(
        "span",
        {
            "class": "sku-price-current",
            "data-testid": "vui_text_container",
            "style": "color:rgb(207,10,44)",
        },
        wrapper,
    )
    amount.text_parts = ["4999"]
    wrapper.children.append(amount)
    product_root.children.append(wrapper)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_reads_visible_detail_price_without_legacy_product_root() -> None:
    """A current VMALL detail may render the offer without ``data-prdid``."""

    page = _HuaweiPage()
    for node in page.detail_root.descendants():
        node.attrs.pop("data-prdid", None)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_formal_capture_does_not_require_price_node_after_observation() -> None:
    """Configuration evidence remains capturable after the price renderer changes."""

    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe(task, page)
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
        node.attrs.pop("data-quote-capture-proof", None)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    )

    assert rectangles == ()


def test_huawei_accepts_visible_selected_price_when_title_is_outside_price_root() -> None:
    """A VMALL layout wrapper change must not block an otherwise valid offer."""

    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    title = next(
        node
        for node in product_root.descendants()
        if node.attrs.get("id") == "prd-detail-name"
    )
    product_root.children.remove(title)
    title.parent = page.detail_root
    page.detail_root.children.insert(0, title)

    observation = adapter.observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_accepts_currency_price_with_unrelated_dynamic_class() -> None:
    """A visible price in the selected product root must not depend on class names."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    wrapper = _OfficialNode("div", {"class": "runtime-token-a8f3"}, product_root)
    amount = _OfficialNode("span", {"class": "runtime-token-b91c"}, wrapper)
    amount.text_parts = ["¥4999"]
    wrapper.children.append(amount)
    product_root.children.append(wrapper)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_accepts_current_price_text_node_without_red_styling() -> None:
    """The current VMALL renderer exposes a neutral-colour price_text div."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    amount = _OfficialNode(
        "div",
        {
            "data-testid": "price_text",
            "style": "color:rgba(0,0,0,0.9)",
        },
        product_root,
    )
    amount.text_parts = ["¥4999"]
    product_root.children.append(amount)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_empty_price_text_shell_does_not_hide_later_current_price() -> None:
    """A visible empty renderer shell must not short-circuit the real price."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    empty_shell = _OfficialNode(
        "div",
        {"data-testid": "price_text"},
        product_root,
    )
    summary = _OfficialNode("div", {"class": "summary-price"}, product_root)
    current = _OfficialNode("div", {"class": "current-price"}, summary)
    amount = _OfficialNode(
        "div",
        {"data-testid": "vui_text_container"},
        current,
    )
    amount.text_parts = ["售价 4999"]
    current.children.append(amount)
    summary.children.append(current)
    product_root.children.extend((empty_shell, summary))

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_accepts_plain_amount_from_primary_product_price_context() -> None:
    """VMALL may paint the currency sign while exposing only the amount as text."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    wrapper = _OfficialNode(
        "div",
        {"class": "runtime-current-price-shell"},
        product_root,
    )
    amount = _OfficialNode(
        "span",
        {
            "class": "runtime-amount-token",
            "style": "color:rgb(207,10,44)",
        },
        wrapper,
    )
    amount.text_parts = ["4999"]
    wrapper.children.append(amount)
    product_root.children.append(wrapper)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_accepts_price_when_currency_and_amount_are_split_across_nodes() -> None:
    """VMALL may render the currency sign and amount as sibling leaves."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    wrapper = _OfficialNode("div", {"class": "runtime-price-shell"}, product_root)
    symbol = _OfficialNode("span", {"class": "runtime-currency"}, wrapper)
    symbol.text_parts = ["¥"]
    amount = _OfficialNode("span", {"class": "runtime-amount"}, wrapper)
    amount.text_parts = ["4999"]
    wrapper.children.extend((symbol, amount))
    product_root.children.append(wrapper)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_ignores_an_earlier_unrelated_product_root_for_split_price() -> None:
    """A related product root must not hide the selected product's live price."""

    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    product_root = next(
        node for node in page.detail_root.descendants() if "data-prdid" in node.attrs
    )
    decoy = _OfficialNode(
        "section",
        {"data-prdid": "earlier-related-product"},
        page.detail_root,
    )
    page.detail_root.children.insert(0, decoy)
    wrapper = _OfficialNode("div", {"class": "runtime-price-shell"}, product_root)
    symbol = _OfficialNode("span", {"class": "runtime-currency"}, wrapper)
    symbol.text_parts = ["¥"]
    amount = _OfficialNode("span", {"class": "runtime-amount"}, wrapper)
    amount.text_parts = ["4999"]
    wrapper.children.extend((symbol, amount))
    product_root.children.append(wrapper)

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4999")


def test_huawei_locks_the_first_exact_price_after_configuration_selection() -> None:
    page = _HuaweiPage()
    page.price_stale_polls = set(range(1, 21))

    result = _adapter().observe(_task(), page)

    assert result.price == Decimal("4999")
    assert page.price_poll == 1


def test_huawei_verified_capture_does_not_repeat_full_price_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe(task, page)

    def reject_full_business_reread(*_args: object) -> object:
        raise AssertionError("full Huawei business discovery must not repeat")

    monkeypatch.setattr(adapter, "_read_business_state", reject_full_business_reread)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    reader = adapter.verified_state_reader(task, page, observation.semantic_state)

    assert reader() == observation.semantic_state
    assert adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    ) == ()


def test_huawei_formal_capture_never_reads_the_numeric_price_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe(task, page)

    def reject_second_price_read(*_args: object) -> object:
        raise AssertionError("Huawei numeric price may only be read during observation")

    monkeypatch.setattr(adapter, "_current_price", reject_second_price_read)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    assert adapter.verified_state_reader(
        task,
        page,
        observation.semantic_state,
    )() == observation.semantic_state
    assert adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    ) == ()


def test_huawei_formal_capture_does_not_search_for_a_price_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe(task, page)
    module = importlib.import_module("quote_app.sites.official_brands.huawei")

    monkeypatch.setattr(
        module,
        "_visible_price_candidates",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("formal capture must not search for a VMALL price candidate")
        ),
    )

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    assert adapter.verified_state_reader(
        task,
        page,
        observation.semantic_state,
    )() == observation.semantic_state
    assert adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    ) == ()


def test_huawei_waits_for_visual_settle_before_formal_capture() -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe_for_capture(task, page)
    assert isinstance(observation, CaptureReadyObservation)

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert 1200 in page.wait_timeout_milliseconds


def test_huawei_capture_boundary_never_touches_the_page_after_configuration_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe_for_capture(task, page)
    assert isinstance(observation, CaptureReadyObservation)

    def reject_page_access(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "Huawei formal capture must not touch the page after configuration settles"
        )

    monkeypatch.setattr(page, "locator", reject_page_access)
    monkeypatch.setattr(page, "evaluate", reject_page_access)
    monkeypatch.setattr(page, "get_by_text", reject_page_access)
    monkeypatch.setattr(page, "wait_for_timeout", reject_page_access)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    reader = adapter.verified_state_reader(task, page, observation.semantic_state)

    assert reader() == observation.semantic_state
    assert (
        adapter.capture_rectangles_for_capture(
            task,
            page,
            observation.semantic_state,
        )
        == ()
    )


def test_huawei_post_capture_price_read_has_a_two_second_browser_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe_for_capture(task, page)
    assert isinstance(observation, CaptureReadyObservation)
    timeouts: list[float] = []
    monkeypatch.setattr(
        page,
        "set_default_timeout",
        lambda milliseconds: timeouts.append(milliseconds),
        raising=False,
    )

    completed = adapter.finalize_observation(task, page, observation)

    assert completed.price == Decimal("4999")
    assert timeouts == [2000, 30000]


def test_huawei_any_post_capture_price_exception_preserves_capture_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _HuaweiPage()
    observation = adapter.observe_for_capture(task, page)
    assert isinstance(observation, CaptureReadyObservation)
    monkeypatch.setattr(
        adapter,
        "_current_price",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stale VMALL price node")),
    )

    with pytest.raises(
        NonRetryableTechnicalError,
        match="截图成功",
    ) as captured:
        adapter.finalize_observation(task, page, observation)

    assert captured.value.code == "PRICE_UNAVAILABLE_AFTER_CAPTURE"


@pytest.mark.parametrize("drift", ["identity", "configuration"])
def test_huawei_price_wait_does_not_swallow_identity_or_configuration_drift(drift: str) -> None:
    page = _HuaweiPage()
    for node in page.price_nodes():
        node.attrs["hidden"] = ""
    page.price_visible_after_waits = 4
    if drift == "identity":
        page.identity_drift_after_price_waits = 2
    else:
        page.ambiguous_capacity_after_price_waits = 2
    with pytest.raises((LayoutRecognitionError, CaptureQualityError, NonRetryableTechnicalError)):
        _adapter().observe(_task(), page)
    assert page.price_waits == 2


def test_huawei_current_price_node_locks_its_first_visible_amount() -> None:
    page = _HuaweiPage()
    page.price_nodes()[0].text_parts = ["¥4999 ¥5199"]

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4999")


@pytest.mark.parametrize(
    ("outcome", "url"),
    [
        (BusinessOutcome.NO_MODEL, DETAIL),
        (BusinessOutcome.PRICE_FOUND, SEARCH),
        (BusinessOutcome.PRICE_FOUND, "https://www.vmall.com/product/no-id.html"),
    ],
)
def test_huawei_resume_rejects_checkpoint_route_for_wrong_outcome(
    outcome: BusinessOutcome,
    url: str,
) -> None:
    task = _task()
    checkpoint = _detail_checkpoint(task, outcome=outcome, url=url)
    with pytest.raises((LayoutRecognitionError, NonRetryableTechnicalError)):
        _adapter().resume(task, _HuaweiPage(), checkpoint)


def test_huawei_price_checkpoint_resumes_directly_without_home_search() -> None:
    task = _task()
    page = _HuaweiPage()
    result = _adapter().resume(task, page, _detail_checkpoint(task))
    assert result.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls == [DETAIL]
    assert page.fill_calls == [] and page.search_submissions == 0


def test_huawei_capture_keeps_current_scale_when_visual_proofs_already_fit() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.capture_scale == 1.0
    assert page.capture_scales == [] and page.light_scrolls == []


def test_huawei_capture_never_changes_scale_when_the_view_is_short() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    page.viewport_height = 340
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.capture_scale == 1.0
    assert page.capture_scales == []
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.capture_scales == []


def test_huawei_capture_never_scrolls_down_after_configuration() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    page.viewport_height = 280
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.light_scrolls == []


def test_huawei_capture_never_scrolls_up_after_configuration() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    page.viewport_height = 300
    page.scroll_offset = 120
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.light_scrolls == []


def test_huawei_capture_does_not_inspect_or_move_for_a_fixed_overlay() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    page.viewport_height = 340
    page.blocker = (280, 330, True)
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.light_scrolls == []


def test_huawei_persistent_overlay_or_missing_geometry_cannot_block_capture() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    page.viewport_height = 340
    page.blocker = (280, 330, False)
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.light_scrolls == []
    missing = _HuaweiPage()
    second = _adapter()
    state = second.observe(_task(), missing)
    missing.remove_color_geometry = True
    second.prepare_capture_view(_task(), missing, state.semantic_state)


def test_huawei_capture_never_scrolls_even_when_geometry_would_previously_fail() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    page.viewport_height = 300
    page.scroll_offset = 300
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    assert page.light_scrolls == []


def test_huawei_final_rectangle_read_does_not_query_capture_selectors() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    result = adapter.observe(_task(), page)
    adapter.prepare_capture_view(_task(), page, result.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        _task(),
        page,
        result.semantic_state,
    )
    assert page.capture_selector_arguments == []
    assert rectangles == ()

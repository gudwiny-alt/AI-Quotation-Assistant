from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from quote_app.sites.catalog import SUPPORTED_BRANDS, SiteSpec, load_site_catalog
from quote_app.sites.detail_capture_view import CaptureViewGeometryError
from quote_app.sites.jd import JDAdapter, _jd_color_matches
from quote_app.sites.protocol import SiteObservationAdapter
from quote_app.sites.registry import AdapterRegistry, RegisteredSiteAdapter
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import (
    LayoutRecognitionError,
    LoginRequired,
    NonRetryableTechnicalError,
    SecurityVerificationRequired,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sites" / "jd"
_ATTR = re.compile(
    r"^(?P<head>[a-zA-Z0-9_-]*)"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?P<classes>(?:\.[a-zA-Z0-9_-]+)*)"
    r"(?P<attrs>(?:\[[^\]]+\])*)$"
)
_ONE_ATTR = re.compile(
    r"\[(?P<name>[a-zA-Z0-9_-]+)"
    r"(?:(?P<operator>[*]?=)[\"']?(?P<value>[^\"'\]]+)[\"']?)?\]"
)


class _Node:
    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_Node] = []
        self.text_parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.text_parts + [child.text for child in self.children]).strip()

    def descendants(self) -> list[_Node]:
        result: list[_Node] = []
        for child in self.children:
            result.append(child)
            result.extend(child.descendants())
        return result

    @property
    def visible(self) -> bool:
        current: _Node | None = self
        while current is not None:
            style = _style(current.attrs.get("style", ""))
            classes = current.attrs.get("class", "").split()
            if (
                "hidden" in current.attrs
                or current.attrs.get("aria-hidden") == "true"
                or style.get("display") == "none"
                or style.get("visibility") == "hidden"
                or "hidden" in classes
            ):
                return False
            current = current.parent
        return True


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = _Node("document", {}, None)
        self.stack = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        node = _Node(
            tag,
            {name: "" if value is None else value for name, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)
        if tag not in {"input", "meta", "link", "img", "br", "hr"}:
            self.stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].text_parts.append(data)


class _Locator:
    def __init__(self, page: _FixturePage, nodes: list[_Node]) -> None:
        self.page = page
        self.nodes = nodes

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> _Locator:
        return _Locator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _Locator:
        matches: list[_Node] = []
        for node in self.nodes:
            matches.extend(_select(node.descendants(), selector))
        return _Locator(self.page, matches)

    def is_visible(self) -> bool:
        return len(self.nodes) == 1 and self.nodes[0].visible

    def inner_text(self) -> str:
        return self.nodes[0].text

    def get_attribute(self, name: str) -> str | None:
        self.page.advance_selection(self.nodes[0])
        self.page.advance_capacity_context(self.nodes[0], name)
        return self.nodes[0].attrs.get(name)

    def fill(self, value: str) -> None:
        self.nodes[0].attrs["value"] = value

    def input_value(self) -> str:
        return self.nodes[0].attrs.get("value", "")

    def click(self) -> None:
        node = self.nodes[0]
        option_kind = self.page.option_kind(node)
        if option_kind is not None:
            self.page.option_events.append(f"click:{option_kind}")
        if (
            "item" in node.attrs.get("class", "").split()
            or "specification-item-sku" in node.attrs.get("class", "").split()
        ):
            self.page.click_option(node)
        target = node.attrs.get("href")
        if target is None and node.parent is not None:
            target = node.parent.attrs.get("action")
        if target is None and node.tag == "input":
            current = node.parent
            while current is not None and current.tag != "form":
                current = current.parent
            if current is not None:
                target = current.attrs.get("action")
        if target is not None and target.startswith("#"):
            if target == "#results":
                self.page.search_submit_count += 1
            self.page.activate(target[1:])

    def scroll_into_view_if_needed(self) -> None:
        option_kind = self.page.option_kind(self.nodes[0])
        if option_kind is not None:
            self.page.option_scrolls.append(option_kind)
            self.page.option_events.append(f"scroll:{option_kind}")

    def bounding_box(self) -> dict[str, float] | None:
        style = _style(self.nodes[0].attrs.get("style", ""))
        if self.nodes[0].attrs.get("data-invalid-box") == "true":
            return None
        return {
            "x": _pixels(style.get("left"), 10),
            "y": _pixels(style.get("top"), 10),
            "width": _pixels(style.get("width"), 100),
            "height": _pixels(style.get("height"), 30),
        }

    def evaluate(self, script: str) -> dict[str, object] | str | bool:
        node = self.nodes[0]
        if "scrollIntoView" in script:
            option_kind = self.page.option_kind(node)
            if option_kind is not None:
                self.page.capture_view_positions.append(option_kind)
            elif node.attrs.get("id") == "key01":
                self.page.capture_view_positions.append("search")
                self.page.apply_result_title_after_search_anchor()
            elif "gl-item" in node.attrs.get("class", "").split():
                self.page.capture_view_positions.append("result-card")
            return ""
        if "getBoundingClientRect" in script:
            if node.attrs.get("id") == "key01":
                self.page.search_input_visibility_checks += 1
                if self.page.capture_position_failures > 0:
                    self.page.capture_position_failures -= 1
                    return False
            if "search-empty" in node.attrs.get("class", "").split():
                return self.page.empty_state_in_viewport
            return True
        if "parentElement?.innerText" in script:
            return node.parent.text if node.parent is not None else node.text
        color = "rgb(0, 0, 0)"
        effective_line_through = False
        current: _Node | None = node
        while current is not None:
            style = _style(current.attrs.get("style", ""))
            color = style.get("color", color)
            effective_line_through = effective_line_through or (
                "line-through" in style.get("text-decoration", "")
            )
            current = current.parent
        return {
            "color": color,
            "effectiveLineThrough": effective_line_through,
        }


class _FixturePage:
    def __init__(
        self,
        fixture: str = "normal.html",
        *,
        html: str | None = None,
        after_search_url: str = (
            "https://mall.jd.com/view_search-1000004123-99-1-24-1.html"
            "?keyword=%E5%B0%8F%E7%B1%B3%2015"
        ),
        after_search_urls: tuple[str, ...] | None = None,
        selection_mode: str = "immediate",
        price_snapshots: tuple[tuple[str, ...], ...] | None = None,
        price_sku_snapshots: tuple[tuple[str | None, ...], ...] | None = None,
        detail_redirect_url: str | None = None,
        capacity_context_mode: str = "immediate",
        store_controls_ready_after: int | None = None,
        result_region_ready_after: int | None = None,
        detail_ready_after: int | None = None,
        detail_seller_ready_after: int | None = None,
        modern_sku_ready_after_scroll: int | None = None,
        modern_exclusive_selection: bool = True,
        modern_price_after_selection: tuple[str, ...] | None = None,
        result_title_after_search_anchor: str | None = None,
        empty_state_in_viewport: bool = True,
        capture_position_failures: int = 0,
        capture_scale_samples: tuple[tuple[float, float], ...] | None = None,
    ) -> None:
        parser = _DocumentParser()
        parser.feed(html if html is not None else (FIXTURES / fixture).read_text("utf-8"))
        self.root = parser.root
        self._url = "about:blank"
        self._active = "store"
        self.after_search_url = after_search_url
        self.after_search_urls = after_search_urls
        self.after_search_url_index = 0
        self.goto_calls: list[str] = []
        self.selected_options: set[str] = set()
        self.option_scrolls: list[str] = []
        self.option_events: list[str] = []
        self.capture_view_positions: list[str] = []
        self.capture_scales: list[float] = []
        self.current_capture_scale = 1.0
        self.capture_scale_restore_count = 0
        self.scale_restored = False
        self.search_submit_count = 0
        self.search_input_visibility_checks = 0
        self.window_scroll_offsets: list[int] = []
        self.wait_timeout_milliseconds: list[float] = []
        self.selection_mode = selection_mode
        self.pending_selections: dict[_Node, int] = {}
        self.price_snapshots = price_snapshots
        self.price_sku_snapshots = price_sku_snapshots
        self.price_snapshot_reads = 0
        self.detail_redirect_url = detail_redirect_url
        self.capacity_context_mode = capacity_context_mode
        self.store_controls_ready_after = store_controls_ready_after
        self.result_region_ready_after = result_region_ready_after
        self.detail_ready_after = detail_ready_after
        self.detail_seller_ready_after = detail_seller_ready_after
        self.modern_sku_ready_after_scroll = modern_sku_ready_after_scroll
        self.modern_exclusive_selection = modern_exclusive_selection
        self.modern_price_after_selection = modern_price_after_selection
        self.result_title_after_search_anchor = result_title_after_search_anchor
        self.empty_state_in_viewport = empty_state_in_viewport
        self.capture_position_failures = capture_position_failures
        self.capture_scale_samples = capture_scale_samples
        self.capture_scale_sample_index = 0
        self.detail_scan_scrolls: list[int] = []
        self.pending_capacity_context: int | None = None
        self.pending_modern_price_update: int | None = None
        self.pending_modern_deselection: _Node | None = None
        self.color_access_before_capacity_context = False
        if capacity_context_mode in {"async", "never"}:
            for node in self.root.descendants():
                if "data-current-sku" in node.attrs:
                    node.attrs["data-current-sku"] = "999999999999"
        if modern_sku_ready_after_scroll is not None:
            for node in self.root.descendants():
                if "specification-item-sku" in node.attrs.get("class", "").split():
                    node.attrs["hidden"] = ""

    @property
    def url(self) -> str:
        return self._url

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)
        self._url = url
        if urlsplit(url).hostname == "item.jd.com":
            self.activate("product")
            if self.detail_redirect_url is not None:
                self._url = self.detail_redirect_url
        elif (
            urlsplit(url).hostname == "mall.jd.com"
            and urlsplit(url).path.startswith("/view_search-")
        ):
            self.activate("results")
        else:
            self._active = "store"

    def title(self) -> str:
        titles = _select(self.root.descendants(), "title")
        return titles[0].text if titles else ""

    def locator(self, selector: str) -> _Locator:
        if selector == '.choose-attrs .p-choose[data-type="color"] .item':
            current_skus = {
                node.attrs["data-current-sku"]
                for node in self.root.descendants()
                if "data-current-sku" in node.attrs
            }
            if current_skus != {"100012345678"}:
                self.color_access_before_capacity_context = True
        if (
            selector == ".summary-price .p-price"
            and self.selected_options != {"12GB + 256GB", "黑色"}
        ):
            return _Locator(self, [])
        screens = [
            node
            for node in self.root.descendants()
            if node.attrs.get("data-screen") == self._active
        ]
        scope = screens[0].descendants() + screens if screens else self.root.descendants()
        nodes = _select(scope, selector)
        if selector == ".summary-price .p-price" and (
            self.price_snapshots or self.price_sku_snapshots
        ):
            snapshot_index = min(
                self.price_snapshot_reads,
                max(
                    len(self.price_snapshots or ()),
                    len(self.price_sku_snapshots or ()),
                )
                - 1,
            )
            self.price_snapshot_reads += 1
            if self.price_snapshots:
                text_index = min(snapshot_index, len(self.price_snapshots) - 1)
                for node, text in zip(
                    nodes,
                    self.price_snapshots[text_index],
                    strict=False,
                ):
                    node.text_parts = [text]
            if self.price_sku_snapshots:
                sku_index = min(
                    snapshot_index,
                    len(self.price_sku_snapshots) - 1,
                )
                for node, sku in zip(
                    nodes,
                    self.price_sku_snapshots[sku_index],
                    strict=False,
                ):
                    if sku is None:
                        node.attrs.pop("data-sku", None)
                    else:
                        node.attrs["data-sku"] = sku
        return _Locator(self, nodes)

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_timeout(self, _milliseconds: float) -> None:
        self.wait_timeout_milliseconds.append(_milliseconds)
        if (
            self._active == "results"
            and self.after_search_urls is not None
            and self.after_search_url_index + 1 < len(self.after_search_urls)
        ):
            self.after_search_url_index += 1
            self._url = self.after_search_urls[self.after_search_url_index]
        if self.store_controls_ready_after is not None:
            self.store_controls_ready_after -= 1
            if self.store_controls_ready_after <= 0:
                for node in self.root.descendants():
                    if "data-delayed-store-control" in node.attrs:
                        node.attrs.pop("hidden", None)
                self.store_controls_ready_after = None
        if self.result_region_ready_after is not None:
            self.result_region_ready_after -= 1
            if self.result_region_ready_after <= 0:
                for node in self.root.descendants():
                    if "data-delayed-result-region" in node.attrs:
                        node.attrs.pop("hidden", None)
                self.result_region_ready_after = None
        if self.detail_ready_after is not None:
            self.detail_ready_after -= 1
            if self.detail_ready_after <= 0:
                for node in self.root.descendants():
                    if "data-delayed-detail" in node.attrs:
                        node.attrs.pop("hidden", None)
                self.detail_ready_after = None
        if self.detail_seller_ready_after is not None:
            self.detail_seller_ready_after -= 1
            if self.detail_seller_ready_after <= 0:
                for node in self.root.descendants():
                    if "data-delayed-detail-seller" in node.attrs:
                        node.attrs.pop("hidden", None)
                self.detail_seller_ready_after = None
        if self.pending_modern_price_update is not None:
            self.pending_modern_price_update -= 1
            if self.pending_modern_price_update <= 0:
                price_nodes = [
                    node
                    for node in self.root.descendants()
                    if {
                        "product-price--main",
                        "product-price--gray-line-through",
                    }
                    & set(node.attrs.get("class", "").split())
                ]
                assert self.modern_price_after_selection is not None
                assert len(price_nodes) == len(self.modern_price_after_selection)
                for node, text in zip(
                    price_nodes,
                    self.modern_price_after_selection,
                    strict=True,
                ):
                    node.text_parts = [text]
                self.pending_modern_price_update = None
        if self.pending_modern_deselection is not None:
            node = self.pending_modern_deselection
            node.attrs["aria-selected"] = "false"
            classes = set(node.attrs.get("class", "").split())
            classes.discard("specification-item-sku--selected")
            node.attrs["class"] = " ".join(sorted(classes))
            self.selected_options.discard(node.text)
            self.pending_modern_deselection = None
        return None

    def evaluate(
        self,
        script: str,
        value: float | None = None,
    ) -> bool | dict[str, str] | None:
        if "quotation-capture-scale" in script:
            if "root.removeAttribute" in script:
                self.scale_restored = True
                self.capture_scale_restore_count += 1
                self.current_capture_scale = 1.0
                return True
            assert value is not None
            self.capture_scales.append(value)
            self.current_capture_scale = value
            return self._capture_scale_sample()
        if "getComputedStyle" in script:
            return self._capture_scale_sample()
        if script == "() => window.scrollBy(0, -120)":
            self.window_scroll_offsets.append(-120)
        if script == "() => window.scrollBy(0, 520)":
            self.detail_scan_scrolls.append(520)
            if (
                self.modern_sku_ready_after_scroll is not None
                and len(self.detail_scan_scrolls)
                >= self.modern_sku_ready_after_scroll
            ):
                for node in self.root.descendants():
                    if "specification-item-sku" in node.attrs.get("class", "").split():
                        node.attrs.pop("hidden", None)
                self.modern_sku_ready_after_scroll = None

    def _capture_scale_sample(self) -> dict[str, str]:
        if self.capture_scale_samples is None:
            inline_zoom = computed_zoom = self.current_capture_scale
        else:
            inline_zoom, computed_zoom = self.capture_scale_samples[
                min(
                    self.capture_scale_sample_index,
                    len(self.capture_scale_samples) - 1,
                )
            ]
        self.capture_scale_sample_index += 1
        return {
            "inlineZoom": str(inline_zoom),
            "computedZoom": str(computed_zoom),
        }

    @staticmethod
    def option_kind(node: _Node) -> str | None:
        classes = set(node.attrs.get("class", "").split())
        if "specification-item-sku" in classes:
            return "modern-capacity" if "GB" in node.text else "modern-color"
        if "item" in classes:
            return "legacy-capacity" if "GB" in node.text else "legacy-color"
        return None

    def activate(self, screen: str) -> None:
        for node in self.root.descendants():
            current_screen = node.attrs.get("data-screen")
            if current_screen is None:
                continue
            if current_screen == screen:
                node.attrs.pop("hidden", None)
            else:
                node.attrs["hidden"] = ""
        self._active = screen
        if screen == "results":
            self._url = (
                self.after_search_urls[0]
                if self.after_search_urls is not None
                else self.after_search_url
            )
        elif screen == "product":
            self._url = "https://item.jd.com/100012345678.html"

    def apply_result_title_after_search_anchor(self) -> None:
        if self.result_title_after_search_anchor is None:
            return
        result_titles = _select(self.root.descendants(), ".p-name em")
        assert result_titles
        result_titles[0].text_parts = [self.result_title_after_search_anchor]
        self.result_title_after_search_anchor = None

    def click_option(self, node: _Node) -> None:
        if self.selection_mode == "never":
            return
        was_selected = self._node_is_selected(node)
        if self.selection_mode == "async":
            self.pending_selections[node] = 2
            return
        self._apply_selection(node)
        if not was_selected and self.option_kind(node).startswith("modern-"):
            if self.modern_price_after_selection is not None:
                self.pending_modern_price_update = 1
            if self.selection_mode == "non_stabilizing":
                self.pending_modern_deselection = node
        if "GB" in node.text and self.capacity_context_mode == "async":
            self.pending_capacity_context = 2

    def advance_selection(self, node: _Node) -> None:
        remaining = self.pending_selections.get(node)
        if remaining is None:
            return
        if remaining > 0:
            self.pending_selections[node] = remaining - 1
            return
        self._apply_selection(node)
        if (
            self.option_kind(node).startswith("modern-")
            and self.modern_price_after_selection is not None
        ):
            self.pending_modern_price_update = 1
        del self.pending_selections[node]

    @staticmethod
    def _node_is_selected(node: _Node) -> bool:
        return (
            node.attrs.get("aria-selected") == "true"
            or "specification-item-sku--selected"
            in node.attrs.get("class", "").split()
        )

    def _apply_selection(self, node: _Node) -> None:
        option_kind = self.option_kind(node)
        if option_kind.startswith("modern-") and self.modern_exclusive_selection:
            for sibling in self.root.descendants():
                if sibling is node or self.option_kind(sibling) != option_kind:
                    continue
                sibling.attrs["aria-selected"] = "false"
                sibling_classes = set(sibling.attrs.get("class", "").split())
                sibling_classes.discard("specification-item-sku--selected")
                sibling.attrs["class"] = " ".join(sorted(sibling_classes))
                self.selected_options.discard(sibling.text)
        node.attrs["aria-selected"] = "true"
        classes = set(node.attrs.get("class", "").split())
        if "specification-item-sku" in classes:
            classes.add("specification-item-sku--selected")
            node.attrs["class"] = " ".join(sorted(classes))
        self.selected_options.add(node.text)

    def advance_capacity_context(self, node: _Node, name: str) -> None:
        if name != "data-current-sku" or self.pending_capacity_context is None:
            return
        if self.pending_capacity_context > 0:
            self.pending_capacity_context -= 1
            return
        node.attrs["data-current-sku"] = "100012345678"
        self.pending_capacity_context = None


def _style(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in value.split(";"):
        if ":" in declaration:
            key, item = declaration.split(":", 1)
            result[key.strip().lower()] = item.strip()
    return result


def _pixels(value: str | None, default: float) -> float:
    if value is None:
        return default
    return float(value.removesuffix("px"))


def _select(nodes: list[_Node], selector: str) -> list[_Node]:
    parts = selector.strip().split()
    if not parts:
        return []
    matched = [node for node in nodes if _matches(node, parts[-1])]
    if len(parts) == 1:
        return matched
    result: list[_Node] = []
    for node in matched:
        wanted = parts[:-1]
        current = node.parent
        index = len(wanted) - 1
        while current is not None and index >= 0:
            if _matches(current, wanted[index]):
                index -= 1
            current = current.parent
        if index < 0:
            result.append(node)
    return result


def _matches(node: _Node, selector: str) -> bool:
    parsed = _ATTR.fullmatch(selector)
    if parsed is None:
        raise AssertionError(f"fixture harness does not support selector {selector!r}")
    tag = parsed.group("head")
    if tag and node.tag != tag:
        return False
    wanted_id = parsed.group("id")
    if wanted_id is not None and node.attrs.get("id") != wanted_id:
        return False
    wanted_classes = parsed.group("classes")
    if wanted_classes:
        actual_classes = set(node.attrs.get("class", "").split())
        if not set(wanted_classes.removeprefix(".").split(".")) <= actual_classes:
            return False
    for attribute in _ONE_ATTR.finditer(parsed.group("attrs")):
        name = attribute.group("name")
        if name not in node.attrs:
            return False
        operator = attribute.group("operator")
        wanted = attribute.group("value")
        if operator == "=" and node.attrs[name] != wanted:
            return False
        if operator == "*=" and wanted not in node.attrs[name]:
            return False
    return True


def _xiaomi_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.JD
    )


def _honor_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "HONOR" and spec.channel is WebsiteChannel.JD
    )


def _task(**changes: object) -> WebsiteTask:
    task = WebsiteTask(
        task_id="jd-1",
        run_id="run-1",
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-1",
        brand="小米",
        model_name="小米 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.JD,
    )
    return replace(task, **changes)


def _observe(
    fixture: str = "normal.html",
    *,
    task: WebsiteTask | None = None,
    **page_kwargs: object,
):
    return JDAdapter(_xiaomi_spec()).observe(
        task or _task(),
        cast(Any, _FixturePage(fixture, **page_kwargs)),
    )


def test_adapter_binds_exact_jd_spec_and_both_registry_surfaces() -> None:
    spec = _xiaomi_spec()
    adapter = JDAdapter(spec)

    assert adapter.spec is spec
    assert adapter.channel is WebsiteChannel.JD
    assert isinstance(adapter, RegisteredSiteAdapter)
    assert isinstance(adapter, SiteObservationAdapter)


def test_adapter_rejects_non_jd_spec_and_task_channel_or_brand_mismatch() -> None:
    spec = _xiaomi_spec()
    object.__setattr__(spec, "channel", WebsiteChannel.TMALL)
    with pytest.raises((TypeError, ValueError)):
        JDAdapter(spec)

    adapter = JDAdapter(_xiaomi_spec())
    with pytest.raises(ValueError, match="channel"):
        adapter.observe(
            _task(channel=WebsiteChannel.TMALL),
            cast(Any, _FixturePage()),
        )
    with pytest.raises(ValueError, match="brand"):
        adapter.observe(_task(brand="HONOR"), cast(Any, _FixturePage()))


def test_direct_execute_fails_without_fabricating_formal_evidence() -> None:
    adapter = JDAdapter(_xiaomi_spec())

    with pytest.raises(NonRetryableTechnicalError) as caught:
        adapter.execute(_task(), cast(Any, _FixturePage()), cast(Any, object()))

    assert caught.value.code == "ADAPTER_DIRECT_EXECUTION_UNSUPPORTED"
    assert caught.value.message == "站点适配器必须通过任务执行器生成正式截图"


@pytest.mark.parametrize(
    "url",
    [
        "https://passport.jd.com/new/login.aspx",
        "https://passport.jd.com/uc/login?returnurl=https%3A%2F%2Fmall.jd.com",
    ],
)
def test_login_redirect_is_never_reported_as_no_model(url: str) -> None:
    with pytest.raises(LoginRequired) as caught:
        _observe("no_model.html", after_search_url=url)

    assert caught.value.site == "jd"


def test_visible_login_page_is_never_reported_as_no_model() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<section id="J_goodsList"',
        '<form id="formlogin"><input id="loginname"></form><section id="J_goodsList"',
    )

    with pytest.raises(LoginRequired):
        _observe(html=html)


def test_visible_risk_control_page_requires_manual_verification() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<section id="J_goodsList"',
        '<div class="JDJRV-wrap">安全验证</div><section id="J_goodsList"',
    )

    with pytest.raises(SecurityVerificationRequired):
        _observe(html=html)


def test_visible_jd_frequency_control_text_requires_manual_verification() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<section id="J_goodsList"',
        '<div>访问过于频繁，请完成安全验证后重试</div>'
        '<section id="J_goodsList"',
    )

    with pytest.raises(SecurityVerificationRequired):
        _observe(html=html)


def test_visible_jd_loading_status_does_not_pause_a_verified_result_page() -> None:
    """A stale loading label must not turn a normal store result into a manual gate."""

    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<section id="J_goodsList"',
        '<div>努力加载中，请稍后...</div><section id="J_goodsList"',
    )

    page = _FixturePage(html=html)
    page.goto(page.after_search_url)
    observation = JDAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert observation.outcome is BusinessOutcome.NO_MODEL


def test_jd_no_model_uses_verified_search_url_when_search_input_is_transiently_hidden() -> None:
    """A ready search URL plus an inspected result region is sufficient evidence."""

    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<input id="key01" value="小米 15"',
        '<input hidden id="key01" value="小米 15"',
        1,
    )

    page = _FixturePage(html=html)
    page.goto(page.after_search_url)
    observation = JDAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rectangle.role for rectangle in observation.css_rectangles) == (
        "result_region",
    )


def test_jd_entry_layout_failure_is_labeled_as_store_page() -> None:
    html = "<title>小米京东自营旗舰店</title>"

    with pytest.raises(LayoutRecognitionError) as caught:
        _observe(html=html)

    assert caught.value.stage == "京东店铺页"


def test_jd_waits_for_store_search_controls_before_declaring_layout_changed() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8")
    html = html.replace(
        'id="key01" style=',
        'id="key01" data-delayed-store-control hidden style=',
        1,
    ).replace(
        'class="button01" type=',
        'class="button01" data-delayed-store-control hidden type=',
        1,
    )

    observation = _observe(html=html, store_controls_ready_after=1)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_waits_for_the_final_approved_store_search_url() -> None:
    observation = _observe(
        after_search_urls=(
            "https://mall.jd.com/intermediate-search",
            "https://mall.jd.com/"
            "view_search-1000004123-99-1-24-1.html?"
            "keyword=%E5%B0%8F%E7%B1%B3%2015",
        ),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_waits_for_the_result_region_after_search_url_is_stable() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'id="J_goodsList" style=',
        'id="J_goodsList" data-delayed-result-region hidden style=',
        1,
    )

    observation = _observe(html=html, result_region_ready_after=1)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_keeps_waiting_for_a_slow_live_store_result_container() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'id="J_goodsList" style=',
        'id="J_goodsList" data-delayed-result-region hidden style=',
        1,
    )

    observation = _observe(html=html, result_region_ready_after=11)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_accepts_the_live_store_result_list_container() -> None:
    """The HONOR JD store currently renders results in ``#comProlist``."""

    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'section id="J_goodsList"',
        'section id="comProlist"',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_accepts_the_unique_marketing_colour_for_a_generic_base_colour() -> None:
    """The base table may say 黑色 while the approved HONOR SKU says 幻夜黑."""

    assert _jd_color_matches("黑色", "幻夜黑")
    assert not _jd_color_matches("黑色", "雪原白")


def test_jd_modern_final_selection_accepts_the_selected_marketing_colour() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace("黑色", "幻夜黑")

    observation = _observe(
        "modern_detail_capacity_unavailable.html",
        html=html,
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_uses_verified_search_url_when_modern_result_search_box_is_blank() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<input id="key01" value="小米 15"',
        '<input id="key01" value=""',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_accepts_a_queryless_final_store_url_only_for_an_exact_result() -> None:
    observation = _observe(
        after_search_url=(
            "https://mall.jd.com/"
            "view_search-1000004123-99-1-24-1.html"
        ),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_deduplicates_matching_cards_that_share_one_approved_item_url() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '''        <article class="gl-item">
          <div class="p-name"><a href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15 12GB+256GB 手机</em></a></div>
        </article>''',
        '''        <article class="gl-item">
          <div class="p-name"><a href="//item.jd.com/100012345678.html"><em>新品 小米15 12GB+256GB 手机</em></a></div>
        </article>
        <article class="gl-item">
          <div class="p-name"><a href="//item.jd.com/100012345678.html"><em>新品 小米15 12GB+256GB 手机</em></a></div>
        </article>''',
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://item.jd.com/100012345678.html"


def test_jd_sold_out_selected_sku_with_bound_price_is_quoted() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'data-sku="100012345678">现货</div>',
        'data-sku="100012345678">暂时缺货</div>',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")
    assert observation.semantic_state.stock_state == "暂时缺货"


def test_jd_prefers_available_exact_model_card_over_sold_out_duplicate() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '''        <article class="gl-item">
          <div class="p-name"><a href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15 12GB+256GB 手机</em></a></div>
        </article>''',
        '''        <article class="gl-item">
          <div class="p-name"><a href="//item.jd.com/100012345679.html"><em>新品 小米15 12GB+256GB 手机</em></a></div>
          <span class="stock-state">暂时缺货</span>
        </article>
        <article class="gl-item">
          <div class="p-name"><a href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15 12GB+256GB 手机</em></a></div>
        </article>''',
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://item.jd.com/100012345678.html"


def test_jd_live_style_result_card_enters_base_model_detail_without_tracking_query() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8")
    html = html.replace('id="J_goodsList"', 'class="jSearchListArea"')
    html = html.replace('class="gl-item"', 'class="jItem"')
    html = html.replace(
        '//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15 12GB+256GB 手机',
        '//item.jd.com/100012345678.html?pcdk=fixture"><em>'
        '新品 小米15 12+256 黑色 第五代旗舰芯片 5G AI手机',
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://item.jd.com/100012345678.html"


def test_jd_result_card_uses_its_verified_visible_text_when_title_wrapper_changes() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<div class="p-name"><a href="//item.jd.com/100012345678.html" '
        'target="_blank"><em>新品 小米15 12GB+256GB 手机</em></a></div>',
        '<div class="listing-title"><a href="//item.jd.com/100012345678.html" '
        'target="_blank"><em>新品 小米15 12GB+256GB 手机</em></a></div>',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://item.jd.com/100012345678.html"


def test_modern_detail_selects_a_shortage_marked_requested_capacity() -> None:
    page = _FixturePage("modern_detail_capacity_unavailable.html")
    observation = JDAdapter(_xiaomi_spec()).observe(
        _task(ram="16GB", storage="512GB"),
        cast(Any, page),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4299")
    assert "click:modern-capacity" in page.option_events


def test_jd_waits_for_the_modern_detail_title_before_selecting_layout() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'class="page-right-skuname"',
        'class="page-right-skuname" data-delayed-detail hidden',
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
        detail_ready_after=1,
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_waits_for_modern_detail_seller_after_title_is_visible() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'class="shop-plugin"',
        'class="shop-plugin" data-delayed-detail-seller hidden',
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
        detail_seller_ready_after=2,
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_allows_a_slow_modern_detail_seller_to_finish_loading() -> None:
    """A live JD product must not be abandoned after the short store-page wait."""

    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'class="shop-plugin"',
        'class="shop-plugin" data-delayed-detail-seller hidden',
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
        detail_seller_ready_after=10,
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_modern_detail_rejects_unapproved_seller_name_suffix() -> None:
    """Break caught: a third-party suffix must not pass an approved-name prefix check."""
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        "小米京东自营旗舰店</div>",
        "小米京东自营旗舰店-第三方</div>",
        1,
    )

    with pytest.raises(LayoutRecognitionError, match="seller"):
        _observe(
            html=html,
            task=_task(ram="16GB", storage="512GB"),
        )


def test_jd_modern_detail_allows_only_known_store_ui_decorations() -> None:
    """The live shop header may append its own non-identity UI labels."""
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        "小米京东自营旗舰店</div>",
        "小米京东自营旗舰店 自营 关注店铺 进店逛逛</div>",
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_modern_detail_allows_the_observed_shop_widget_copy() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        "小米京东自营旗舰店</div>",
        "小米京东自营旗舰店 进店逛逛，享更多优惠 精选镇店好物，快来逛逛 "
        "关注店铺 联系客服</div>",
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_waits_for_legacy_detail_seller_after_title_is_visible() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'class="shop-name"',
        'class="shop-name" data-delayed-detail-seller hidden',
        1,
    )

    observation = _observe(
        html=html,
        detail_seller_ready_after=2,
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_jd_detail_seller_wait_surfaces_authentication_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'class="shop-name"',
        'class="shop-name" data-delayed-detail-seller hidden',
        1,
    )
    page = _FixturePage(html=html, detail_seller_ready_after=5)
    adapter = JDAdapter(_xiaomi_spec())
    checks = 0
    original = adapter._raise_if_authentication_blocked

    def authentication_check(candidate: object) -> None:
        nonlocal checks
        checks += 1
        if page.wait_timeout_milliseconds:
            raise LoginRequired("jd", "京东需要人工登录")
        original(candidate)

    monkeypatch.setattr(
        adapter,
        "_raise_if_authentication_blocked",
        authentication_check,
    )

    with pytest.raises(LoginRequired):
        adapter.observe(_task(), cast(Any, page))

    assert page.wait_timeout_milliseconds


def test_modern_detail_selects_available_configuration_and_reads_valid_price() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4299")
    assert observation.url == "https://item.jd.com/100012345678.html"


def test_jd_modern_positions_the_selected_detail_only_when_formal_capture_is_prepared() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    )
    page = _FixturePage(html=html)
    task = _task(ram="16GB", storage="512GB")
    adapter = JDAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))
    goto_count = len(page.goto_calls)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_events == [
        "scroll:modern-capacity",
        "click:modern-capacity",
        "scroll:modern-color",
        "click:modern-color",
    ]
    assert page.option_scrolls == ["modern-capacity", "modern-color"]
    assert page.capture_view_positions == []
    assert page.window_scroll_offsets == []

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.current_capture_scale == 0.8
    assert page.capture_view_positions == ["modern-capacity"]
    assert len(page.goto_calls) == goto_count
    assert 300 in page.wait_timeout_milliseconds


def test_jd_fixed_scale_is_kept_during_repeated_capture_preparation() -> None:
    """A screenshot retry must not restore and reapply JD's 80% page scale."""

    page = _FixturePage("no_model.html")
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert page.current_capture_scale == 0.8
    restores_before_capture = page.capture_scale_restore_count

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)
    adapter.restore_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.current_capture_scale == 0.8
    assert page.capture_scale_restore_count == restores_before_capture == 0


def test_jd_modern_detail_scans_down_before_abandoning_late_sku_options() -> None:
    """JD may render the valid SKU controls below the first viewport."""

    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        "16GB+512GB 无货",
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    )
    page = _FixturePage(html=html, modern_sku_ready_after_scroll=1)

    observation = JDAdapter(_xiaomi_spec()).observe(
        _task(ram="16GB", storage="512GB"),
        cast(Any, page),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.detail_scan_scrolls == [520]
    assert len(page.goto_calls) == 2
    assert page.goto_calls[-1] == "https://item.jd.com/100012345678.html"


def test_jd_resume_goes_directly_to_saved_detail_without_store_search() -> None:
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    first_page = _FixturePage()
    original = adapter.observe(task, cast(Any, first_page))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=original.price,
        url=original.url,
        observed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    resumed_page = _FixturePage()

    resumed = adapter.resume(task, cast(Any, resumed_page), checkpoint)

    assert resumed == original
    assert resumed_page.goto_calls == [checkpoint.url]


def test_jd_resume_revalidates_saved_no_model_search_without_search_submit() -> None:
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    original = adapter.observe(task, cast(Any, _FixturePage("no_model.html")))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=original.price,
        url=original.url,
        observed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    resumed_page = _FixturePage("no_model.html")

    resumed = adapter.resume(task, cast(Any, resumed_page), checkpoint)

    assert resumed == original
    assert resumed_page.goto_calls == [checkpoint.url]


def test_jd_reuses_the_current_matching_search_results_after_manual_pause() -> None:
    """A resolved manual pause must continue from the visible JD result page."""

    page = _FixturePage()
    page.goto(page.after_search_url)

    observation = JDAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls == [
        page.after_search_url,
        "https://item.jd.com/100012345678.html",
    ]


def test_jd_legacy_positions_the_selected_detail_only_when_formal_capture_is_prepared() -> None:
    page = _FixturePage()
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_events == [
        "scroll:legacy-capacity",
        "click:legacy-capacity",
        "scroll:legacy-color",
        "click:legacy-color",
    ]
    assert page.option_scrolls == ["legacy-capacity", "legacy-color"]
    assert page.capture_view_positions == []
    assert page.window_scroll_offsets == []

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.capture_view_positions == ["legacy-capacity"]
    assert 300 in page.wait_timeout_milliseconds


def test_honor_magic8_modern_result_enters_exact_item_and_reads_offer() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    )
    html = html.replace("小米京东自营旗舰店", "荣耀京东自营旗舰店")
    html = html.replace("小米 15", "荣耀Magic8")
    html = html.replace("小米15", "荣耀Magic8")
    html = html.replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku specification-item-sku--selected" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    )
    html = html.replace(
        "12GB+256GB</div>",
        "12GB+256GB</div>",
        1,
    ).replace(
        ">黑色</div>",
        ">天青釉</div>",
        1,
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Magic8",
        ram="16GB",
        storage="512GB",
        color="天青釉",
    )
    page = _FixturePage(
        html=html,
        after_search_url=(
            "https://mall.jd.com/view_search-1000000904-99-1-24-1.html"
            "?keyword=%E8%8D%A3%E8%80%80Magic8"
        ),
    )
    adapter = JDAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4299")
    assert observation.url == "https://item.jd.com/100012345678.html"
    assert adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )() == observation.semantic_state


def test_honor_store_entry_can_open_one_exact_item_without_legacy_search_form() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    )
    html = html.replace("小米京东自营旗舰店", "荣耀京东自营旗舰店")
    html = html.replace("小米 15", "荣耀Magic8")
    html = html.replace("小米15", "荣耀Magic8")
    html = re.sub(
        r'<div class="i-search">.*?</div>',
        (
            '<a class="entry-product" '
            'href="//item.jd.com/100012345678.html">'
            "荣耀Magic8 12GB+256GB 黑色 5G手机</a>"
        ),
        html,
        count=1,
        flags=re.DOTALL,
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Magic8",
    )
    page = _FixturePage(html=html)
    adapter = JDAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://item.jd.com/100012345678.html"
    assert page.goto_calls == [
        _honor_spec().entry_url,
        "https://item.jd.com/100012345678.html",
    ]


def test_modern_detail_reports_ineligible_subsidy_price_for_manual_completion() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    ).replace(
        '<div class="product-price-panel"><span class="product-price--main">¥4,299</span></div>',
        '<div class="product-price-panel"><span class="product-price--main">¥4,299</span>国补领后价</div>',
        1,
    )

    with pytest.raises(NonRetryableTechnicalError) as caught:
        _observe(html=html, task=_task(ram="16GB", storage="512GB"))

    assert caught.value.code == "NO_VALID_SELLING_PRICE"
    assert caught.value.message == "目标配置仅展示补贴价或划线原价，需人工补充"


def test_modern_detail_prefers_the_verified_struck_through_price_over_subsidy_price() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    ).replace(
        '<div class="product-price-panel"><span class="product-price--main">¥4,299</span></div>',
        '<div class="product-price-panel">'
        '<span class="product-price--main">¥4,299</span>国补领后价'
        '<span class="product-price--gray-line-through" style="text-decoration:line-through">¥4,499</span>'
        '</div>',
        1,
    )

    observation = _observe(
        html=html,
        task=_task(ram="16GB", storage="512GB"),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499")


def _honor_power2_modern_html() -> str:
    """Build the Honor Power2 modern-detail fixture before its target update."""

    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    )
    html = html.replace("小米京东自营旗舰店", "荣耀京东自营旗舰店")
    html = html.replace("小米 15", "荣耀Power2")
    html = html.replace("小米15", "荣耀Power2")
    html = html.replace(
        'class="specification-item-sku specification-item-sku--selected">'
        "12GB+256GB",
        "class='specification-item-sku specification-item-sku--selected'>12GB+256GB",
        1,
    )
    html = html.replace(
        ">黑色</div>",
        ">幻夜黑</div>",
        1,
    )
    html = html.replace(
        '      </section>\n'
        '      <div class="product-price-panel">',
        '        <div class="specification-item-sku '
        'specification-item-sku--selected">官方标配</div>\n'
        '      </section>\n'
        '      <div class="product-price-panel">',
    )
    html = html.replace(
        '<div class="product-price-panel">'
        '<span class="product-price--main">¥4,299</span></div>',
        '<div class="product-price-panel">'
        '<span class="product-price--main">¥3,699</span>国补领后价'
        '<span class="product-price--gray-line-through" '
        'style="text-decoration:line-through">¥4,499</span>'
        "</div>",
        1,
    )
    return html


def test_modern_power2_ignores_selected_official_package_when_binding_visible_configuration() -> None:
    html = _honor_power2_modern_html().replace(
        "class='specification-item-sku specification-item-sku--selected'>12GB+256GB",
        "class='specification-item-sku specification-item-sku--lack'>12GB+256GB",
        1,
    )
    page = _FixturePage(
        html=html,
        modern_exclusive_selection=False,
        modern_price_after_selection=("¥2,466.65", "¥2,999"),
    )
    page.after_search_url = (
        "https://mall.jd.com/view_search-1000000904-99-1-24-1.html"
        "?keyword=%E8%8D%A3%E8%80%80Power2"
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Power2",
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    observation = JDAdapter(_honor_spec()).observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("2999")


def test_modern_power2_rejects_duplicate_selected_matching_capacity() -> None:
    task = _task(
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    with pytest.raises(LayoutRecognitionError, match="configuration"):
        JDAdapter._require_exact_modern_configuration(
            task,
            (
                ("12GB+256GB", True),
                ("12GB+256GB", True),
                ("幻夜黑", True),
                ("官方标配", True),
            ),
        )


def test_modern_power2_rejects_selected_target_and_other_real_capacity() -> None:
    task = _task(
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    with pytest.raises(LayoutRecognitionError, match="configuration"):
        JDAdapter._require_exact_modern_configuration(
            task,
            (
                ("12GB+256GB", True),
                ("16GB+512GB", True),
                ("幻夜黑", True),
            ),
        )


def test_modern_power2_ignores_selected_package_and_service_labels() -> None:
    task = _task(
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    JDAdapter._require_exact_modern_configuration(
        task,
        (
            ("12GB+256GB", True),
            ("幻夜黑", True),
            ("官方标配", True),
            ("2年碎屏服务", True),
        ),
    )


def test_modern_power2_rejects_duplicate_selected_matching_colour() -> None:
    task = _task(
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    with pytest.raises(LayoutRecognitionError, match="configuration"):
        JDAdapter._require_exact_modern_configuration(
            task,
            (
                ("12GB+256GB", True),
                ("幻夜黑", True),
                ("幻夜黑", True),
                ("官方标配", True),
            ),
        )


def test_modern_exact_shortage_capacity_is_clicked_before_price_decision() -> None:
    html = _honor_power2_modern_html()
    html = html.replace(
        "specification-item-sku specification-item-sku--selected'>12GB+256GB",
        "specification-item-sku specification-item-sku--lack'>12GB+256GB 无货",
        1,
    )
    page = _FixturePage(
        html=html,
        modern_price_after_selection=("¥2,466.65", "¥2,999"),
    )
    page.after_search_url = (
        "https://mall.jd.com/view_search-1000000904-99-1-24-1.html"
        "?keyword=%E8%8D%A3%E8%80%80Power2"
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Power2",
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    observation = JDAdapter(_honor_spec()).observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("2999")
    assert "click:modern-capacity" in page.option_events


def test_modern_dual_selected_matching_capacity_cannot_bind_a_stale_global_price() -> None:
    html = _honor_power2_modern_html()
    html = html.replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        "16GB+512GB 无货",
        'specification-item-sku specification-item-sku--selected" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        "12GB+256GB",
        1,
    )
    page = _FixturePage(
        html=html,
        modern_exclusive_selection=False,
    )
    page.after_search_url = (
        "https://mall.jd.com/view_search-1000000904-99-1-24-1.html"
        "?keyword=%E8%8D%A3%E8%80%80Power2"
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Power2",
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    with pytest.raises(LayoutRecognitionError, match="ambiguous"):
        JDAdapter(_honor_spec()).observe(task, cast(Any, page))


def test_modern_transient_selection_cannot_stabilize_a_price_after_it_drops() -> None:
    html = _honor_power2_modern_html().replace(
        "specification-item-sku specification-item-sku--selected'>12GB+256GB",
        "specification-item-sku specification-item-sku--lack'>12GB+256GB 无货",
        1,
    )
    page = _FixturePage(
        html=html,
        selection_mode="non_stabilizing",
        modern_price_after_selection=("¥2,466.65", "¥2,999"),
    )
    page.after_search_url = (
        "https://mall.jd.com/view_search-1000000904-99-1-24-1.html"
        "?keyword=%E8%8D%A3%E8%80%80Power2"
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Power2",
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
    )

    with pytest.raises(LayoutRecognitionError, match="configuration|selected"):
        JDAdapter(_honor_spec()).observe(task, cast(Any, page))


def test_modern_price_found_exposes_a_live_formal_capture_reader() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    )
    task = _task(ram="16GB", storage="512GB")
    page = _FixturePage(html=html)
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert reader() == observation.semantic_state


def test_modern_formal_reader_accepts_marketing_colour_for_generic_base_colour() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text(
        "utf-8"
    ).replace(
        'specification-item-sku specification-item-sku--lack" '
        'style="left:20px;top:120px;width:150px;height:28px">'
        '16GB+512GB 无货',
        'specification-item-sku" '
        'style="left:20px;top:120px;width:150px;height:28px">16GB+512GB',
        1,
    ).replace(
        ">黑色</div>",
        ">幻夜黑</div>",
        1,
    )
    task = _task(ram="16GB", storage="512GB", color="黑色")
    page = _FixturePage(html=html)
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert reader() == observation.semantic_state


def test_jd_verified_state_reader_retries_transient_layout_errors_until_exact_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    page = _FixturePage()
    adapter = JDAdapter(_xiaomi_spec())
    expected = adapter.observe(task, cast(Any, page)).semantic_state
    page.wait_timeout_milliseconds.clear()
    reads = 0
    authentication_checks: list[str] = []

    def read_legacy(*_args: object) -> object:
        nonlocal reads
        reads += 1
        if reads < 3:
            raise LayoutRecognitionError("JD fixture is still hydrating")
        return expected

    monkeypatch.setattr(
        adapter,
        "_raise_if_authentication_blocked",
        lambda _page: authentication_checks.append("checked"),
    )
    monkeypatch.setattr(adapter, "_read_legacy_price_state", read_legacy)

    reader = adapter.verified_state_reader(task, cast(Any, page), expected)

    assert reader() == expected
    assert reads == 3
    assert authentication_checks == ["checked", "checked", "checked"]
    assert page.wait_timeout_milliseconds == [250, 250]


def test_jd_verified_state_reader_tolerates_non_quote_delivery_region_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    page = _FixturePage()
    adapter = JDAdapter(_xiaomi_spec())
    expected = adapter.observe(task, cast(Any, page)).semantic_state
    page.wait_timeout_milliseconds.clear()
    reads = 0
    authentication_checks: list[str] = []

    def read_legacy(*_args: object) -> object:
        nonlocal reads
        reads += 1
        return replace(expected, region="福建>福州>鼓楼")

    monkeypatch.setattr(
        adapter,
        "_raise_if_authentication_blocked",
        lambda _page: authentication_checks.append("checked"),
    )
    monkeypatch.setattr(adapter, "_read_legacy_price_state", read_legacy)

    reader = adapter.verified_state_reader(task, cast(Any, page), expected)

    assert reader() == expected

    assert reads == 1
    assert authentication_checks == ["checked"]
    assert page.wait_timeout_milliseconds == []


def test_jd_verified_state_reader_rejects_a_material_price_change_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    page = _FixturePage()
    adapter = JDAdapter(_xiaomi_spec())
    expected = adapter.observe(task, cast(Any, page)).semantic_state
    page.wait_timeout_milliseconds.clear()
    reads = 0

    def read_legacy(*_args: object) -> object:
        nonlocal reads
        reads += 1
        return replace(expected, price=Decimal("9999"))

    monkeypatch.setattr(adapter, "_read_legacy_price_state", read_legacy)

    reader = adapter.verified_state_reader(task, cast(Any, page), expected)

    with pytest.raises(
        LayoutRecognitionError,
        match="JD verified offer did not stabilize before capture",
    ):
        reader()

    assert reads == 10
    assert page.wait_timeout_milliseconds == [250] * 9


def test_jd_verified_state_reader_surfaces_authentication_block_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    page = _FixturePage()
    adapter = JDAdapter(_xiaomi_spec())
    expected = adapter.observe(task, cast(Any, page)).semantic_state
    reads = 0

    def raise_authentication_block(_page: object) -> None:
        raise SecurityVerificationRequired("jd", "京东需要人工完成安全验证")

    def read_legacy(*_args: object) -> object:
        nonlocal reads
        reads += 1
        return expected

    monkeypatch.setattr(adapter, "_raise_if_authentication_blocked", raise_authentication_block)
    monkeypatch.setattr(adapter, "_read_legacy_price_state", read_legacy)

    reader = adapter.verified_state_reader(task, cast(Any, page), expected)

    with pytest.raises(SecurityVerificationRequired):
        reader()

    assert reads == 0


def test_risk_control_redirect_requires_manual_verification() -> None:
    with pytest.raises(SecurityVerificationRequired):
        _observe(
            "no_model.html",
            after_search_url="https://cfe.m.jd.com/privatedomain/risk_handler",
        )


def test_target_blank_protocol_relative_item_link_is_navigated_on_controlled_page() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15',
        'href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15',
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://item.jd.com/100012345678.html"


@pytest.mark.parametrize(
    "href",
    [
        "#product",
        "https://evil.example/100012345678.html",
        "https://item.jd.com/not-a-number.html",
        "https://user:password@item.jd.com/100012345678.html",
        "https://item.jd.com/100012345678.html?access_token=secret",
        "",
    ],
)
def test_unapproved_or_credential_bearing_product_link_fails_closed(
    href: str,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15',
        f'href="{href}"><em>新品 小米15',
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    "href",
    [
        "https://item.jd.com:443/100012345678.html",
        "https://item.jd.com:444/100012345678.html",
        "https://item.jd.com:notaport/100012345678.html",
    ],
)
def test_item_link_with_explicit_or_malformed_port_fails_closed(
    href: str,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//item.jd.com/100012345678.html" target="_blank"><em>新品 小米15',
        f'href="{href}"><em>新品 小米15',
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    "seller_html",
    [
        '<div class="shop-name">第三方卖家</div>',
        "",
        '<div class="shop-name">小米京东自营旗舰店</div>'
        '<div class="shop-name">小米京东自营旗舰店</div>',
    ],
)
def test_detail_page_requires_one_exact_approved_seller(
    seller_html: str,
) -> None:
    html = re.sub(
        r'<div class="shop-name">.*?</div>',
        seller_html,
        (FIXTURES / "normal.html").read_text("utf-8"),
        count=1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://evil.example/100012345678.html",
        "https://item.jd.com/999999999999.html",
        "https://item.jd.com:443/100012345678.html",
        "https://item.jd.com/100012345678.html?access_token=secret",
    ],
)
def test_detail_redirect_must_remain_the_exact_approved_item_url(
    redirect_url: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(detail_redirect_url=redirect_url)


@pytest.mark.parametrize(
    "result_url",
    [
        "https://search.jd.com/Search?keyword=x",
        "https://evil.example/view_search-1.html",
        "https://mall.jd.com:443/view_search-1000004123-99-1-24-1.html",
        "https://mall.jd.com/not-store-search.html",
    ],
)
def test_result_url_must_be_approved_credential_free_store_search(
    result_url: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe("no_model.html", after_search_url=result_url)


@pytest.mark.parametrize(
    "result_url",
    [
        "https://mall.jd.com/view_search-1000004123-99-1-24-1.html"
        "?keyword=%E5%B0%8F%E7%B1%B3%2015",
        "https://mall.jd.com/view_search-1000004123-99-1-24-1.html"
        "?keyword=%25E5%25B0%258F%25E7%25B1%25B3%252015",
    ],
)
def test_store_search_accepts_one_or_two_percent_decoding_layers(
    result_url: str,
) -> None:
    observation = _observe("no_model.html", after_search_url=result_url)

    assert observation.outcome is BusinessOutcome.NO_MODEL


@pytest.mark.parametrize(
    "query",
    [
        "keyword=",
        "keyword=%E5%B0%8F%E7%B1%B3%2014",
        "keyword=%E5%B0%8F%E7%B1%B3%2015&keyword=%E5%B0%8F%E7%B1%B3%2015",
        "keyword=%E5%B0%8F%E7%B1%B3%2015&page=1",
        "keyword=%E5%B0%8F%E7%B1%B3%2015&access_token=secret",
        "keyword=%E0%A4%A",
        "keyword=%2525E5%2525B0%25258F",
    ],
)
def test_store_search_rejects_invalid_or_nonexact_keyword_query(
    query: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            "no_model.html",
            after_search_url=(
                "https://mall.jd.com/"
                "view_search-1000004123-99-1-24-1.html?"
                f"{query}"
            ),
        )


def test_store_search_rejects_keyword_fragment() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            "no_model.html",
            after_search_url=(
                "https://mall.jd.com/"
                "view_search-1000004123-99-1-24-1.html"
                "?keyword=%E5%B0%8F%E7%B1%B3%2015#results"
            ),
        )


def test_conflicting_visible_store_name_fails_technically() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        "小米京东自营旗舰店</a>",
        "非批准店铺</a>",
        1,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_exact_model_normalizes_spacing_and_case_but_rejects_variants_and_accessories() -> None:
    observation = _observe()

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price is not None


def test_no_exact_model_has_exact_search_and_result_evidence_roles() -> None:
    observation = _observe("no_model.html")

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


@pytest.mark.parametrize(
    "result_html",
    [
        "",
        '<article class="unknown-card">无法识别的页面片段</article>',
    ],
)
def test_unrecognized_or_empty_result_structure_is_not_no_model(
    result_html: str,
) -> None:
    html = re.sub(
        r'(<section id="J_goodsList"[^>]*>).*?(</section>)',
        rf"\1{result_html}\2",
        (FIXTURES / "no_model.html").read_text("utf-8"),
        flags=re.DOTALL,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_explicit_visible_empty_result_state_is_legal_no_model() -> None:
    html = re.sub(
        r'(<section id="J_goodsList"[^>]*>).*?(</section>)',
        r'\1<div class="search-empty">未找到相关商品</div>\2',
        (FIXTURES / "no_model.html").read_text("utf-8"),
        flags=re.DOTALL,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.NO_MODEL


@pytest.mark.parametrize(
    ("fixture", "outcome", "role"),
    [
        ("capacity_disabled.html", BusinessOutcome.CAPACITY_UNAVAILABLE, "capacity"),
        ("color_disabled.html", BusinessOutcome.COLOR_UNAVAILABLE, "color"),
    ],
)
def test_legal_no_variant_states_use_the_exact_target_rectangle(
    fixture: str,
    outcome: BusinessOutcome,
    role: str,
) -> None:
    observation = _observe(fixture)

    assert observation.outcome is outcome
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (role,)


@pytest.mark.parametrize(
    "fixture",
    (
        "no_model.html",
        "capacity_disabled.html",
        "color_disabled.html",
    ),
)
def test_legal_no_exposes_a_live_formal_capture_reader(
    fixture: str,
) -> None:
    task = _task()
    page = _FixturePage(fixture)
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert reader() == observation.semantic_state


@pytest.mark.parametrize(
    "replacement",
    [
        'data-context-sku="999999999999"',
        "",
    ],
)
def test_color_unavailable_requires_confirmed_capacity_context_binding(
    replacement: str,
) -> None:
    html = (FIXTURES / "color_disabled.html").read_text("utf-8").replace(
        'data-sku="100012345678" data-context-sku="100012345678" style=',
        f'data-sku="100012345678" {replacement} style=',
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_normal_selects_exact_variant_and_highest_valid_current_sku_price() -> None:
    observation = _observe()

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert str(observation.price) == "4399"
    assert observation.css_rectangles == ()
    assert observation.url == "https://item.jd.com/100012345678.html"
    assert observation.semantic_state.current_sku == "100012345678"
    assert observation.semantic_state.region == "福建>福州>台江"
    assert observation.semantic_state.stock_state == "现货"


@pytest.mark.parametrize("stock_text", [None, "库存信息加载中"])
def test_price_found_requires_recognized_actual_stock_state(
    stock_text: str | None,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8")
    stock = (
        '<div class="stock-state" '
        'data-sku="100012345678">现货</div>'
    )
    replacement = (
        ""
        if stock_text is None
        else (
            '<div class="stock-state" '
            f'data-sku="100012345678">{stock_text}</div>'
        )
    )

    with pytest.raises(LayoutRecognitionError, match="stock"):
        _observe(html=html.replace(stock, replacement))


def test_price_found_requires_stock_bound_to_current_sku() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<div class="stock-state" data-sku="100012345678">现货</div>',
        '<div class="stock-state" data-sku="999999999999">现货</div>',
    )

    with pytest.raises(LayoutRecognitionError, match="stock.*SKU|SKU.*stock"):
        _observe(html=html)


def test_async_selected_state_is_verified_before_reading_current_sku_price() -> None:
    observation = _observe(selection_mode="async")

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert str(observation.price) == "4399"


def test_capacity_context_may_update_asynchronously_before_color_is_read() -> None:
    page = _FixturePage(capacity_context_mode="async")

    observation = JDAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert not page.color_access_before_capacity_context


def test_capacity_context_that_never_updates_fails_before_color_is_read() -> None:
    page = _FixturePage(capacity_context_mode="never")

    with pytest.raises(LayoutRecognitionError):
        JDAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert not page.color_access_before_capacity_context


def test_click_without_approved_selected_state_fails_closed() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(selection_mode="never")


def test_price_must_reach_a_bounded_stable_snapshot_after_variant_selection() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            price_snapshots=(
                ("¥4,099", "¥4,199", "¥9,999"),
                ("¥4,199", "¥4,299", "¥9,999"),
                ("¥4,299", "¥4,399", "¥9,999"),
                ("¥4,399", "¥4,499", "¥9,999"),
                ("¥4,499", "¥4,599", "¥9,999"),
            )
        )


def test_stale_price_snapshots_are_ignored_until_current_sku_binding_is_stable() -> None:
    observation = _observe(
        price_snapshots=(
            ("¥4,099", "¥4,199", "¥9,999"),
            ("¥4,099", "¥4,199", "¥9,999"),
            ("¥4,299", "¥4,399", "¥9,999"),
            ("¥4,299", "¥4,399", "¥9,999"),
        ),
        price_sku_snapshots=(
            ("999999999999",) * 3,
            ("999999999999",) * 3,
            ("100012345678",) * 3,
            ("100012345678",) * 3,
        ),
    )

    assert str(observation.price) == "4399"


def test_forever_stale_price_sku_binding_fails_closed() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            price_sku_snapshots=(
                ("999999999999",) * 3,
                ("999999999999",) * 3,
            )
        )


def test_mixed_price_sku_bindings_fail_closed() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            price_sku_snapshots=(
                ("100012345678", "999999999999", "100012345678"),
            )
        )


def test_missing_price_sku_binding_fails_closed() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        ' data-sku="100012345678"',
        "",
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_coupon_installment_trade_in_and_css_line_through_prices_do_not_win() -> None:
    observation = _observe()

    assert str(observation.price) == "4399"


def test_selected_variant_without_valid_current_sku_price_fails_technically() -> None:
    html = re.sub(
        r'<section class="summary-price">.*?</section>',
        '<section class="summary-price"><span class="p-price">12期 ¥399</span></section>',
        (FIXTURES / "normal.html").read_text("utf-8"),
        flags=re.DOTALL,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda html: html.replace(
            '<input id="key01"',
            '<input id="key01"><input id="key01"',
            1,
        ),
        lambda html: html.replace(
            '<article class="gl-item">',
            '<article class="gl-item"><div class="p-name"><a href="#product">'
            "<em>小米 15 12GB+256GB 手机</em></a></div></article>"
            '<article class="gl-item">',
            1,
        ),
    ],
)
def test_duplicate_structural_or_exact_product_elements_fail_closed(
    mutation: Any,
) -> None:
    html = mutation((FIXTURES / "normal.html").read_text("utf-8"))
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_store_search_is_scoped_away_from_same_page_global_search_decoys() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<main data-screen="store">',
        '<main data-screen="store">'
        '<form class="global-search"><input id="key01">'
        '<input class="button01" type="submit" value="搜本店"></form>',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_generic_global_search_controls_cannot_replace_store_search() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<input id="key01"',
        '<input name="keyword"',
        1,
    ).replace(
        '<input class="button01" type="submit" value="搜本店">',
        '<button type="submit">搜索</button>',
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_result_page_store_conflict_fails_before_product_or_no_model_decision() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<main data-screen="results" hidden>',
        '<main data-screen="results" hidden>'
        '<div class="jLogo"><a class="logo-m">非批准店铺</a></div>',
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_price_found_requires_exact_result_page_search_keyword() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<input id="key01" value="小米 15" ',
        '<input id="key01" value="小米 14" ',
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_no_model_with_visible_result_cards_accepts_approved_query_when_input_is_blank(
) -> None:
    """JD sometimes renders the searched cards before restoring input.value."""

    html = (FIXTURES / "no_model.html").read_text("utf-8")
    target = (
        '<input id="key01" value="小米 15" '
        'style="left:20px;top:20px;width:260px;height:32px">'
    )
    position = html.rfind(target)
    assert position >= 0
    html = html[:position] + html[position:].replace(
        target,
        '<input id="key01" value="" '
        'style="left:20px;top:20px;width:260px;height:32px">',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "result_region",
    )


def test_jd_no_model_prepares_a_search_first_result_view_with_readable_card_names() -> None:
    page = _FixturePage("no_model.html")
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert page.current_capture_scale == 0.8
    assert page.capture_view_positions == ["search"]
    assert page.search_input_visibility_checks == 1
    assert 300 in page.wait_timeout_milliseconds
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        cast(Any, page),
        observation.semantic_state,
    )
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "search_keyword",
        "result_region",
    )


def test_jd_rejects_an_unstable_fixed_scale_without_restoring_to_100() -> None:
    page = _FixturePage(
        "no_model.html",
        capture_scale_samples=((0.8, 1.0), (0.8, 1.0)),
    )
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    with pytest.raises(
        LayoutRecognitionError,
        match="capture scale 0.8 did not become visually stable",
    ):
        adapter.observe(task, cast(Any, page))

    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 0


def test_jd_no_model_capture_retry_keeps_the_fixed_scale() -> None:
    page = _FixturePage(
        "no_model.html",
        capture_position_failures=1,
    )
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))
    page.search_submit_count = 0
    goto_count = len(page.goto_calls)

    def observe_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("capture preparation retry must not observe")

    adapter.observe = cast(Any, observe_must_not_run)

    with pytest.raises(CaptureViewGeometryError, match="search input"):
        adapter.prepare_capture_view(
            task,
            cast(Any, page),
            observation.semantic_state,
        )
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert page.search_submit_count == 0
    assert len(page.goto_calls) == goto_count
    assert page.current_capture_scale == 0.8
    assert page.capture_scale_restore_count == 0
    assert 500 not in page.wait_timeout_milliseconds
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "search_keyword",
        "result_region",
    )


@pytest.mark.parametrize(
    ("failure_kind", "message"),
    [
        ("url", "search URL changed"),
        ("store", "approved store identity is missing"),
        ("exact-product", "exact product appeared before no-model capture"),
        ("conflict", "result became conflicting before capture"),
    ],
)
def test_jd_no_model_semantic_failure_does_not_retry_capture_positioning(
    failure_kind: str,
    message: str,
) -> None:
    html: str | None = None
    if failure_kind == "conflict":
        html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
            '<section id="J_goodsList" '
            'style="left:10px;top:70px;width:780px;height:360px">',
            '<section id="J_goodsList" '
            'style="left:10px;top:70px;width:780px;height:360px">'
            '<div class="search-empty" hidden>未找到相关商品</div>',
        )
    page = _FixturePage("no_model.html", html=html)
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))
    waits_before = len(page.wait_timeout_milliseconds)

    if failure_kind == "url":
        page._url = f"{page.url}&page=2"
    elif failure_kind == "store":
        titles = _select(page.root.descendants(), "title")
        assert len(titles) == 1
        titles[0].text_parts = ["其他店铺 - 京东"]
    elif failure_kind == "exact-product":
        result_titles = _select(page.root.descendants(), ".p-name em")
        assert result_titles
        result_titles[0].text_parts = ["小米 15 12GB+256GB 手机"]
    else:
        empty_states = _select(page.root.descendants(), ".search-empty")
        assert len(empty_states) == 1
        empty_states[0].attrs.pop("hidden")

    with pytest.raises(LayoutRecognitionError, match=message):
        adapter.prepare_capture_view(
            task,
            cast(Any, page),
            observation.semantic_state,
        )

    capture_waits = page.wait_timeout_milliseconds[waits_before:]
    assert page.current_capture_scale == 0.8
    assert page.capture_scale_restore_count == 0
    assert capture_waits == []


def test_jd_no_model_rereads_the_first_product_title_after_search_positioning() -> None:
    page = _FixturePage(
        "no_model.html",
        result_title_after_search_anchor="小米 15 12GB+256GB 手机",
    )
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    waits_before = len(page.wait_timeout_milliseconds)

    with pytest.raises(LayoutRecognitionError, match="after no-model positioning"):
        adapter.prepare_capture_view(
            task,
            cast(Any, page),
            observation.semantic_state,
        )

    capture_waits = page.wait_timeout_milliseconds[waits_before:]
    assert page.current_capture_scale == 0.8
    assert page.capture_scale_restore_count == 0
    assert page.capture_view_positions == ["search"]
    assert 500 not in capture_waits


def test_jd_empty_no_model_capture_requires_the_empty_marker_in_viewport() -> None:
    html = re.sub(
        r'(<section id="J_goodsList"[^>]*>).*?(</section>)',
        r'\1<div class="search-empty">未找到相关商品</div>\2',
        (FIXTURES / "no_model.html").read_text("utf-8"),
        flags=re.DOTALL,
    )
    page = _FixturePage(
        html=html,
        empty_state_in_viewport=False,
    )
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    with pytest.raises(LayoutRecognitionError, match="empty state"):
        adapter.prepare_capture_view(
            task,
            cast(Any, page),
            observation.semantic_state,
        )

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert page.current_capture_scale == 0.8
    assert page.capture_scale_restore_count == 0


def test_jd_no_model_rereads_capture_rectangles_after_result_positioning() -> None:
    page = _FixturePage("no_model.html")
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert tuple(rectangle.role for rectangle in rectangles) == (
        "search_keyword",
        "result_region",
    )
    assert adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )() == observation.semantic_state


@pytest.mark.parametrize(
    ("role", "safe_stage"),
    [
        ("search_keyword", "搜索框定位"),
        ("result_region", "结果区域定位"),
    ],
)
def test_jd_no_model_capture_rectangle_failure_has_dedicated_geometry_stage(
    role: str,
    safe_stage: str,
) -> None:
    page = _FixturePage("no_model.html")
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(
        task,
        cast(Any, page),
        observation.semantic_state,
    )
    if role == "search_keyword":
        targets = [
            node
            for node in page.root.descendants()
            if node.attrs.get("id") == "key01" and node.visible
        ]
    else:
        targets = _select(page.root.descendants(), "#J_goodsList")
    assert len(targets) == 1
    targets[0].attrs["data-invalid-box"] = "true"

    with pytest.raises(CaptureViewGeometryError) as captured:
        adapter.capture_rectangles_for_capture(
            task,
            cast(Any, page),
            observation.semantic_state,
        )

    assert captured.value.safe_stage == safe_stage


@pytest.mark.parametrize("result_value", ["小米 14"])
def test_no_model_rejects_conflicting_result_page_search_keyword(
    result_value: str,
) -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8")
    target = (
        '<input id="key01" value="小米 15" '
        'style="left:20px;top:20px;width:260px;height:32px">'
    )
    position = html.rfind(target)
    assert position >= 0
    replacement = (
        f'<input id="key01" value="{result_value}" '
        'style="left:20px;top:20px;width:260px;height:32px">'
    )
    html = html[:position] + html[position:].replace(
        target,
        replacement,
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_invalid_bounding_box_prevents_unverifiable_legal_no() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        'id="J_goodsList"',
        'id="J_goodsList" data-invalid-box="true"',
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_credential_bearing_final_url_is_rejected_before_formal_capture_boundary() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            "no_model.html",
            after_search_url="https://mall.jd.com/search?access_token=secret",
        )


def test_default_registry_lazily_resolves_jd_adapter_for_all_seven_specs() -> None:
    registry = AdapterRegistry()
    adapters = [
        registry.adapter_for(brand, WebsiteChannel.JD)
        for brand in SUPPORTED_BRANDS
    ]

    assert all(isinstance(adapter, JDAdapter) for adapter in adapters)
    assert [adapter.spec.brand for adapter in adapters] == list(SUPPORTED_BRANDS)
    assert all(adapter.spec.channel is WebsiteChannel.JD for adapter in adapters)


def test_adapter_navigates_only_to_approved_entry_and_exact_jd_item_url() -> None:
    spec = _xiaomi_spec()
    page = _FixturePage()

    JDAdapter(spec).observe(_task(), cast(Any, page))

    assert page.goto_calls == [
        spec.entry_url,
        "https://item.jd.com/100012345678.html",
    ]

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
from quote_app.sites.protocol import SiteObservationAdapter
from quote_app.sites.registry import AdapterRegistry, RegisteredSiteAdapter
from quote_app.sites.tmall import (
    TmallAdapter,
    _approved_item_url,
    _approved_result_item_url,
    _tmall_color_matches,
)
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

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sites" / "tmall"
_SELECTOR = re.compile(
    r"^(?P<tag>[a-zA-Z0-9_-]*)"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?P<classes>(?:\.[a-zA-Z0-9_-]+)*)"
    r"(?P<attrs>(?:\[[^\]]+\])*)$"
)
_ATTRIBUTE = re.compile(
    r"\[(?P<name>[a-zA-Z0-9_-]+)"
    r"(?:(?P<operator>[\^*]?=)[\"']?(?P<value>[^\"'\]]+)[\"']?)?\]"
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
            styles = _style(current.attrs.get("style", ""))
            classes = current.attrs.get("class", "").split()
            if (
                "hidden" in current.attrs
                or current.attrs.get("aria-hidden") == "true"
                or styles.get("display") == "none"
                or styles.get("visibility") == "hidden"
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
        if selector == '[class^="highlightPrice--"]':
            self.page.apply_price_snapshot(matches)
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
        if any(
            class_name.startswith(("tmall-option", "valueItem--"))
            for class_name in node.attrs.get("class", "").split()
        ):
            option_kind = self.page.option_kind(node)
            if option_kind is not None:
                self.page.option_events.append(f"click:{option_kind}")
            self.page.click_option(node)
        if node.attrs.get("id") == "J_CurrShopBtn":
            self.page.activate("results")
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
            self.page.activate(target[1:])

    def scroll_into_view_if_needed(self) -> None:
        option_kind = self.page.option_kind(self.nodes[0])
        if option_kind is not None:
            self.page.option_scrolls.append(option_kind)
            self.page.option_events.append(f"scroll:{option_kind}")

    def bounding_box(self) -> dict[str, float] | None:
        styles = _style(self.nodes[0].attrs.get("style", ""))
        if self.nodes[0].attrs.get("data-invalid-box") == "true":
            return None
        return {
            "x": _pixels(styles.get("left"), 10),
            "y": _pixels(styles.get("top"), 10),
            "width": _pixels(styles.get("width"), 100),
            "height": _pixels(styles.get("height"), 30),
        }

    def evaluate(self, script: str) -> dict[str, object] | str | bool:
        node = self.nodes[0]
        if "scrollIntoView" in script:
            option_kind = self.page.option_kind(node)
            if option_kind is not None:
                self.page.capture_view_positions.append(option_kind)
            return ""
        if "getBoundingClientRect" in script:
            return True
        color = "rgb(0, 0, 0)"
        effective_line_through = False
        context_text: str | None = None
        current: _Node | None = node
        while current is not None:
            styles = _style(current.attrs.get("style", ""))
            color = styles.get("color", color)
            effective_line_through = effective_line_through or (
                "line-through" in styles.get("text-decoration", "")
            )
            if "tmall-current-selling" in current.attrs.get("class", "").split():
                context_text = current.text
            if any(
                class_name.startswith(("priceWrap--", "normalPrice--"))
                for class_name in current.attrs.get("class", "").split()
            ):
                context_text = current.text
            current = current.parent
        result: dict[str, object] = {
            "color": color,
            "effectiveLineThrough": effective_line_through,
        }
        if self.page.price_context_mode == "dom":
            result["contextText"] = context_text
        elif self.page.price_context_mode == "too_long":
            result["contextText"] = "售价" + ("很" * 1001)
        return result


class _FixturePage:
    def __init__(
        self,
        fixture: str = "normal.html",
        *,
        html: str | None = None,
        after_search_url: str = (
            "https://xiaomi.tmall.com/"
            "?q=%E5%B0%8F%E7%B1%B3%2015"
            "&type=p&search=y&newHeader_b=s"
            "&searcy_type=item&from=mallfp&spm=a1"
        ),
        selection_mode: str = "immediate",
        capacity_context_mode: str = "immediate",
        price_snapshots: tuple[tuple[str, ...], ...] | None = None,
        price_sku_snapshots: tuple[tuple[str | None, ...], ...] | None = None,
        detail_redirect_url: str | None = None,
        price_context_mode: str = "dom",
        final_selection_mode: str = "normal",
        stock_snapshots: tuple[str, ...] | None = None,
        capacity_transition_url: str | None = None,
        color_transition_url: str | None = None,
        poll_identity_mode: str = "normal",
        delayed_nodes_ready_after: int | None = None,
        store_ready_after: int | None = None,
        result_region_ready_after: int | None = None,
        detail_ready_after: int | None = None,
        risk_control_ready_after: int | None = None,
        capture_scale_samples: tuple[tuple[float, float], ...] | None = None,
    ) -> None:
        parser = _DocumentParser()
        source = (
            html
            if html is not None
            else (FIXTURES / fixture).read_text("utf-8")
        )
        if "slogo-shopname" not in source:
            source = _live_observed_html(fixture, source=source)
        parser.feed(source)
        self.root = parser.root
        self._url = "about:blank"
        self._active = "store"
        self.after_search_url = after_search_url
        self.selection_mode = selection_mode
        self.capacity_context_mode = capacity_context_mode
        self.price_snapshots = price_snapshots
        self.price_sku_snapshots = price_sku_snapshots
        self.detail_redirect_url = detail_redirect_url
        self.price_context_mode = price_context_mode
        self.final_selection_mode = final_selection_mode
        self.stock_snapshots = stock_snapshots
        self.stock_snapshot_reads = 0
        self.capacity_transition_url = capacity_transition_url
        self.color_transition_url = color_transition_url
        self.poll_identity_mode = poll_identity_mode
        self.delayed_nodes_ready_after = delayed_nodes_ready_after
        self.store_ready_after = store_ready_after
        self.result_region_ready_after = result_region_ready_after
        self.detail_ready_after = detail_ready_after
        self.risk_control_ready_after = risk_control_ready_after
        self.poll_waits = 0
        self.goto_calls: list[str] = []
        self.selected_options: set[str] = set()
        self.option_scrolls: list[str] = []
        self.option_events: list[str] = []
        self.capture_view_positions: list[str] = []
        self.capture_scales: list[float] = []
        self.capture_scale = 1.0
        self.capture_scale_restore_count = 0
        self.capture_scale_samples = capture_scale_samples
        self.capture_scale_sample_index = 0
        self.scale_restored = False
        self.window_scroll_offsets: list[int] = []
        self.wait_timeout_milliseconds: list[float] = []
        self.pending_selections: dict[_Node, int] = {}
        self.pending_capacity_context: int | None = None
        self.price_snapshot_reads = 0
        self.color_access_before_capacity_context = False
        if capacity_context_mode in {"async", "never"}:
            for node in self.root.descendants():
                if "data-current-sku" in node.attrs:
                    node.attrs["data-current-sku"] = "999999999999"

    @property
    def url(self) -> str:
        return self._url

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)
        self._url = url
        if urlsplit(url).hostname == "detail.tmall.com":
            self.activate("product")
            if self.detail_redirect_url is not None:
                self._url = self.detail_redirect_url
        elif urlsplit(url).hostname == "xiaomi.tmall.com" and urlsplit(url).query:
            self.activate("results")
        else:
            self._active = "store"

    def title(self) -> str:
        titles = _select(self.root.descendants(), "title")
        return titles[0].text if titles else ""

    def locator(self, selector: str) -> _Locator:
        if selector == '[class^="valueItem--"]':
            current_skus = {
                node.attrs["data-current-sku"]
                for node in self.root.descendants()
                if "data-current-sku" in node.attrs
            }
            if current_skus != {"123456789018"}:
                self.color_access_before_capacity_context = True
        if (
            selector
            == '#tbpcDetail_SkuPanelRightWrap [class^="highlightPrice--"]'
            and self.selected_options != {"12GB + 256GB", "黑色"}
        ):
            return _Locator(self, [])
        if selector == "body":
            return _Locator(self, _select(self.root.descendants(), selector))
        screens = [
            node
            for node in self.root.descendants()
            if node.attrs.get("data-screen") == self._active
        ]
        scope = screens[0].descendants() + screens if screens else self.root.descendants()
        nodes = _select(scope, selector)
        if selector == '[class^="skuWrapper--"]' and self.stock_snapshots and nodes:
            snapshot_index = min(
                self.stock_snapshot_reads,
                len(self.stock_snapshots) - 1,
            )
            self.stock_snapshot_reads += 1
            nodes[0].text_parts = [self.stock_snapshots[snapshot_index]]
        if (
            selector
            == '#tbpcDetail_SkuPanelRightWrap [class^="highlightPrice--"]'
            and (self.price_snapshots or self.price_sku_snapshots)
        ):
            self.apply_price_snapshot(nodes)
        return _Locator(self, nodes)

    def apply_price_snapshot(self, nodes: list[_Node]) -> None:
        if not nodes or not (self.price_snapshots or self.price_sku_snapshots):
            return
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
            sku_index = min(snapshot_index, len(self.price_sku_snapshots) - 1)
            for node, sku in zip(
                nodes,
                self.price_sku_snapshots[sku_index],
                strict=False,
            ):
                if sku is None:
                    node.attrs.pop("data-sku", None)
                else:
                    node.attrs["data-sku"] = sku

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_timeout(self, milliseconds: float) -> None:
        self.wait_timeout_milliseconds.append(milliseconds)
        if self.delayed_nodes_ready_after is not None:
            self.delayed_nodes_ready_after -= 1
            if self.delayed_nodes_ready_after <= 0:
                for node in self.root.descendants():
                    if "data-delayed-ready" in node.attrs:
                        node.attrs.pop("hidden", None)
                self.delayed_nodes_ready_after = None
        self._reveal_delayed_nodes("data-delayed-store", "store_ready_after")
        self._reveal_delayed_nodes(
            "data-delayed-result-region",
            "result_region_ready_after",
        )
        self._reveal_delayed_nodes("data-delayed-detail", "detail_ready_after")
        self._reveal_delayed_nodes(
            "data-delayed-risk-control",
            "risk_control_ready_after",
        )
        if (
            self.poll_identity_mode == "current_sku_changed"
            and self.poll_waits == 0
        ):
            marker = next(
                node
                for node in self.root.descendants()
                if "data-current-sku" in node.attrs
            )
            marker.attrs["data-current-sku"] = "999999999999"
        if self.poll_waits == 0 and self.poll_identity_mode in {
            "visible_capacity_changed",
            "visible_color_ambiguous",
            "title_changed",
        }:
            if self.poll_identity_mode == "title_changed":
                title = next(
                    node
                    for node in self.root.descendants()
                    if "ItemTitle--fixture" in node.attrs.get("class", "").split()
                )
                title.text_parts = ["小米 15 官方旗舰新品"]
                self.poll_waits += 1
                return
            options = [
                node
                for node in self.root.descendants()
                if any(
                    class_name.startswith(("tmall-option", "valueItem--"))
                    for class_name in node.attrs.get("class", "").split()
                )
            ]
            if self.poll_identity_mode == "visible_capacity_changed":
                next(node for node in options if node.text == "12GB + 256GB").attrs[
                    "aria-selected"
                ] = "false"
                next(node for node in options if node.text == "8GB + 256GB").attrs[
                    "aria-selected"
                ] = "true"
            else:
                next(node for node in options if node.text == "白色").attrs[
                    "aria-selected"
                ] = "true"
        self.poll_waits += 1

    def evaluate(
        self,
        script: str,
        value: float | None = None,
    ) -> dict[str, float] | bool | None:
        if (
            "quotation-capture-scale" in script
            or "computedZoom: getComputedStyle(root).zoom" in script
        ):
            if "root.removeAttribute" in script:
                self.capture_scale = 1.0
                self.capture_scale_restore_count += 1
                self.scale_restored = True
                return True
            if value is not None:
                self.capture_scale = value
                self.capture_scales.append(value)
                self.option_events.append(f"scale:{value}")
            return self._capture_scale_sample()
        if script == "() => window.scrollBy(0, -120)":
            self.window_scroll_offsets.append(-120)
        return None

    def _capture_scale_sample(self) -> dict[str, float]:
        if self.capture_scale_samples is None:
            inline_zoom = computed_zoom = self.capture_scale
        else:
            inline_zoom, computed_zoom = self.capture_scale_samples[
                min(
                    self.capture_scale_sample_index,
                    len(self.capture_scale_samples) - 1,
                )
            ]
        self.capture_scale_sample_index += 1
        return {
            "inlineZoom": inline_zoom,
            "computedZoom": computed_zoom,
        }

    def _reveal_delayed_nodes(self, attribute: str, counter: str) -> None:
        remaining = getattr(self, counter)
        if remaining is None:
            return
        remaining -= 1
        setattr(self, counter, remaining)
        if remaining > 0:
            return
        for node in self.root.descendants():
            if attribute in node.attrs:
                node.attrs.pop("hidden", None)
        setattr(self, counter, None)

    @staticmethod
    def option_kind(node: _Node) -> str | None:
        if not any(
            class_name.startswith(("tmall-option", "valueItem--"))
            for class_name in node.attrs.get("class", "").split()
        ):
            return None
        return "capacity" if "GB" in node.text else "color"

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
            self._url = self.after_search_url
        elif screen == "product":
            self._url = "https://detail.tmall.com/item.htm?id=123456789018"

    def click_option(self, node: _Node) -> None:
        if self.selection_mode == "never":
            return
        if self.selection_mode == "async":
            self.pending_selections[node] = 2
            return
        node.attrs["aria-selected"] = "true"
        self.selected_options.add(node.text)
        if "GB" in node.text and self.capacity_context_mode == "async":
            self.pending_capacity_context = 2
        if "GB" in node.text and self.capacity_transition_url is not None:
            self._url = self.capacity_transition_url
        if "GB" not in node.text and self.color_transition_url is not None:
            self._url = self.color_transition_url
        if node.text == "黑色":
            self.apply_final_selection_mutation()

    def advance_selection(self, node: _Node) -> None:
        remaining = self.pending_selections.get(node)
        if remaining is None:
            return
        if remaining > 0:
            self.pending_selections[node] = remaining - 1
            return
        node.attrs["aria-selected"] = "true"
        self.selected_options.add(node.text)
        del self.pending_selections[node]

    def advance_capacity_context(self, node: _Node, name: str) -> None:
        if name != "data-current-sku" or self.pending_capacity_context is None:
            return
        if self.pending_capacity_context > 0:
            self.pending_capacity_context -= 1
            return
        node.attrs["data-current-sku"] = "123456789018"
        self.pending_capacity_context = None

    def apply_final_selection_mutation(self) -> None:
        options = [
            node
            for node in self.root.descendants()
            if any(
                class_name.startswith(("tmall-option", "valueItem--"))
                for class_name in node.attrs.get("class", "").split()
            )
        ]
        capacity = next(node for node in options if node.text == "12GB + 256GB")
        color = next(node for node in options if node.text == "黑色")
        if self.final_selection_mode == "rerender":
            for stale in (capacity, color):
                assert stale.parent is not None
                replacement = _Node(
                    stale.tag,
                    dict(stale.attrs),
                    stale.parent,
                )
                replacement.text_parts = list(stale.text_parts)
                replacement.children = list(stale.children)
                for child in replacement.children:
                    child.parent = replacement
                index = stale.parent.children.index(stale)
                stale.parent.children[index] = replacement
                stale.attrs["data-sku"] = "999999999999"
        elif self.final_selection_mode == "capacity_deselected":
            capacity.attrs["aria-selected"] = "false"
        elif self.final_selection_mode == "multiple_capacity_selected":
            next(node for node in options if node.text == "8GB + 256GB").attrs[
                "aria-selected"
            ] = "true"
        elif self.final_selection_mode == "capacity_text_changed":
            capacity.text_parts = ["8GB + 256GB"]
        elif self.final_selection_mode == "color_context_changed":
            color.attrs["data-context-sku"] = "999999999999"


def _style(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in value.split(";"):
        if ":" in declaration:
            key, item = declaration.split(":", 1)
            result[key.strip().lower()] = item.strip()
    return result


def _pixels(value: str | None, default: float) -> float:
    return default if value is None else float(value.removesuffix("px"))


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
    parsed = _SELECTOR.fullmatch(selector)
    if parsed is None:
        raise AssertionError(f"fixture harness does not support selector {selector!r}")
    tag = parsed.group("tag")
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
    for attribute in _ATTRIBUTE.finditer(parsed.group("attrs")):
        name = attribute.group("name")
        if name not in node.attrs:
            return False
        operator = attribute.group("operator")
        wanted = attribute.group("value")
        if operator == "=" and node.attrs[name] != wanted:
            return False
        if operator == "*=" and wanted not in node.attrs[name]:
            return False
        if operator == "^=" and not node.attrs[name].startswith(wanted):
            return False
    return True


def _xiaomi_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.TMALL
    )


def _honor_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "HONOR" and spec.channel is WebsiteChannel.TMALL
    )


def _task(**changes: object) -> WebsiteTask:
    task = WebsiteTask(
        task_id="tmall-1",
        run_id="run-1",
        source_row_number=2,
        output_row_number=2,
        material_code="CODE-1",
        brand="小米",
        model_name="小米 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.TMALL,
    )
    return replace(task, **changes)


def _observe(fixture: str = "normal.html", **page_kwargs: object):
    return TmallAdapter(_xiaomi_spec()).observe(
        _task(),
        cast(Any, _FixturePage(fixture, **page_kwargs)),
    )


def _live_observed_html(
    fixture: str = "normal.html",
    *,
    source: str | None = None,
) -> str:
    html = (
        source
        if source is not None
        else (FIXTURES / fixture).read_text("utf-8")
    )
    html = re.sub(
        r'<header class="shop-header"><span class="shop-name">(.*?)</span></header>',
        r'<a class="slogo-shopname">\1</a>',
        html,
    )
    html = html.replace('<div class="shop-search">', "<div>")
    html = html.replace(
        '<form action="#results">',
        '<form name="searchTop" action="https://list.tmall.com/search_product.htm">',
    )
    html = html.replace(
        'class="shop-search-input"',
        'id="mq" class="s-combobox-input" name="q"',
    )
    html = re.sub(
        r'<input class="shop-search-submit" type="submit" value="搜本店">',
        '<button id="J_CurrShopBtn" class="currShopBtn" type="button">搜本店</button>',
        html,
    )
    html = html.replace(
        'class="tmall-shop-search-result"',
        'id="J_ShopSearchResult"',
    )
    html = html.replace('class="tmall-product-card"', 'class="item"')
    html = html.replace("<article ", "<dl ").replace("</article>", "</dl>")
    html = html.replace(
        'class="tmall-product-link"',
        'class="item-name J_TGoldData"',
    )
    html = html.replace('class="tmall-detail-seller"', 'class="shopName--fixture"')
    html = re.sub(
        r'<div class="shopName--fixture">(.*?)</div>',
        r'<span class="shopName--fixture">\1</span>',
        html,
    )
    html = html.replace('class="tmall-detail-title"', 'class="ItemTitle--fixture"')
    html = html.replace(
        'class="tmall-sku-context"',
        'id="SkuPanel_tbpcDetail_ssr2025"',
    )
    html = re.sub(
        r'(<section id="SkuPanel_tbpcDetail_ssr2025"[^>]*>)',
        r'\1<div id="skuOptionsArea">',
        html,
        count=1,
    )
    html = html.replace(
        '<div class="tmall-capacity-options">',
        '<div class="skuItem--fixture"><div class="ItemLabel--fixture">'
        "存储容量</div>",
    )
    html = html.replace(
        '<div class="tmall-color-options">',
        '<div class="skuItem--fixture"><div class="ItemLabel--fixture">'
        "机身颜色</div>",
    )
    html = html.replace('class="tmall-option', 'class="valueItem--fixture')
    html = html.replace(
        '</section>\n      <section class="tmall-current-selling">',
        '</div></section>\n      <section class="tmall-current-selling">',
        1,
    )
    html = html.replace(
        'class="tmall-current-selling"',
        'class="normalPrice--fixture"',
    )
    html = html.replace(
        '<section class="normalPrice--fixture">',
        '<section id="tbpcDetail_SkuPanelRightWrap">'
        '<div class="normalPrice--fixture">',
        1,
    )
    html = html.replace(
        '</section>\n      <div class="tmall-coupon-price">',
        '</div></section>\n      <div class="tmall-coupon-price">',
        1,
    )
    html = html.replace('class="tmall-selling-price"', 'class="highlightPrice--fixture"')
    html = html.replace('class="tmall-stock-status', 'class="skuWrapper--fixture')
    html = html.replace(
        "</main>",
        '<form name="SearchForm" '
        'action="https://xiaomi.tmall.com/search.htm?scene=taobao_shop"></form>'
        "</main>",
        1,
    )
    return html


_LIVE_RESULTS_URL = (
    "https://xiaomi.tmall.com/"
    "?q=%E5%B0%8F%E7%B1%B3%2015"
    "&type=p&search=y&newHeader_b=s"
    "&searcy_type=item&from=mallfp&spm=a1"
)
_LIVE_RESULTS_STATIC_QUERY = (
    "&type=p&search=y&newHeader_b=s"
    "&searcy_type=item&from=mallfp&spm=a1"
)


def _honor_power2_result_url() -> str:
    return (
        "https://hihonor.tmall.com/"
        "?q=%E8%8D%A3%E8%80%80Power2"
        + _LIVE_RESULTS_STATIC_QUERY
    )


def _without_hidden_sku_bindings(html: str) -> str:
    return re.sub(
        r'\sdata-(?:sku|context-sku|current-sku)="[^"]*"',
        "",
        html,
    )


def _normal_html_without_stock_or_delivery_region() -> str:
    return (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<div class="tmall-delivery-region">福建 &gt; 福州 &gt; 台江</div>',
        "",
    ).replace(
        '<div class="tmall-stock-status" data-sku="123456789018">现货</div>',
        "",
    )


def _with_ambiguous_selected_capacity(html: str) -> str:
    return html.replace(
        '<div class="tmall-capacity-options">',
        '<div class="tmall-capacity-options">'
        '<div class="tmall-option" aria-selected="true">8GB + 256GB</div>',
        1,
    )


def test_live_observed_store_search_results_and_product_selectors_drive_path() -> None:
    html = _live_observed_html().replace(
        "item.htm?id=123456789018",
        "item.htm?id=123456789018&rn=live-rn&abbucket=1",
        1,
    )

    observation = _observe(
        html=html,
        after_search_url=_LIVE_RESULTS_URL,
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_rich_product_card_title_enters_the_exact_base_model() -> None:
    html = _live_observed_html().replace(
        "新品 小米15 12GB+256GB 手机",
        "【政府补贴15%】小米15智能手机大电池第二代通信官方旗舰店",
        1,
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_honor_power2_marketing_detail_title_reaches_price_and_capture_stage() -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Power2")
    html = html.replace("小米15", "荣耀Power2")
    html = html.replace(
        '<h1 class="ItemTitle--fixture">荣耀Power2</h1>',
        '<h1 class="ItemTitle--fixture">【政府补贴15%】HONOR/荣耀Power2智能手机10080mAh官方旗舰店</h1>',
    )
    task = _task(brand="HONOR", model_name="荣耀Power2")
    page = _FixturePage(html=html, after_search_url=_honor_power2_result_url())
    adapter = TmallAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.capture_scales == [0.8]


def test_honor_power2_uses_stable_visible_configuration_without_hidden_sku_attributes(
) -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Power2")
    html = html.replace("小米15", "荣耀Power2")
    html = html.replace(
        '<h1 class="ItemTitle--fixture">荣耀Power2</h1>',
        '<h1 class="ItemTitle--fixture">'
        '【政府补贴15%】HONOR/荣耀Power2智能手机10080mAh官方旗舰店'
        "</h1>",
    )
    html = _without_hidden_sku_bindings(html)
    task = _task(brand="HONOR", model_name="荣耀Power2")
    page = _FixturePage(html=html, after_search_url=_honor_power2_result_url())
    adapter = TmallAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")
    assert observation.semantic_state.current_sku == "visible:12GB + 256GB|黑色"
    assert observation.semantic_state.region == "not-required-for-quotation"
    assert observation.semantic_state.stock_state == "not-required-for-quotation"


def test_honor_power2_variant_detail_title_does_not_match_power2_task() -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("小米 15", "荣耀Power2 Pro")
    html = html.replace("小米15", "荣耀Power2 Pro")
    page = _FixturePage(html=html)
    page.activate("product")

    with pytest.raises(LayoutRecognitionError, match="detail model does not match"):
        TmallAdapter(_honor_spec())._matching_detail_titles(
            cast(Any, page),
            _task(brand="HONOR", model_name="荣耀Power2"),
        )


@pytest.mark.parametrize(
    "variant_title",
    ("荣耀Power2-Pro", "荣耀Power2·Plus"),
)
def test_honor_power2_punctuated_variant_detail_title_is_not_the_base_model(
    variant_title: str,
) -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("小米 15", variant_title)
    html = html.replace("小米15", variant_title)
    page = _FixturePage(html=html)
    page.activate("product")

    with pytest.raises(LayoutRecognitionError, match="detail model does not match"):
        TmallAdapter(_honor_spec())._matching_detail_titles(
            cast(Any, page),
            _task(brand="HONOR", model_name="荣耀Power2"),
        )


def test_tmall_enters_an_exact_base_model_card_even_when_its_card_lists_other_sku_values() -> None:
    html = _live_observed_html().replace(
        "新品 小米15 12GB+256GB 手机",
        "小米15 16GB+512GB 蓝色 官方旗舰店",
        1,
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_store_controls_accept_live_store_page_form_action() -> None:
    """Search is executed by the bounded button, not by the form action URL."""
    html = _live_observed_html().replace(
        'action="https://xiaomi.tmall.com/search.htm?scene=taobao_shop"',
        'action="https://xiaomi.tmall.com/shop/view_shop.htm"',
        1,
    )
    page = _FixturePage(html=html)

    search_input, search_action = TmallAdapter(_xiaomi_spec())._wait_for_store_search_controls(
        cast(Any, page)
    )

    assert search_input.get_attribute("id") == "mq"
    assert search_action.get_attribute("id") == "J_CurrShopBtn"


def test_tmall_prefers_available_exact_model_card_over_sold_out_duplicate() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '''        <article class="tmall-product-card"><a class="tmall-product-link" href="//detail.tmall.com/item.htm?id=123456789018" target="_blank"><span class="tmall-product-title">新品 小米15 12GB+256GB 手机</span></a></article>''',
        '''        <article class="tmall-product-card"><a class="tmall-product-link" href="//detail.tmall.com/item.htm?id=123456789019"><span class="tmall-product-title">新品 小米15 12GB+256GB 手机</span></a><span class="stock-state">暂时缺货</span></article>
        <article class="tmall-product-card"><a class="tmall-product-link" href="//detail.tmall.com/item.htm?id=123456789018" target="_blank"><span class="tmall-product-title">新品 小米15 12GB+256GB 手机</span></a></article>''',
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_accepts_the_unique_marketing_colour_for_a_generic_base_colour() -> None:
    assert _tmall_color_matches("黑色", "幻夜黑")
    assert not _tmall_color_matches("黑色", "雪原白")


def test_tmall_uses_the_first_available_exact_model_card_when_the_store_lists_duplicates() -> None:
    """Live flagship-store results can repeat one exact model with offers."""

    html = _live_observed_html().replace(
        "item.htm?id=123456789018",
        "item.htm?id=123456789020",
        1,
    ).replace(
        "</section>",
        '<dl class="item"><a class="item-name J_TGoldData" '
        'href="//detail.tmall.com/item.htm?id=123456789018">'
        "新品 小米15 12GB+256GB 手机</a></dl></section>",
        1,
    )
    page = _FixturePage(html=html)
    page.activate("results")
    cards = tuple(
        page.locator("dl.item").nth(index)
        for index in range(page.locator("dl.item").count())
    )

    adapter = TmallAdapter(_xiaomi_spec())
    detail_url = adapter._exact_product_detail_url(
        adapter._exact_product_cards(cards, _task().model_name),
        base_url=_LIVE_RESULTS_URL,
    )

    assert detail_url == "https://detail.tmall.com/item.htm?id=123456789020"


def test_tmall_sold_out_selected_sku_with_bound_price_is_quoted() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'data-sku="123456789018">现货</div>',
        'data-sku="123456789018">已售罄</div>',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")
    assert observation.semantic_state.stock_state == "not-required-for-quotation"


def test_price_found_exposes_a_live_formal_capture_reader() -> None:
    task = _task()
    page = _FixturePage()
    adapter = TmallAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert reader() == observation.semantic_state


def test_honor_magic8_enters_exact_item_and_reads_offer() -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Magic8")
    html = html.replace("小米15", "荣耀Magic8")
    task = _task(
        brand="HONOR",
        model_name="荣耀Magic8",
    )
    page = _FixturePage(
        html=html,
        after_search_url=(
            "https://hihonor.tmall.com/"
            "?q=%E8%8D%A3%E8%80%80Magic8"
            "&type=p&search=y&newHeader_b=s"
            "&searcy_type=item&from=mallfp&spm=a1"
        ),
    )
    adapter = TmallAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"
    assert adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )() == observation.semantic_state


def test_honor_store_entry_can_open_one_exact_item_without_search_form() -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Magic8")
    html = html.replace("小米15", "荣耀Magic8")
    html = re.sub(
        r'<form name="searchTop".*?</form>',
        (
            '<a class="item-name J_TGoldData" '
            'href="//detail.tmall.com/item.htm?id=123456789018">'
            "新品 荣耀Magic8 12GB+256GB 手机</a>"
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
    adapter = TmallAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"
    assert page.goto_calls == [
        _honor_spec().entry_url,
        "https://detail.tmall.com/item.htm?id=123456789018",
    ]


def test_live_store_search_uses_the_bounded_button_not_form_action() -> None:
    html = _live_observed_html().replace(
        "https://xiaomi.tmall.com/search.htm?scene=taobao_shop",
        "https://list.tmall.com/search_product.htm?q=x",
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_live_store_search_ignores_overencoded_unused_form_action_scene() -> None:
    html = _live_observed_html().replace(
        "scene=taobao_shop",
        "scene=%2525",
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_adapter_binds_exact_tmall_spec_and_both_runtime_protocols() -> None:
    spec = _xiaomi_spec()
    adapter = TmallAdapter(spec)

    assert adapter.spec is spec
    assert adapter.channel is WebsiteChannel.TMALL
    assert isinstance(adapter, RegisteredSiteAdapter)
    assert isinstance(adapter, SiteObservationAdapter)


def test_adapter_rejects_non_tmall_spec_and_wrong_task_channel_or_brand() -> None:
    spec = _xiaomi_spec()
    object.__setattr__(spec, "channel", WebsiteChannel.JD)
    with pytest.raises((TypeError, ValueError)):
        TmallAdapter(spec)

    adapter = TmallAdapter(_xiaomi_spec())
    with pytest.raises(ValueError, match="channel"):
        adapter.observe(_task(channel=WebsiteChannel.JD), cast(Any, _FixturePage()))
    with pytest.raises(ValueError, match="brand"):
        adapter.observe(_task(brand="HONOR"), cast(Any, _FixturePage()))


def test_direct_execute_has_stable_nonretryable_failure() -> None:
    adapter = TmallAdapter(_xiaomi_spec())

    with pytest.raises(NonRetryableTechnicalError) as caught:
        adapter.execute(_task(), cast(Any, _FixturePage()), cast(Any, object()))

    assert caught.value.code == "ADAPTER_DIRECT_EXECUTION_UNSUPPORTED"
    assert caught.value.message == "站点适配器必须通过任务执行器生成正式截图"


@pytest.mark.parametrize(
    "url",
    [
        "https://login.tmall.com/",
        "https://login.taobao.com/member/login.jhtml",
    ],
)
def test_login_url_pauses_without_becoming_no_model(url: str) -> None:
    with pytest.raises(LoginRequired) as caught:
        _observe("no_model.html", after_search_url=url)

    assert caught.value.site == "tmall"
    assert caught.value.reason == "天猫需要人工登录"
    assert caught.value.retry_cost == 0


def test_saved_visible_login_fixture_pauses_for_user_action() -> None:
    with pytest.raises(LoginRequired):
        _observe("login_required.html")


def test_visible_password_login_overlay_pauses_for_user_action() -> None:
    html = """
    <div class="account-login-overlay">
      <input placeholder="账号名/邮箱/手机号" />
      <input placeholder="请输入登录密码" type="password" />
    </div>
    """

    with pytest.raises(LoginRequired) as caught:
        _observe(html=html)

    assert caught.value.site == "tmall"


def test_embedded_taobao_login_frame_pauses_for_user_action() -> None:
    html = '''
    <iframe src="https://login.taobao.com/member/login.jhtml?style=mini"></iframe>
    '''

    with pytest.raises(LoginRequired) as caught:
        _observe(html=html)

    assert caught.value.site == "tmall"
    assert caught.value.reason == "天猫需要人工登录"


def test_store_benefit_login_gate_pauses_before_searching() -> None:
    """A signed-out flagship-store landing page is still a login state."""

    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<form action="#results">',
        """<section class="member-benefit-gate">
          <p>登录后可查看完整店铺优惠权益</p>
          <button>立即登录</button>
        </section>
        <form action="#results">""",
        1,
    )
    page = _FixturePage(html=html)

    with pytest.raises(LoginRequired) as caught:
        TmallAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert caught.value.site == "tmall"
    assert caught.value.reason == "天猫需要人工登录"
    assert len(page.goto_calls) == 1


def test_delayed_embedded_login_frame_pauses_before_store_layout_failure() -> None:
    html = _live_observed_html("normal.html")
    html = html.replace(
        'id="mq"',
        'id="mq" data-delayed-ready hidden',
        1,
    ).replace(
        'id="J_CurrShopBtn"',
        'id="J_CurrShopBtn" data-delayed-ready hidden',
        1,
    ).replace(
        '<form name="SearchForm"',
        '''<iframe data-delayed-ready hidden
          src="https://login.taobao.com/member/login.jhtml?style=mini"></iframe>
        <form name="SearchForm"''',
        1,
    )

    page = _FixturePage(html=html, delayed_nodes_ready_after=1)
    search_form = page.locator('form[name="searchTop"]')
    assert not search_form.locator("#mq").is_visible()
    assert page.locator("iframe").count() == 1
    page.wait_for_timeout(1)
    assert page.locator("iframe").is_visible()
    assert page.locator('iframe[src*="login.taobao.com"]').is_visible()

    page = _FixturePage(html=html, delayed_nodes_ready_after=1)

    with pytest.raises(LoginRequired) as caught:
        TmallAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert caught.value.site == "tmall"
    assert page.wait_timeout_milliseconds == [500]


def test_tmall_waits_for_delayed_store_result_and_detail_layout() -> None:
    html = _live_observed_html()
    html = html.replace(
        'class="slogo-shopname"',
        'class="slogo-shopname" data-delayed-store hidden',
        1,
    ).replace(
        'id="J_ShopSearchResult"',
        'id="J_ShopSearchResult" data-delayed-result-region hidden',
        1,
    ).replace(
        'class="shopName--fixture"',
        'class="shopName--fixture" data-delayed-detail hidden',
        1,
    ).replace(
        'class="ItemTitle--fixture"',
        'class="ItemTitle--fixture" data-delayed-detail hidden',
        1,
    )
    page = _FixturePage(
        html=html,
        store_ready_after=1,
        result_region_ready_after=2,
        detail_ready_after=3,
    )

    observation = TmallAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert observation.price == Decimal("4399")
    assert page.wait_timeout_milliseconds[:3] == [500, 500, 500]


def test_tmall_delayed_result_risk_control_pauses_instead_of_layout_error() -> None:
    html = _live_observed_html().replace(
        'id="J_ShopSearchResult"',
        'id="J_ShopSearchResult" data-delayed-result-region hidden',
        1,
    ).replace(
        '<main data-screen="results" hidden>',
        '<main data-screen="results" hidden>'
        '<div class="tmall-security-check" data-delayed-risk-control hidden>'
        "请完成验证</div>",
        1,
    )
    page = _FixturePage(
        html=html,
        result_region_ready_after=5,
        risk_control_ready_after=1,
    )

    with pytest.raises(SecurityVerificationRequired) as caught:
        TmallAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert caught.value.site == "tmall"
    assert page.wait_timeout_milliseconds == [500]


def test_tmall_store_readiness_exhaustion_waits_nine_times_then_keeps_layout_error() -> None:
    html = _live_observed_html().replace(
        'class="slogo-shopname"',
        'class="slogo-shopname" data-delayed-store hidden',
        1,
    )
    page = _FixturePage(html=html)

    with pytest.raises(LayoutRecognitionError, match="approved store identity"):
        TmallAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert page.wait_timeout_milliseconds == [500] * 9


@pytest.mark.parametrize(
    "url",
    [
        "https://captcha.tmall.com/check",
        "https://sec.taobao.com/query.htm",
        "https://hihonor.tmall.com/shop/view_shop.htm/_____tmd_____/punish?x5step=1",
    ],
)
def test_risk_control_url_requires_manual_verification(url: str) -> None:
    with pytest.raises(SecurityVerificationRequired) as caught:
        _observe("no_model.html", after_search_url=url)

    assert caught.value.site == "tmall"
    assert caught.value.reason == "天猫需要人工完成安全验证"
    assert caught.value.retry_cost == 0


def test_visible_risk_control_marker_requires_manual_verification() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<section class="tmall-shop-search-result"',
        '<div class="tmall-security-check">请完成验证</div>'
        '<section class="tmall-shop-search-result"',
    )
    with pytest.raises(SecurityVerificationRequired):
        _observe(html=html)


def test_visible_system_error_is_technical_not_business_no() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        '<section class="tmall-shop-search-result"',
        '<div class="tmall-system-error">系统繁忙，请稍后再试</div>'
        '<section class="tmall-shop-search-result"',
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_exact_store_identity_is_required() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        "小米官方旗舰店</span>",
        "第三方手机店</span>",
        1,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_matching_title_without_visible_store_marker_fails_closed() -> None:
    html = re.sub(
        r'<header class="shop-header">.*?</header>',
        "",
        (FIXTURES / "normal.html").read_text("utf-8"),
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_conflicting_visible_store_marker_in_fallback_family_fails_closed() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<header class="shop-header"><span class="shop-name">'
        "小米官方旗舰店</span></header>",
        '<header class="shop-header"><span class="shop-name">'
        "小米官方旗舰店</span></header>"
        '<div class="slogo-shopname">第三方手机店</div>',
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_store_search_ignores_same_page_global_search_decoy() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<main data-screen="store">',
        '<main data-screen="store"><form class="global-search">'
        '<input class="shop-search-input">'
        '<input class="shop-search-submit" value="搜本店"></form>',
        1,
    )
    assert _observe(html=html).outcome is BusinessOutcome.PRICE_FOUND


def test_generic_global_controls_cannot_replace_bounded_store_search() -> None:
    html = _live_observed_html().replace(
        '<form name="searchTop"',
        '<form name="globalSearch"',
        1,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_result_page_store_conflict_fails_before_card_interpretation() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8")
    marker = '<span class="shop-name">小米官方旗舰店</span>'
    second = html.find(marker, html.find(marker) + 1)
    assert second >= 0
    html = html[:second] + html[second:].replace(
        marker,
        '<span class="shop-name">第三方手机店</span>',
        1,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    "result_url",
    [
        "https://xiaomi.tmall.com/"
        "?q=%E5%B0%8F%E7%B1%B3%2015&type=p&search=y&newHeader_b=s"
        "&searcy_type=item&from=mallfp&spm=a1",
        "https://xiaomi.tmall.com/"
        "?q=%25E5%25B0%258F%25E7%25B1%25B3%252015&type=p&search=y"
        "&newHeader_b=s&searcy_type=item&from=mallfp&spm=a1",
    ],
)
def test_store_search_accepts_one_or_two_strict_percent_decoding_layers(
    result_url: str,
) -> None:
    assert (
        _observe("no_model.html", after_search_url=result_url).outcome
        is BusinessOutcome.NO_MODEL
    )


@pytest.mark.parametrize(
    "encoded_keyword",
    [
        "%D0%A1%C3%D7%2015",
        "%25D0%25A1%25C3%25D7%252015",
    ],
)
def test_store_search_accepts_observed_exact_gbk_keyword_bytes(
    encoded_keyword: str,
) -> None:
    result_url = (
        f"https://xiaomi.tmall.com/?q={encoded_keyword}"
        + _LIVE_RESULTS_STATIC_QUERY
    )

    assert (
        _observe("no_model.html", after_search_url=result_url).outcome
        is BusinessOutcome.NO_MODEL
    )


def test_tmall_uses_verified_gbk_url_when_result_search_input_is_blank() -> None:
    html = _live_observed_html().replace(
        'name="q" value="小米 15"',
        'name="q" value=""',
        1,
    )
    result_url = (
        "https://xiaomi.tmall.com/?q=%D0%A1%C3%D7%2015"
        + _LIVE_RESULTS_STATIC_QUERY
    )

    observation = _observe(html=html, after_search_url=result_url)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_tmall_blank_result_input_can_record_a_verified_no_model_result() -> None:
    html = _live_observed_html().replace(
        'name="q" value="小米 15"',
        'name="q" value=""',
        1,
    ).replace("新品 小米15 12GB+256GB 手机", "新品 小米14 手机", 1)
    result_url = (
        "https://xiaomi.tmall.com/?q=%D0%A1%C3%D7%2015"
        + _LIVE_RESULTS_STATIC_QUERY
    )

    observation = _observe(html=html, after_search_url=result_url)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "result_region",
    )


def test_tmall_no_model_prepares_a_result_view_with_readable_card_names() -> None:
    page = _FixturePage("no_model.html")
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert page.capture_scales == []
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert page.capture_scales == [0.8]
    assert 300 in page.wait_timeout_milliseconds
    adapter.restore_capture_view(task, cast(Any, page), observation.semantic_state)
    assert page.scale_restored is True


def test_tmall_restores_original_scale_when_initial_visual_proof_fails() -> None:
    page = _FixturePage(
        "no_model.html",
        capture_scale_samples=((0.8, 1.0), (0.8, 1.0)),
    )
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    with pytest.raises(
        LayoutRecognitionError,
        match="capture scale 0.8 did not become visually stable",
    ):
        adapter.prepare_capture_view(
            task,
            cast(Any, page),
            observation.semantic_state,
        )

    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 1
    assert page.capture_scale == 1.0


def test_tmall_semantic_capture_failure_restores_scale_exactly_once() -> None:
    page = _FixturePage()
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))
    page._url = "https://detail.tmall.com/item.htm?id=999999999999"

    with pytest.raises(LayoutRecognitionError, match="detail URL changed"):
        adapter.prepare_capture_view(
            task,
            cast(Any, page),
            observation.semantic_state,
        )

    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 1
    assert page.capture_scale == 1.0


def test_tmall_no_model_rereads_capture_rectangles_after_result_positioning() -> None:
    page = _FixturePage("no_model.html")
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())
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
    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )
    assert reader() == observation.semantic_state


@pytest.mark.parametrize(
"result_url",
    [
        "https://xiaomi.tmall.com/?q=" + _LIVE_RESULTS_STATIC_QUERY,
        "https://xiaomi.tmall.com/?q=%E5%B0%8F%E7%B1%B3%2014"
        + _LIVE_RESULTS_STATIC_QUERY,
        "https://xiaomi.tmall.com/?q=%E5%B0%8F%E7%B1%B3%2015"
        "&q=%E5%B0%8F%E7%B1%B3%2015"
        + _LIVE_RESULTS_STATIC_QUERY,
        _LIVE_RESULTS_URL + "&page=1",
        _LIVE_RESULTS_URL + "&access_token=secret",
        "https://xiaomi.tmall.com/?q=%E0%A4%A" + _LIVE_RESULTS_STATIC_QUERY,
        "https://xiaomi.tmall.com/?q=%2525E5%2525B0%25258F"
        + _LIVE_RESULTS_STATIC_QUERY,
        "https://xiaomi.tmall.com:443/"
        "?q=%E5%B0%8F%E7%B1%B3%2015"
        + _LIVE_RESULTS_STATIC_QUERY,
        "https://user:password@xiaomi.tmall.com/"
        "?q=%E5%B0%8F%E7%B1%B3%2015"
        + _LIVE_RESULTS_STATIC_QUERY,
        _LIVE_RESULTS_URL + "#results",
        "https://list.tmall.com/"
        "?q=%E5%B0%8F%E7%B1%B3%2015"
        + _LIVE_RESULTS_STATIC_QUERY,
        "https://s.taobao.com/"
        "?q=%E5%B0%8F%E7%B1%B3%2015"
        + _LIVE_RESULTS_STATIC_QUERY,
        "https://xiaomi.tmall.com/search.htm"
        "?q=%E5%B0%8F%E7%B1%B3%2015"
        + _LIVE_RESULTS_STATIC_QUERY,
    ],
)
def test_store_search_url_rejects_unapproved_or_nonexact_shapes(
    result_url: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe("no_model.html", after_search_url=result_url)


@pytest.mark.parametrize("value", ["小米 14"])
def test_nonblank_result_input_must_prove_exact_keyword_before_price_or_no_model(
    value: str,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8")
    marker = 'class="shop-search-input" value="小米 15"'
    html = html.replace(marker, f'class="shop-search-input" value="{value}"', 1)
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_exact_model_rejects_pro_plus_ultra_and_accessory_cards() -> None:
    observation = _observe()

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert str(observation.price) == "4399"


@pytest.mark.parametrize(
    "result_html",
    ["", '<article class="unknown-card">未知布局</article>'],
)
def test_unknown_or_empty_layout_is_technical(result_html: str) -> None:
    html = re.sub(
        r'(<section class="tmall-shop-search-result"[^>]*>).*?(</section>)',
        rf"\1{result_html}\2",
        (FIXTURES / "no_model.html").read_text("utf-8"),
        flags=re.DOTALL,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_tmall_layout_error_identifies_the_failed_page_stage() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        "小米官方旗舰店",
        "未知店铺",
    )

    with pytest.raises(LayoutRecognitionError) as caught:
        _observe(html=html)

    assert caught.value.stage == "天猫店铺页"


def test_unobserved_synthetic_empty_state_is_technical() -> None:
    html = re.sub(
        r'(<section class="tmall-shop-search-result"[^>]*>).*?(</section>)',
        r'\1<div class="tmall-empty-result">未找到商品</div>\2',
        (FIXTURES / "no_model.html").read_text("utf-8"),
        flags=re.DOTALL,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_nonmatching_recognized_cards_have_exact_legal_no_roles() -> None:
    observation = _observe("no_model.html")

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )
    assert all(rect.width > 0 and rect.height > 0 for rect in observation.css_rectangles)


def test_target_blank_item_uses_controlled_existing_page_navigation() -> None:
    observation = _observe()

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_item_link_strips_observed_opaque_mi_id_tracking_key() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//detail.tmall.com/item.htm?id=123456789018" target="_blank"',
        'href="//detail.tmall.com/item.htm?id=123456789018&rn=fixture&'
        'abbucket=3&mi_id=opaque-tracking-key" target="_blank"',
        1,
    )

    observation = _observe(html=html)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_item_link_strips_the_observed_spm_tracking_key() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//detail.tmall.com/item.htm?id=123456789018" target="_blank"',
        'href="//detail.tmall.com/item.htm?id=123456789018&spm=fixture-tracking" '
        'target="_blank"',
        1,
    )

    observation = _observe(html=html)

    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_item_link_strips_the_observed_ali_tracking_keys() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//detail.tmall.com/item.htm?id=123456789018" target="_blank"',
        'href="//detail.tmall.com/item.htm?id=123456789018&'
        'ali_refid=fixture-ref&ali_trackid=fixture-track&bxsign=fixture-sign" '
        'target="_blank"',
        1,
    )

    observation = _observe(html=html)

    assert observation.url == "https://detail.tmall.com/item.htm?id=123456789018"


def test_selected_tmall_sku_properties_keeps_the_same_approved_item() -> None:
    """Selecting a colour can add Tmall's own SKU-properties query key."""

    assert _approved_item_url(
        "https://detail.tmall.com/item.htm?id=123456789018&"
        "sku_properties=5919063%3A6536025",
        base_url="https://detail.tmall.com/item.htm?id=123456789018",
    ) == "https://detail.tmall.com/item.htm?id=123456789018"


def test_tmall_color_selection_keeps_processing_on_the_same_item_url() -> None:
    observation = _observe(
        color_transition_url=(
            "https://detail.tmall.com/item.htm?id=123456789018&"
            "sku_properties=5919063%3A6536025"
        ),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith("sku_properties=5919063%3A6536025")


def test_tmall_capture_accepts_selected_sku_id_on_the_same_item() -> None:
    selected_url = (
        "https://detail.tmall.com/item.htm?id=123456789018&"
        "skuId=6174222558230"
    )
    page = _FixturePage(color_transition_url=selected_url)
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.url == selected_url
    assert page.capture_view_positions == ["capacity"]


@pytest.mark.parametrize(
    "href",
    [
        "#product",
        "https://evil.example/item.htm?id=123456789018",
        "https://detail.tmall.com/not-item.htm?id=123456789018",
        "https://detail.tmall.com/item.htm?id=not-a-number",
        "https://detail.tmall.com/item.htm",
        "https://detail.tmall.com/item.htm?id=123456789018&id=123456789018",
        "https://detail.tmall.com:443/item.htm?id=123456789018",
        "https://detail.tmall.com/item.htm?id=123456789018#sku",
        "https://user:password@detail.tmall.com/item.htm?id=123456789018",
        "",
    ],
)
def test_unapproved_product_href_fails_closed(href: str) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'href="//detail.tmall.com/item.htm?id=123456789018" target="_blank"',
        f'href="{href}"',
        1,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://evil.example/item.htm?id=123456789018",
        "https://detail.tmall.com/item.htm?id=999999999999",
        "https://detail.tmall.com:443/item.htm?id=123456789018",
        "https://detail.tmall.com/item.htm?id=123456789018&access_token=secret",
        "https://user:password@detail.tmall.com/item.htm?id=123456789018",
    ],
)
def test_detail_redirect_must_remain_exact_canonical_item(
    redirect_url: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(detail_redirect_url=redirect_url)


@pytest.mark.parametrize(
    "seller_html",
    [
        '<div class="tmall-detail-seller">第三方卖家</div>',
        "",
        '<div class="tmall-detail-seller">小米官方旗舰店</div>'
        '<div class="tmall-detail-seller">小米官方旗舰店</div>',
    ],
)
def test_detail_requires_one_exact_approved_seller(seller_html: str) -> None:
    html = re.sub(
        r'<div class="tmall-detail-seller">.*?</div>',
        seller_html,
        (FIXTURES / "normal.html").read_text("utf-8"),
        count=1,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_detail_model_is_revalidated() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<h1 class="tmall-detail-title">小米 15</h1>',
        '<h1 class="tmall-detail-title">小米 15 Pro</h1>',
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_two_bounded_live_detail_titles_are_accepted_only_when_both_match() -> None:
    title = '<h1 class="ItemTitle--fixture">小米 15</h1>'
    html = _live_observed_html().replace(
        title,
        title + '<div class="ItemTitle--fixture">小米 15</div>',
        1,
    )

    assert (
        _observe(html=html, after_search_url=_LIVE_RESULTS_URL).outcome
        is BusinessOutcome.PRICE_FOUND
    )


def test_multiple_matching_detail_title_wordings_share_one_model_identity() -> None:
    """A short title and a marketing title may prove the same clicked model."""

    title = '<h1 class="ItemTitle--fixture">小米 15</h1>'
    html = _live_observed_html().replace(
        title,
        title
        + '<div class="ItemTitle--fixture">'
        '【政府补贴15%】小米15智能手机官方旗舰店'
        "</div>",
        1,
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")


def test_nonmatching_auxiliary_live_detail_title_does_not_replace_matching_product_title() -> None:
    title = '<h1 class="ItemTitle--fixture">小米 15</h1>'
    html = _live_observed_html().replace(
        title,
        title + '<div class="ItemTitle--fixture">小米 15 Pro</div>',
        1,
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_tmall_accepts_a_verified_legacy_product_title_when_live_css_class_changes() -> None:
    """The product title is semantic evidence, not a generated CSS classname."""

    html = _live_observed_html().replace(
        'class="ItemTitle--fixture"',
        'class="tb-main-title"',
        1,
    )

    assert (
        _observe(html=html, after_search_url=_LIVE_RESULTS_URL).outcome
        is BusinessOutcome.PRICE_FOUND
    )


@pytest.mark.parametrize(
    ("fixture", "outcome", "role"),
    [
        ("capacity_disabled.html", BusinessOutcome.CAPACITY_UNAVAILABLE, "capacity"),
        ("color_disabled.html", BusinessOutcome.COLOR_UNAVAILABLE, "color"),
    ],
)
def test_legal_no_variant_states_use_exact_valid_target_rectangles(
    fixture: str,
    outcome: BusinessOutcome,
    role: str,
) -> None:
    observation = _observe(fixture)

    assert observation.outcome is outcome
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (role,)
    assert observation.css_rectangles[0].width > 0
    assert observation.css_rectangles[0].height > 0


@pytest.mark.parametrize(
    "fixture",
    ("no_model.html", "capacity_disabled.html", "color_disabled.html"),
)
def test_legal_no_exposes_a_live_formal_capture_reader(
    fixture: str,
) -> None:
    task = _task()
    page = _FixturePage(fixture)
    adapter = TmallAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert reader() == observation.semantic_state


@pytest.mark.parametrize(
    ("fixture", "outcome"),
    [
        ("capacity_disabled.html", BusinessOutcome.CAPACITY_UNAVAILABLE),
        ("color_disabled.html", BusinessOutcome.COLOR_UNAVAILABLE),
    ],
)
def test_honor_power2_promotional_title_survives_legal_no_capture_revalidation(
    fixture: str,
    outcome: BusinessOutcome,
) -> None:
    html = _live_observed_html(fixture)
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Power2")
    html = html.replace("小米15", "荣耀Power2")
    html = html.replace(
        '<h1 class="ItemTitle--fixture">荣耀Power2</h1>',
        '<h1 class="ItemTitle--fixture">'
        '【政府补贴15%】HONOR/荣耀Power2智能手机10080mAh官方旗舰店'
        "</h1>",
    )
    task = _task(brand="HONOR", model_name="荣耀Power2")
    page = _FixturePage(
        html=html,
        after_search_url=_honor_power2_result_url(),
    )
    adapter = TmallAdapter(_honor_spec())
    observation = adapter.observe(task, cast(Any, page))
    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )

    assert observation.outcome is outcome
    assert reader() == observation.semantic_state


def test_coarse_live_sku_wrapper_cannot_prove_sold_out_rectangle() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe("sold_out.html")


def test_unavailable_class_or_attribute_is_required_for_legal_disabled_state() -> None:
    html = (FIXTURES / "capacity_disabled.html").read_text("utf-8").replace(
        'class="tmall-option unavailable" aria-disabled="true"',
        'class="tmall-option"',
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize(
    ("fixture", "outcome"),
    [
        (
            "capacity_disabled.html",
            BusinessOutcome.CAPACITY_UNAVAILABLE,
        ),
        (
            "color_disabled.html",
            BusinessOutcome.COLOR_UNAVAILABLE,
        ),
    ],
)
def test_unavailable_variant_uses_visible_disabled_option_without_hidden_binding(
    fixture: str,
    outcome: BusinessOutcome,
) -> None:
    html = _without_hidden_sku_bindings(
        (FIXTURES / fixture).read_text("utf-8")
    )

    assert _observe(html=html).outcome is outcome


@pytest.mark.parametrize(
    "replacement",
    ['data-context-sku="999999999999"', ""],
)
def test_color_unavailable_ignores_hidden_capacity_context(
    replacement: str,
) -> None:
    html = (FIXTURES / "color_disabled.html").read_text("utf-8").replace(
        'data-context-sku="123456789018" style=',
        f"{replacement} style=",
        1,
    )
    assert _observe(html=html).outcome is BusinessOutcome.COLOR_UNAVAILABLE


def test_color_unavailable_rejects_ambiguous_visible_capacity_context() -> None:
    html = _with_ambiguous_selected_capacity(
        (FIXTURES / "color_disabled.html").read_text("utf-8")
    )

    with pytest.raises(
        LayoutRecognitionError,
        match="final selected capacity is missing, ambiguous, or changed",
    ):
        _observe(html=html)


def test_color_unavailable_capture_reader_rejects_ambiguous_visible_capacity() -> None:
    task = _task()
    page = _FixturePage("color_disabled.html")
    adapter = TmallAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))
    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )
    target = next(
        node
        for node in page.root.descendants()
        if node.text == "12GB + 256GB"
    )
    assert target.parent is not None
    other = _Node(
        "div",
        {"class": "valueItem--fixture", "aria-selected": "true"},
        target.parent,
    )
    other.text_parts = ["8GB + 256GB"]
    target.parent.children.insert(0, other)

    with pytest.raises(
        LayoutRecognitionError,
        match="final selected capacity is missing, ambiguous, or changed",
    ):
        reader()


@pytest.mark.parametrize(
    "capacity_transition_url",
    [
        "https://evil.example/item.htm?id=123456789018",
        "https://detail.tmall.com/not-item.htm?id=123456789018",
        "https://detail.tmall.com/item.htm?id=999999999999",
        "https://detail.tmall.com/item.htm?id=123456789018&foo=1",
        "https://detail.tmall.com:443/item.htm?id=123456789018",
        "https://detail.tmall.com/item.htm?id=123456789018#variant",
    ],
)
def test_capacity_transition_must_keep_exact_detail_url_before_disabled_color_no(
    capacity_transition_url: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(
            "color_disabled.html",
            capacity_transition_url=capacity_transition_url,
        )


def test_normal_selects_exact_variant_and_highest_bound_selling_price() -> None:
    observation = _observe()

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert str(observation.price) == "4399"
    assert observation.css_rectangles == ()
    assert observation.semantic_state.current_sku == "visible:12GB + 256GB|黑色"
    assert observation.semantic_state.region == "not-required-for-quotation"
    assert observation.semantic_state.stock_state == "not-required-for-quotation"


def test_tmall_price_and_capture_do_not_require_stock_or_delivery_region() -> None:
    html = _normal_html_without_stock_or_delivery_region()
    page = _FixturePage(html=html)
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")
    assert page.capture_scales == [0.8]
    assert page.capture_view_positions == ["capacity"]


def test_tmall_detail_scales_before_selection_and_positions_only_for_capture() -> None:
    page = _FixturePage(
        price_snapshots=(
            ("¥4,099", "¥4,399", "¥9,999"),
            ("¥4,199", "¥4,399", "¥9,999"),
            ("¥4,299", "¥4,399", "¥9,999"),
        )
    )
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_events == [
        "scale:0.8",
        "scroll:capacity",
        "click:capacity",
        "scroll:color",
        "click:color",
    ]
    assert page.option_scrolls == ["capacity", "color"]
    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 0
    assert page.capture_view_positions == []
    assert page.window_scroll_offsets == []

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 0
    assert page.capture_scale == 0.8
    assert page.capture_view_positions == ["capacity"]
    assert 300 in page.wait_timeout_milliseconds
    adapter.restore_capture_view(task, cast(Any, page), observation.semantic_state)
    assert page.capture_scale_restore_count == 1
    assert page.scale_restored is True


def test_tmall_detail_observation_failure_restores_early_scale_once() -> None:
    page = _FixturePage(
        price_snapshots=tuple(
            (
                f"¥{4_099 + (index * 100):,}",
                f"¥{4_199 + (index * 100):,}",
                "¥9,999",
            )
            for index in range(21)
        ),
    )
    adapter = TmallAdapter(_xiaomi_spec())

    with pytest.raises(
        LayoutRecognitionError,
        match="price did not reach a verified stable state",
    ):
        adapter.observe(_task(), cast(Any, page))

    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 1
    assert page.capture_scale == 1.0


def test_tmall_resume_goes_directly_to_saved_detail_without_store_search() -> None:
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())
    original = adapter.observe(task, cast(Any, _FixturePage()))
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


def test_tmall_resume_revalidates_saved_no_model_without_search_submit() -> None:
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())
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


def test_tmall_card_link_discards_unrecognized_nonempty_tracking_query_parameters() -> None:
    """A product card may add tracking data that is irrelevant after ID canonicalization."""

    assert _approved_result_item_url(
        "https://detail.tmall.com/item.htm?id=123456&pvid=live-card&"
        "trace=abc&wxid=runtime-card-token",
        base_url="https://xiaomi.tmall.com/",
    ) == "https://detail.tmall.com/item.htm?id=123456"


def test_async_selected_state_is_confirmed() -> None:
    assert _observe(selection_mode="async").outcome is BusinessOutcome.PRICE_FOUND


@pytest.mark.parametrize("capacity_context_mode", ["async", "never"])
def test_hidden_capacity_context_does_not_gate_visible_color_selection(
    capacity_context_mode: str,
) -> None:
    page = _FixturePage(capacity_context_mode=capacity_context_mode)

    observation = TmallAdapter(_xiaomi_spec()).observe(_task(), cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_rerendered_exact_selected_options_replace_stale_locators() -> None:
    observation = _observe(final_selection_mode="rerender")

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert str(observation.price) == "4399"


@pytest.mark.parametrize(
    "final_selection_mode",
    [
        "capacity_deselected",
        "multiple_capacity_selected",
        "capacity_text_changed",
    ],
)
def test_final_selected_dom_must_remain_unique_and_exact(
    final_selection_mode: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(final_selection_mode=final_selection_mode)


def test_hidden_current_sku_marker_change_does_not_override_visible_configuration() -> None:
    observation = _observe(poll_identity_mode="current_sku_changed")

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_visible_capacity_change_between_price_samples_fails_closed() -> None:
    with pytest.raises(
        LayoutRecognitionError,
        match="final selected capacity is missing, ambiguous, or changed",
    ):
        _observe(poll_identity_mode="visible_capacity_changed")


def test_ambiguous_visible_color_during_price_sampling_fails_closed() -> None:
    with pytest.raises(
        LayoutRecognitionError,
        match="final selected color is missing, ambiguous, or changed",
    ):
        _observe(poll_identity_mode="visible_color_ambiguous")


def test_matching_detail_title_wording_change_keeps_the_verified_model_identity() -> None:
    observation = _observe(poll_identity_mode="title_changed")

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")


def test_click_without_approved_selected_state_fails_closed() -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(selection_mode="never")


def test_old_old_new_new_visible_price_waits_for_post_transition_stability() -> None:
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
            ("123456789018",) * 3,
            ("123456789018",) * 3,
        ),
    )
    assert str(observation.price) == "4399"


def test_changing_auxiliary_prices_do_not_block_a_stable_selected_quotation() -> None:
    observation = _observe(
        price_snapshots=(
            ("¥4,099", "¥4,399", "¥9,999"),
            ("¥4,199", "¥4,399", "¥9,999"),
            ("¥4,299", "¥4,399", "¥9,999"),
            ("¥4,099", "¥4,399", "¥9,999"),
            ("¥4,199", "¥4,399", "¥9,999"),
        ),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")


def test_multiple_visible_current_price_containers_fail_closed() -> None:
    html = _live_observed_html()
    container = re.search(
        r'<section id="tbpcDetail_SkuPanelRightWrap">.*?</section>',
        html,
        flags=re.DOTALL,
    )
    assert container is not None
    html = html.replace(container.group(), container.group() * 2, 1)

    with pytest.raises(
        LayoutRecognitionError,
        match="current selling price container structural element is missing or ambiguous",
    ):
        _observe(html=html, after_search_url=_LIVE_RESULTS_URL)


def test_stable_visible_price_ignores_permanently_old_hidden_binding() -> None:
    observation = _observe(price_sku_snapshots=(("999999999999",) * 3,) * 5)

    assert str(observation.price) == "4399"


@pytest.mark.parametrize(
    "bindings",
    [
        ("123456789018", None, "123456789018"),
        ("123456789018", "999999999999", "123456789018"),
    ],
)
def test_missing_or_mixed_hidden_price_bindings_do_not_override_visible_price(
    bindings: tuple[str | None, ...],
) -> None:
    observation = _observe(
        price_sku_snapshots=(bindings,),
    )

    assert str(observation.price) == "4399"


def test_price_snapshot_must_stabilize_after_sku_identity() -> None:
    page = _FixturePage(
        price_snapshots=tuple(
            (
                f"¥{4_099 + (index * 100):,}",
                f"¥{4_199 + (index * 100):,}",
                "¥9,999",
            )
            for index in range(21)
        )
    )
    adapter = TmallAdapter(_xiaomi_spec())

    with pytest.raises(LayoutRecognitionError):
        adapter.observe(_task(), cast(Any, page))

    assert page.wait_timeout_milliseconds.count(250) == 20


def test_coupon_installment_trade_in_deposit_and_line_through_cannot_win() -> None:
    assert str(_observe().price) == "4399"


def test_live_observed_subprice_prefix_cannot_replace_current_selling_price() -> None:
    html = _live_observed_html().replace(
        'class="tmall-coupon-price">优惠券 ¥12,999',
        'class="subPrice--fixture">优惠前 ￥52,999',
        1,
    )

    assert str(_observe(html=html, after_search_url=_LIVE_RESULTS_URL).price) == "4399"


def test_honor_power2_uses_explicit_pre_discount_price_beside_subsidy_price(
) -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Power2")
    html = html.replace("小米15", "荣耀Power2")
    html = re.sub(
        r'(<section id="tbpcDetail_SkuPanelRightWrap">).*?(</section>)',
        r'\1<div class="priceWrap--fixture">'
        r'平台补贴后 <span class="highlightPrice--fixture" '
        r'style="color:rgb(255,0,0)">¥2549</span>'
        r'<span class="subPrice--fixture" '
        r'style="color:rgb(120,120,120)">优惠前 ¥2699</span>'
        r'</div>\2',
        html,
        count=1,
        flags=re.DOTALL,
    )
    task = _task(brand="HONOR", model_name="荣耀Power2")
    page = _FixturePage(html=html, after_search_url=_honor_power2_result_url())

    observation = TmallAdapter(_honor_spec()).observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("2699")


def test_live_observed_platform_subsidy_context_is_not_policy_safe_price() -> None:
    html = _live_observed_html().replace(
        'style="color:rgb(0,0,0)">¥4,299',
        'style="color:rgb(0,0,0)">平台加补后 ￥4,299',
        1,
    ).replace(
        'style="color:rgb(255,0,0)">¥4,399',
        'style="color:rgb(255,0,0)">平台加补后 ￥4,399',
        1,
    ).replace(
        'style="color:rgb(120,120,120)">¥9,999',
        'style="color:rgb(120,120,120)">平台加补后 ￥9,999',
        1,
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html, after_search_url=_LIVE_RESULTS_URL)


@pytest.mark.parametrize("risk_context", ["分期", "优惠券", "补贴", "原价"])
def test_price_child_is_rejected_when_bounded_parent_context_is_non_selling(
    risk_context: str,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<section class="tmall-current-selling">',
        f'<section class="tmall-current-selling">{risk_context}',
    )

    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


@pytest.mark.parametrize("price_context_mode", ["missing", "too_long"])
def test_missing_or_unbounded_price_context_is_technical(
    price_context_mode: str,
) -> None:
    with pytest.raises(LayoutRecognitionError):
        _observe(price_context_mode=price_context_mode)


def test_no_valid_visible_selling_price_is_technical() -> None:
    html = re.sub(
        r'<section class="tmall-current-selling">.*?</section>',
        '<section class="tmall-current-selling">'
        '<span class="tmall-selling-price" data-sku="123456789018">'
        "12期 ¥399</span></section>",
        (FIXTURES / "normal.html").read_text("utf-8"),
        flags=re.DOTALL,
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_live_product_without_observed_numeric_sku_binding_uses_visible_state() -> None:
    html = re.sub(
        r' data-(?:current-)?sku="[^"]*"',
        "",
        _live_observed_html(),
    )

    observation = _observe(html=html, after_search_url=_LIVE_RESULTS_URL)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert str(observation.price) == "4399"


def test_visible_sold_out_state_cannot_return_price_without_hidden_bindings() -> None:
    html = _without_hidden_sku_bindings(
        (FIXTURES / "sold_out.html").read_text("utf-8")
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_visible_stock_state_changes_do_not_change_the_price_result() -> None:
    observation = _observe(
        stock_snapshots=("现货", "已售罄", "现货", "已售罄", "现货")
    )

    assert observation.price == Decimal("4399")


@pytest.mark.parametrize(
    "stock_text",
    [
        "现货 已售罄",
        "预计明日更新",
        "",
    ],
)
def test_visible_stock_text_does_not_change_the_price_result(
    stock_text: str,
) -> None:
    assert _observe(stock_snapshots=(stock_text,)).price == Decimal("4399")


def test_blank_visible_delivery_region_does_not_change_the_price_result() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<div class="tmall-delivery-region">福建 &gt; 福州 &gt; 台江</div>',
        '<div class="tmall-delivery-region"> </div>',
    )

    assert _observe(html=html).price == Decimal("4399")


@pytest.mark.parametrize("stock_text", ["现货", "已售罄"])
def test_recognized_available_and_sold_out_stock_continue_quotation(
    stock_text: str,
) -> None:
    observation = _observe(stock_snapshots=(stock_text,))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.semantic_state.stock_state == "not-required-for-quotation"


def test_conflicting_available_and_sold_out_stock_states_do_not_change_price() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        '<div class="tmall-stock-status" data-sku="123456789018">现货</div>',
        '<div class="tmall-stock-status" data-sku="123456789018">现货</div>'
        '<div class="tmall-stock-status" data-sku="123456789018">已售罄</div>',
    )

    assert _observe(html=html).price == Decimal("4399")


@pytest.mark.parametrize("stock_mutation", ["missing", "duplicate"])
def test_visible_stock_state_presence_does_not_change_price(
    stock_mutation: str,
) -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8")
    stock = '<div class="tmall-stock-status" data-sku="123456789018">现货</div>'
    if stock_mutation == "missing":
        html = html.replace(stock, "")
    elif stock_mutation == "duplicate":
        html = html.replace(stock, stock + stock)

    assert _observe(html=html).price == Decimal("4399")


def test_hidden_stock_sku_does_not_change_price_result() -> None:
    html = (FIXTURES / "normal.html").read_text("utf-8").replace(
        'data-sku="123456789018">现货</div>',
        'data-sku="9">现货</div>',
    )

    assert _observe(html=html).price == Decimal("4399")


def test_invalid_rectangle_prevents_legal_no() -> None:
    html = (FIXTURES / "no_model.html").read_text("utf-8").replace(
        'class="tmall-shop-search-result"',
        'class="tmall-shop-search-result" data-invalid-box="true"',
    )
    with pytest.raises(LayoutRecognitionError):
        _observe(html=html)


def test_default_registry_resolves_tmall_for_all_seven_specs() -> None:
    registry = AdapterRegistry()
    adapters = [
        registry.adapter_for(brand, WebsiteChannel.TMALL)
        for brand in SUPPORTED_BRANDS
    ]

    assert all(isinstance(adapter, TmallAdapter) for adapter in adapters)
    assert [adapter.spec.brand for adapter in adapters] == list(SUPPORTED_BRANDS)
    assert all(adapter.spec.channel is WebsiteChannel.TMALL for adapter in adapters)


def test_adapter_navigates_only_to_approved_entry_and_exact_item_url() -> None:
    spec = _xiaomi_spec()
    page = _FixturePage()

    TmallAdapter(spec).observe(_task(), cast(Any, page))

    assert page.goto_calls == [
        spec.entry_url,
        "https://detail.tmall.com/item.htm?id=123456789018",
    ]

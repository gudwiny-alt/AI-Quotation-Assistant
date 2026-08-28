from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from quote_app.evidence.quality import CaptureQualityError
from quote_app.sites.catalog import load_site_catalog
from quote_app.sites.official_brands.factory import create_official_adapter
from quote_app.sites.official_brands.xiaomi import XiaomiOfficialAdapter
from quote_app.sites.official_brands.xiaomi import _approved_product_url
from quote_app.sites.official_brands.xiaomi import _scroll_proof_group_into_view
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import LayoutRecognitionError

_FIXTURES = Path(__file__).parents[1] / "fixtures/sites/official_live/xiaomi"
_ROLE = re.compile(r'^\[data-xiaomi-role=["\'](?P<role>[^"\']+)["\']\]$')
_HREF = re.compile(r"^a\[href\*=[\"'](?P<part>[^\"']+)[\"']\]$")


class _Locator:
    def __init__(self, page: _XiaomiFixturePage, nodes: list[ET.Element]) -> None:
        self.page = page
        self.nodes = nodes
        self.generation = page.generation

    def _fresh(self) -> None:
        if self.generation != self.page.generation:
            raise RuntimeError("stale fixture locator")

    def count(self) -> int:
        self._fresh()
        return len(self.nodes)

    def nth(self, index: int) -> _Locator:
        self._fresh()
        return _Locator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _Locator:
        self._fresh()
        if selector.startswith("xpath=following::"):
            all_nodes = list(self.page.root.iter())
            start = all_nodes.index(self.nodes[0])
            matches = [
                node
                for node in all_nodes[start + 1 :]
                if node.tag in {"ul", "ol"} or node.get("role") == "list"
            ]
            return _Locator(self.page, matches[:1])
        if selector.startswith("xpath=ancestor::"):
            node = self.nodes[0]
            ancestors = self.page.ancestors_of(node)
            if "aside" in selector:
                ancestors = [ancestor for ancestor in ancestors if ancestor.tag == "aside"]
            elif "purchase-summary" in selector:
                ancestors = [
                    ancestor
                    for ancestor in ancestors
                    if ancestor.get("data-xiaomi-role") == "purchase-summary"
                ]
            elif "product-info" in selector:
                ancestors = [
                    ancestor
                    for ancestor in ancestors
                    if "product-info" in ancestor.get("class", "")
                ]
            elif "product-con" in selector:
                ancestors = [
                    ancestor
                    for ancestor in ancestors
                    if "product-con" in ancestor.get("class", "").split()
                ]
            return _Locator(self.page, ancestors[:1])
        descendants = [child for node in self.nodes for child in node.iter() if child is not node]
        return _Locator(self.page, self.page.select(selector, descendants))

    def get_by_text(self, value: str | re.Pattern[str], *, exact: bool = False) -> _Locator:
        self._fresh()
        if self.page.price_text_search_unavailable and hasattr(value, "search"):
            return _Locator(self.page, [])
        descendants = [child for node in self.nodes for child in node.iter()]
        nodes = []
        for node in descendants:
            text = "".join(node.itertext()).strip()
            matches = value.search(text) is not None if hasattr(value, "search") else (
                text == value if exact else str(value) in text
            )
            if matches:
                nodes.append(node)
        return _Locator(self.page, nodes)

    def is_visible(self) -> bool:
        self._fresh()
        return len(self.nodes) == 1 and self.nodes[0].get("hidden") is None

    def inner_text(self) -> str:
        self._fresh()
        node = self.nodes[0]
        if node.get("data-xiaomi-role") in {"selling-price", "main-price"}:
            self.page.events.append("price-read")
        sequence = node.get("data-price-sequence")
        if sequence:
            values = sequence.split("|")
            index = min(self.page.price_reads, len(values) - 1)
            self.page.price_reads += 1
            return f"销售价 ¥{values[index]}"
        return "".join(node.itertext()).strip()

    def get_attribute(self, name: str) -> str | None:
        self._fresh()
        return self.nodes[0].get(name)

    def input_value(self) -> str:
        self._fresh()
        return self.nodes[0].get("value", "")

    def click(self) -> None:
        self._fresh()
        node = self.nodes[0]
        href = node.get("href")
        if href:
            self.page.goto(href)
            return
        role = node.get("data-xiaomi-role", "")
        test_kind = node.get("data-test-kind")
        if role.endswith("-option") or test_kind is not None:
            option_kind = test_kind or role.removesuffix("-option")
            for candidate in self.page.root.iter():
                if (
                    candidate.get("data-xiaomi-role") == role
                    if test_kind is None
                    else candidate.get("data-test-kind") == test_kind
                ):
                    candidate.set("aria-selected", "false")
            node.set("aria-selected", "true")
            self.page.option_clicks.append(option_kind)
            self.page.generation += 1

    def scroll_into_view_if_needed(self) -> None:
        self._fresh()
        role = self.nodes[0].get("data-xiaomi-role", "")
        self.page.events.append(f"scroll:{role}")
        self.page.scrolls.append(role)

    def bounding_box(self) -> dict[str, float]:
        self._fresh()
        style = self.nodes[0].get("style", "")
        values = dict(
            item.strip().split(":", 1)
            for item in style.split(";")
            if ":" in item
        )
        return {
            "x": float(values.get("left", "10px").removesuffix("px")),
            "y": float(values.get("top", "10px").removesuffix("px")),
            "width": float(values.get("width", "300px").removesuffix("px")),
            "height": float(values.get("height", "60px").removesuffix("px")),
        }

    def evaluate(self, _script: str) -> object:
        self._fresh()
        node = self.nodes[0]
        if "__xiaomiExplicitSelection" in _script:
            for candidate in [node, *self.page.ancestors_of(node)]:
                classes = set(candidate.get("class", "").lower().split())
                if classes.intersection({"selected", "active", "current", "checked"}):
                    return True
                values = {
                    candidate.get("data-selected", "").lower(),
                    candidate.get("data-state", "").lower(),
                }
                if values.intersection({"true", "selected", "active", "checked"}):
                    return True
            return False
        style = node.get("style", "")
        all_nodes = list(self.page.root.iter())
        node_index = all_nodes.index(node)
        service_index = next(
            (
                index
                for index, candidate in enumerate(all_nodes)
                if candidate.tag == "p"
                and "选择小米提供的" in "".join(candidate.itertext())
            ),
            None,
        )
        ancestors = self.page.ancestors_of(node)
        nearest_context = (
            "".join(ancestors[0].itertext()).strip() if ancestors else ""
        )

        def non_struck_text(candidate: ET.Element) -> str:
            parts = [candidate.text or ""]
            for child in list(candidate):
                if child.tag.lower() not in {"del", "s", "strike"}:
                    parts.append(non_struck_text(child))
                parts.append(child.tail or "")
            return "".join(parts)

        price_sequence = node.get("data-price-sequence")
        if price_sequence:
            sequence_values = price_sequence.split("|")
            sequence_index = min(
                max(self.page.price_reads - 1, 0),
                len(sequence_values) - 1,
            )
            effective_text = f"销售价 ¥{sequence_values[sequence_index]}"
        else:
            effective_text = non_struck_text(node).strip()

        return {
            "color": "rgb(255, 0, 0)" if "255" in style else "rgb(0, 0, 0)",
            "effectiveLineThrough": node.tag == "del",
            "effectiveText": effective_text,
            "inViewport": node.get("data-in-viewport", "true") == "true",
            "contextText": node.get("data-price-kind"),
            "beforeVersion": True,
            "beforeServices": service_index is None or node_index < service_index,
            "nearestContextText": nearest_context,
            "nearestContextClass": ancestors[0].get("class", "") if ancestors else "",
            "nearestContextIsSummary": bool(
                ancestors
                and (
                    ancestors[0].tag == "aside"
                    or "product-con" in ancestors[0].get("class", "").split()
                )
            ),
        }

    def element_handle(self) -> _Locator:
        self._fresh()
        return self


class _XiaomiFixturePage:
    def __init__(self, fixture: str) -> None:
        source = (_FIXTURES / fixture).read_text(encoding="utf-8")
        source = re.sub(r"<!doctype html>", "", source, flags=re.IGNORECASE)
        self.root = ET.fromstring(source)
        self.url = "about:blank"
        self.screen = "results"
        self.generation = 0
        self.price_reads = 0
        self.price_text_search_unavailable = False
        self.wait_calls = 0
        self.goto_calls: list[str] = []
        self.option_clicks: list[str] = []
        self.scrolls: list[str] = []
        self.capture_scale = 1.0
        self.capture_scales: list[float] = []
        self.capture_scale_restore_count = 0
        self.events: list[str] = []
        self.proofs_fit = True
        self.main_price_group_fits = True
        self.proof_position_attempts = 0
        self.positioning_succeeds = True

    def goto(self, url: str, **_kwargs: object) -> None:
        if url.startswith("/"):
            url = f"https://www.mi.com{url}"
        self.goto_calls.append(url)
        parsed = urlsplit(url)
        if parsed.path == "/shop/buy":
            self.url = f"https://www.mi.com/shop/buy/detail?{parsed.query}"
            self.screen = "detail"
        elif parsed.path == "/shop/buy/detail":
            self.url = url
            self.screen = "detail"
        elif parsed.path == "/shop/search":
            self.url = url
            self.screen = "results"
        else:
            self.url = url

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_timeout(self, _milliseconds: float) -> None:
        self.wait_calls += 1
        for node in self.root.iter():
            threshold = node.get("data-show-after-waits")
            if threshold is not None and self.wait_calls >= int(threshold):
                node.attrib.pop("hidden", None)
                node.attrib.pop("data-show-after-waits", None)

    def evaluate(self, script: str, argument: object = None) -> object:
        if "data-quotation-capture-scale-original" in script:
            if "removeAttribute" in script:
                self.capture_scale = 1.0
                self.capture_scale_restore_count += 1
                return True
            if argument is not None:
                self.capture_scale = float(argument)
                self.capture_scales.append(float(argument))
                self.events.append(f"scale:{float(argument):.1f}")
            return {
                "inlineZoom": self.capture_scale,
                "computedZoom": self.capture_scale,
            }
        if "computedZoom: getComputedStyle(root).zoom" in script:
            return {
                "inlineZoom": self.capture_scale,
                "computedZoom": self.capture_scale,
            }
        if "__xiaomiProofsFitCurrentViewport" in script:
            handles = argument if isinstance(argument, list) else []
            if any(
                isinstance(handle, _Locator)
                and handle.nodes
                and handle.nodes[0].get("data-xiaomi-role") == "main-price"
                for handle in handles
            ):
                return self.proofs_fit and self.main_price_group_fits
            return self.proofs_fit
        if "window.scrollTo" in script:
            self.proof_position_attempts += 1
            if self.positioning_succeeds:
                self.proofs_fit = True
            return self.positioning_succeeds
        return True

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, self.select(selector, list(self.root.iter())))

    def get_by_text(self, value: str | re.Pattern[str], *, exact: bool = False) -> _Locator:
        if self.price_text_search_unavailable and hasattr(value, "search"):
            return _Locator(self, [])
        nodes = []
        for node in self._active_nodes():
            text = "".join(node.itertext()).strip()
            matches = value.search(text) is not None if hasattr(value, "search") else (
                text == value if exact else str(value) in text
            )
            if matches:
                nodes.append(node)
        return _Locator(self, nodes)

    def get_by_role(
        self,
        role: str,
        *,
        name: str | None = None,
        exact: bool = False,
    ) -> _Locator:
        if role == "heading":
            nodes = [node for node in self._active_nodes() if node.tag in {"h1", "h2", "h3"}]
        elif role == "searchbox":
            nodes = [
                node
                for node in self._active_nodes()
                if node.tag == "input" and node.get("role") == "searchbox"
            ]
        elif role == "listitem":
            nodes = [
                node
                for node in self._active_nodes()
                if node.tag == "li" or node.get("role") == "listitem"
            ]
        else:
            nodes = []
        if name is not None:
            nodes = [
                node
                for node in nodes
                if (("".join(node.itertext()).strip() == name) if exact else name in "".join(node.itertext()))
            ]
        return _Locator(self, nodes)

    def select(self, selector: str, nodes: list[ET.Element]) -> list[ET.Element]:
        active = [node for node in nodes if self._screen_for(node) in {None, self.screen}]
        role_match = _ROLE.fullmatch(selector)
        if role_match:
            return [
                node
                for node in active
                if node.get("data-xiaomi-role") == role_match.group("role")
            ]
        href_match = _HREF.fullmatch(selector)
        if href_match:
            return [
                node
                for node in active
                if node.tag == "a" and href_match.group("part") in node.get("href", "")
            ]
        if selector == 'a[href*="/shop/buy/detail"][href*="product_id="]':
            return [
                node
                for node in active
                if node.tag == "a"
                and "/shop/buy/detail" in node.get("href", "")
                and "product_id=" in node.get("href", "")
            ]
        if selector == 'a[href*="/shop/buy?"][href*="product_id="]':
            return [
                node
                for node in active
                if node.tag == "a"
                and "/shop/buy?" in node.get("href", "")
                and "product_id=" in node.get("href", "")
            ]
        if selector == "li":
            return [node for node in active if node.tag == "li"]
        if selector == "button":
            return [node for node in active if node.tag == "button"]
        if selector == '[role="listitem"]':
            return [node for node in active if node.get("role") == "listitem"]
        if selector == 'input[role="searchbox"]':
            return [
                node
                for node in active
                if node.tag == "input" and node.get("role") == "searchbox"
            ]
        if selector == '[class*="search-result"]':
            return [
                node
                for node in active
                if "search-result" in node.get("class", "")
            ]
        if selector == '[class~="product-con"]':
            return [
                node
                for node in active
                if "product-con" in node.get("class", "").split()
            ]
        if selector == '[class~="price-info"] > span':
            return [
                child
                for node in active
                if "price-info" in node.get("class", "").split()
                for child in list(node)
                if child.tag == "span"
            ]
        if selector in {"main", "h1", "h2", "h3"}:
            return [node for node in active if node.tag == selector]
        return []

    def _active_nodes(self) -> list[ET.Element]:
        return [
            node
            for node in self.root.iter()
            if self._screen_for(node) in {None, self.screen}
        ]

    def _screen_for(self, node: ET.Element) -> str | None:
        for main in self.root.findall(".//main"):
            if node is main or node in set(main.iter()):
                return main.get("data-screen")
        return None

    def ancestors_of(self, node: ET.Element) -> list[ET.Element]:
        parents = {
            child: parent
            for parent in self.root.iter()
            for child in list(parent)
        }
        ancestors: list[ET.Element] = []
        current = parents.get(node)
        while current is not None:
            ancestors.append(current)
            current = parents.get(current)
        return ancestors


def _task(*, color: str = "黑色") -> WebsiteTask:
    return WebsiteTask(
        task_id="xiaomi-17-max",
        run_id="run-xiaomi",
        source_row_number=2,
        output_row_number=2,
        material_code="XM-17-MAX",
        brand="小米",
        model_name="Xiaomi 17 Max",
        ram="12GB",
        storage="256GB",
        color=color,
        channel=WebsiteChannel.OFFICIAL,
    )


def _adapter() -> XiaomiOfficialAdapter:
    spec = next(
        item
        for item in load_site_catalog()
        if item.brand == "小米" and item.channel is WebsiteChannel.OFFICIAL
    )
    adapter = create_official_adapter(spec)
    assert isinstance(adapter, XiaomiOfficialAdapter)
    return adapter


def test_xiaomi_enters_only_exact_model_and_reads_red_selling_price() -> None:
    page = _XiaomiFixturePage("normal.html")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")
    assert observation.url == "https://www.mi.com/shop/buy/detail?product_id=24648"
    assert observation.semantic_state.current_sku == "official-detail:24648"
    assert page.option_clicks == ["capacity", "color"]
    assert not any("24649" in call or "24650" in call for call in page.goto_calls)


def test_xiaomi_scales_detail_to_80_percent_before_option_scroll_and_price_read() -> None:
    page = _XiaomiFixturePage("normal.html")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.capture_scale == 0.8
    assert page.capture_scales == [0.8]
    scale_index = page.events.index("scale:0.8")
    scroll_indexes = [
        index for index, event in enumerate(page.events) if event.startswith("scroll:")
    ]
    assert scroll_indexes
    assert scale_index < min(scroll_indexes)
    assert scale_index < page.events.index("price-read")


def test_xiaomi_accepts_real_protocol_relative_product_card_url() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact_link = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and node.get("href") == "/shop/buy?product_id=24648"
    )
    exact_link.set("href", "//www.mi.com/shop/buy?product_id=24648")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://www.mi.com/shop/buy/detail?product_id=24648"
    assert page.goto_calls[1] == "https://www.mi.com/shop/buy?product_id=24648"


def test_xiaomi_accepts_direct_detail_product_card_url_without_legacy_role() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact_link = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and node.get("href") == "/shop/buy?product_id=24648"
    )
    exact_link.attrib.pop("data-xiaomi-role")
    exact_link.set("href", "/shop/buy/detail?product_id=24648")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://www.mi.com/shop/buy/detail?product_id=24648"
    assert page.goto_calls[1] == "https://www.mi.com/shop/buy/detail?product_id=24648"


def test_xiaomi_accepts_tracked_detail_card_when_product_id_is_not_first_query_parameter() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact_link = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and node.get("href") == "/shop/buy?product_id=24648"
    )
    exact_link.attrib.pop("data-xiaomi-role")
    exact_link.set(
        "href",
        "/shop/buy/detail?cfrom=search&product_id=24648",
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == (
        "https://www.mi.com/shop/buy/detail?cfrom=search&product_id=24648"
    )
    assert page.goto_calls[1] == (
        "https://www.mi.com/shop/buy/detail?cfrom=search&product_id=24648"
    )


def test_xiaomi_accepts_tracked_buy_card_when_product_id_is_not_first_query_parameter() -> None:
    """Current Xiaomi cards may put tracking fields before the numeric product id."""
    page = _XiaomiFixturePage("normal.html")
    exact_link = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and node.get("href") == "/shop/buy?product_id=24648"
    )
    exact_link.attrib.pop("data-xiaomi-role")
    exact_link.set(
        "href",
        "/shop/buy?cfrom=search&product_id=24648",
    )

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == "https://www.mi.com/shop/buy/detail?cfrom=search&product_id=24648"
    assert page.goto_calls[1] == (
        "https://www.mi.com/shop/buy?cfrom=search&product_id=24648"
    )


def test_xiaomi_supports_real_heading_anchor_listitem_and_summary_price_semantics() -> None:
    page = _XiaomiFixturePage("real_semantic.html")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4799")
    assert page.option_clicks == ["capacity", "color"]


def test_xiaomi_supports_real_product_con_purchase_summary() -> None:
    page = _XiaomiFixturePage("real_semantic.html")
    summary = next(node for node in page.root.iter() if node.tag == "aside")
    summary.tag = "div"
    summary.set("class", "product-con")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4799")


def test_xiaomi_reads_real_price_info_without_broad_text_search() -> None:
    page = _XiaomiFixturePage("real_semantic.html")
    page.price_text_search_unavailable = True

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4799")


def test_xiaomi_reads_unique_page_price_when_purchase_summary_class_drifts() -> None:
    page = _XiaomiFixturePage("real_semantic.html")
    summary = next(node for node in page.root.iter() if node.tag == "aside")
    summary.tag = "section"
    summary.set("class", "detail-right-pane")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4799")


def test_xiaomi_reads_visible_price_without_price_wrapper_hierarchy() -> None:
    page = _XiaomiFixturePage("real_semantic.html")
    detail = next(
        node
        for node in page.root.iter()
        if node.tag == "main" and node.get("data-screen") == "detail"
    )
    true_price = next(
        node
        for node in detail.iter()
        if "4799" in "".join(node.itertext()) and node.tag == "span"
    )
    true_price_parent = page.ancestors_of(true_price)[0]
    true_price_parent.set("class", "amount")
    for node in detail.iter():
        text = "".join(node.itertext()).strip()
        if node.tag == "span" and text in {"9999 元", "299 元"}:
            node.set("data-in-viewport", "false")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4799")


def test_xiaomi_reads_current_price_when_old_price_is_nested_inside_same_node() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("real_semantic.html")
    detail = next(
        node
        for node in page.root.iter()
        if node.tag == "main" and node.get("data-screen") == "detail"
    )
    price = next(
        node
        for node in detail.iter()
        if node.tag == "span" and "4799" in "".join(node.itertext())
    )
    price.text = "2999 元"
    old_price = ET.SubElement(price, "del")
    old_price.text = "3299 元"

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    captured_state = adapter.verified_state_reader(
        task,
        page,
        observation.semantic_state,
    )()

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("2999")
    assert captured_state == observation.semantic_state
    assert page.capture_scale == 0.8


def test_xiaomi_does_not_quote_when_nested_node_contains_only_old_price() -> None:
    page = _XiaomiFixturePage("real_semantic.html")
    detail = next(
        node
        for node in page.root.iter()
        if node.tag == "main" and node.get("data-screen") == "detail"
    )
    price = next(
        node
        for node in detail.iter()
        if node.tag == "span" and "4799" in "".join(node.itertext())
    )
    price.text = ""
    old_price = ET.SubElement(price, "del")
    old_price.text = "3299 元"

    with pytest.raises(CaptureQualityError, match="selling price did not appear"):
        _adapter().observe(_task(), page)


@pytest.mark.parametrize("fixture", ["normal.html", "sold_out.html"])
def test_xiaomi_stock_state_never_blocks_target_offer(fixture: str) -> None:
    observation = _adapter().observe(_task(), _XiaomiFixturePage(fixture))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")


@pytest.mark.parametrize(
    ("fixture", "outcome", "roles"),
    [
        ("no_model.html", BusinessOutcome.NO_MODEL, ("search_keyword", "result_region")),
        ("no_capacity.html", BusinessOutcome.CAPACITY_UNAVAILABLE, ("capacity",)),
        ("no_color.html", BusinessOutcome.COLOR_UNAVAILABLE, ("color",)),
    ],
)
def test_xiaomi_emits_only_approved_legal_no_states(
    fixture: str,
    outcome: BusinessOutcome,
    roles: tuple[str, ...],
) -> None:
    observation = _adapter().observe(_task(), _XiaomiFixturePage(fixture))

    assert observation.outcome is outcome
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == roles


def test_xiaomi_reacquires_locators_after_spa_rebuild_and_waits_for_price() -> None:
    page = _XiaomiFixturePage("delayed_price.html")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4399")
    assert page.option_clicks == ["capacity", "color"]
    assert page.price_reads >= 3


def test_xiaomi_resume_uses_saved_detail_without_repeating_search() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    first = adapter.observe(task, page)
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=first.outcome,
        price=first.price,
        url=first.url,
        observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    resumed_page = _XiaomiFixturePage("normal.html")

    resumed = adapter.resume(task, resumed_page, checkpoint)

    assert resumed == first
    assert resumed_page.goto_calls == [checkpoint.url]


def test_xiaomi_capture_preparation_is_idempotent_and_state_is_reread() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    first_scrolls = tuple(page.scrolls)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    current = adapter.verified_state_reader(task, page, observation.semantic_state)()

    assert tuple(page.scrolls) == first_scrolls
    assert current == observation.semantic_state


def test_xiaomi_capture_uses_80_percent_without_scrolling_when_proofs_fit() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 0.8
    assert page.capture_scales == [0.8]
    assert page.proof_position_attempts == 0


def test_xiaomi_capture_uses_visible_price_instead_of_price_region_geometry() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.main_price_group_fits = False
    page.positioning_succeeds = False

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 0.8
    assert page.proof_position_attempts == 0


def test_xiaomi_capture_positions_once_only_when_80_percent_does_not_fit() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.proofs_fit = False

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scale == 0.8
    assert page.proof_position_attempts == 1
    assert page.proofs_fit is True


@pytest.mark.parametrize(
    "url",
    [
        "http://www.mi.com/shop/buy/detail?product_id=24648",
        "https://evil.example/shop/buy/detail?product_id=24648",
        "//evil.example/shop/buy/detail?product_id=24648",
        "https://user:pass@www.mi.com/shop/buy/detail?product_id=24648",
        "https://www.mi.com/product/24648",
        "https://www.mi.com/shop/buy/detail?product_id=not-a-number",
    ],
)
def test_xiaomi_rejects_unapproved_product_urls(url: str) -> None:
    with pytest.raises(ValueError, match="approved numeric"):
        _approved_product_url(url, allow_card=False)


def test_xiaomi_capture_reader_fails_closed_when_product_identity_drifts() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.url = "https://www.mi.com/shop/buy/detail?product_id=99999"

    with pytest.raises(LayoutRecognitionError):
        adapter.verified_state_reader(task, page, observation.semantic_state)()


def test_xiaomi_capture_reader_fails_closed_when_configuration_drifts() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    capacity = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "capacity-option"
        and "12GB" in "".join(node.itertext())
    )
    capacity.set("aria-selected", "false")

    with pytest.raises(LayoutRecognitionError):
        adapter.verified_state_reader(task, page, observation.semantic_state)()


def test_xiaomi_capture_reader_fails_closed_when_price_drifts() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    price = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "selling-price"
    )
    price.text = "销售价 ¥4499"

    with pytest.raises(LayoutRecognitionError):
        adapter.verified_state_reader(task, page, observation.semantic_state)()


def test_xiaomi_exact_model_with_illegal_link_is_technical_failure_not_no_model() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and "Xiaomi 17 Max" == "".join(node.itertext()).strip()
    )
    exact.set("href", "https://evil.example/shop/buy?product_id=24648")

    with pytest.raises(ValueError, match="approved numeric"):
        _adapter().observe(_task(), page)


def test_xiaomi_capture_layout_does_not_claim_oversized_proof_fits() -> None:
    class HandleLocator:
        def element_handle(self) -> object:
            return object()

    class OversizedPage:
        def evaluate(self, _script: str, _handles: list[object]) -> bool:
            return False

    assert not _scroll_proof_group_into_view(
        OversizedPage(),
        (HandleLocator(), HandleLocator(), HandleLocator(), HandleLocator()),
    )


@pytest.mark.parametrize(
    ("fixture", "outcome"),
    [
        ("no_model.html", BusinessOutcome.NO_MODEL),
        ("empty_results.html", BusinessOutcome.NO_MODEL),
        ("no_capacity.html", BusinessOutcome.CAPACITY_UNAVAILABLE),
        ("no_color.html", BusinessOutcome.COLOR_UNAVAILABLE),
    ],
)
def test_xiaomi_legal_no_is_revalidated_and_uses_latest_capture_geometry(
    fixture: str,
    outcome: BusinessOutcome,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage(fixture)
    observation = adapter.observe(task, page)
    assert observation.outcome is outcome
    proof_role = {
        BusinessOutcome.NO_MODEL: "results",
        BusinessOutcome.CAPACITY_UNAVAILABLE: "capacity-group",
        BusinessOutcome.COLOR_UNAVAILABLE: "color-group",
    }[outcome]
    proof = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == proof_role
    )
    proof.set("style", "left:77px;top:88px;width:500px;height:120px")

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    verified = adapter.verified_state_reader(task, page, observation.semantic_state)()
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    )

    assert verified == observation.semantic_state
    assert rectangles[-1].x == 77


def test_xiaomi_no_model_resume_fails_if_exact_card_appears() -> None:
    adapter = _adapter()
    task = _task()
    original = adapter.observe(task, _XiaomiFixturePage("no_model.html"))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=None,
        url=original.url,
        observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    with pytest.raises(LayoutRecognitionError):
        adapter.resume(task, _XiaomiFixturePage("normal.html"), checkpoint)


def test_xiaomi_no_model_resume_rejects_url_keyword_drift() -> None:
    task = _task()
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=BusinessOutcome.NO_MODEL,
        price=None,
        url="https://www.mi.com/shop/search?keyword=Xiaomi%2017%20Pro",
        observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    with pytest.raises(LayoutRecognitionError, match="keyword"):
        _adapter().resume(task, _XiaomiFixturePage("no_model.html"), checkpoint)


def test_xiaomi_zero_cards_without_explicit_empty_state_is_not_no_model() -> None:
    page = _XiaomiFixturePage("empty_results.html")
    empty = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "empty-results"
    )
    empty.attrib.pop("data-xiaomi-role")

    with pytest.raises(LayoutRecognitionError, match="stabilize"):
        _adapter().observe(_task(), page)


def test_xiaomi_waits_for_delayed_search_cards_before_deciding_no_model() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and "Xiaomi 17 Max" == "".join(node.itertext()).strip()
    )
    exact.set("hidden", "")
    exact.set("data-show-after-waits", "3")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.wait_calls >= 3


def test_xiaomi_exact_card_does_not_depend_on_blank_header_search_value() -> None:
    page = _XiaomiFixturePage("normal.html")
    keyword = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "search-keyword"
    )
    keyword.set("value", "")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[1] == "https://www.mi.com/shop/buy?product_id=24648"


def test_xiaomi_no_model_decision_waits_for_full_search_settle_window() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and "Xiaomi 17 Max" == "".join(node.itertext()).strip()
    )
    exact.set("hidden", "")
    exact.set("data-show-after-waits", "8")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.wait_calls >= 8


def test_xiaomi_no_model_waits_full_ten_seconds_for_late_exact_card() -> None:
    page = _XiaomiFixturePage("normal.html")
    exact = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "product-link"
        and "Xiaomi 17 Max" == "".join(node.itertext()).strip()
    )
    exact.set("hidden", "")
    exact.set("data-show-after-waits", "28")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.wait_calls >= 28


def test_xiaomi_waits_for_delayed_capacity_options_before_writing_legal_no() -> None:
    page = _XiaomiFixturePage("normal.html")
    target = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "capacity-option"
        and "12GB" in "".join(node.itertext())
    )
    target.set("hidden", "")
    target.set("data-show-after-waits", "3")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_xiaomi_capacity_no_decision_waits_for_full_option_settle_window() -> None:
    page = _XiaomiFixturePage("normal.html")
    target = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "capacity-option"
        and "12GB" in "".join(node.itertext())
    )
    target.set("hidden", "")
    target.set("data-show-after-waits", "8")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.wait_calls >= 8


def test_xiaomi_capacity_no_waits_full_five_seconds_for_late_target() -> None:
    page = _XiaomiFixturePage("normal.html")
    target = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "capacity-option"
        and "12GB" in "".join(node.itertext())
    )
    target.set("hidden", "")
    target.set("data-show-after-waits", "18")

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.wait_calls >= 18


def test_xiaomi_price_must_remain_stable_for_full_window() -> None:
    page = _XiaomiFixturePage("normal.html")
    price = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "selling-price"
    )
    price.set("data-price-sequence", "4299|4299|4399|4399")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4399")
    assert page.price_reads >= 12


def test_xiaomi_retries_temporarily_missing_price_within_five_second_budget() -> None:
    page = _XiaomiFixturePage("normal.html")
    price = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "selling-price"
    )
    price.set("hidden", "")
    price.set("data-show-after-waits", "12")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4399")
    assert 12 <= page.wait_calls <= 29


def test_xiaomi_allows_ten_seconds_for_first_valid_price_then_stabilizes() -> None:
    page = _XiaomiFixturePage("normal.html")
    price = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "selling-price"
    )
    price.set("hidden", "")
    price.set("data-show-after-waits", "28")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4399")
    assert page.wait_calls >= 40


def test_xiaomi_does_not_wait_full_appearance_budget_when_price_is_ready() -> None:
    page = _XiaomiFixturePage("normal.html")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4399")
    assert page.wait_calls < 40


def test_xiaomi_normal_price_stage_stays_within_five_second_tick_budget() -> None:
    page = _XiaomiFixturePage("normal.html")

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("4399")
    assert page.wait_calls <= 29


def test_xiaomi_detail_title_prefers_purchase_summary_duplicate_heading() -> None:
    adapter = _adapter()
    page = _XiaomiFixturePage("real_semantic.html")
    page.goto("https://www.mi.com/shop/buy/detail?product_id=24648")

    title = adapter._detail_title(page, _task().model_name)

    assert title is not None
    assert any(ancestor.tag == "aside" for ancestor in page.ancestors_of(title.nodes[0]))


def test_xiaomi_no_model_uses_specific_result_region_instead_of_main() -> None:
    observation = _adapter().observe(_task(), _XiaomiFixturePage("real_no_model.html"))

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert observation.css_rectangles[-1].x == 77


def test_xiaomi_duplicate_exact_cards_with_different_product_ids_fail_closed() -> None:
    page = _XiaomiFixturePage("normal.html")
    results = next(
        node
        for node in page.root.iter()
        if node.get("data-xiaomi-role") == "results"
    )
    duplicate = ET.fromstring(
        '<a data-xiaomi-role="product-link" href="/shop/buy?product_id=99999">'
        '<span data-xiaomi-role="product-title">Xiaomi 17 Max</span></a>'
    )
    results.append(duplicate)

    with pytest.raises(LayoutRecognitionError, match="ambiguous"):
        _adapter().observe(_task(), page)


def test_xiaomi_unknown_border_only_selection_is_not_accepted() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    for node in page.root.iter():
        if node.get("data-xiaomi-role") in {"capacity-option", "color-option"}:
            node.attrib.pop("aria-selected", None)
            node.set("style", "border:2px solid red")

    with pytest.raises(LayoutRecognitionError):
        adapter.verified_state_reader(task, page, observation.semantic_state)()


def test_xiaomi_prepare_capture_fails_when_four_proofs_cannot_fit_one_view() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("normal.html")
    observation = adapter.observe(task, page)
    page.proofs_fit = False
    page.positioning_succeeds = False

    with pytest.raises(LayoutRecognitionError, match="same viewport"):
        adapter.prepare_capture_view(task, page, observation.semantic_state)


def test_xiaomi_no_model_capture_requires_keyword_and_results_in_one_view() -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage("no_model.html")
    observation = adapter.observe(task, page)
    page.evaluate = lambda _script, _handles: False  # type: ignore[attr-defined]

    with pytest.raises(LayoutRecognitionError, match="same viewport"):
        adapter.prepare_capture_view(task, page, observation.semantic_state)


@pytest.mark.parametrize(
    ("fixture", "proof_role"),
    [("no_capacity.html", "capacity-group"), ("no_color.html", "color-group")],
)
def test_xiaomi_configuration_no_capture_scrolls_final_proof_into_view(
    fixture: str,
    proof_role: str,
) -> None:
    adapter = _adapter()
    task = _task()
    page = _XiaomiFixturePage(fixture)
    observation = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert proof_role in page.scrolls

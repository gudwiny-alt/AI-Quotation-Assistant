from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import pytest

from quote_app.domain.models import InputPaths
from tests.factories.workbook_factory import save_workbook

_OFFICIAL_FIXTURES = Path(__file__).parent / "fixtures" / "sites" / "official"
_OFFICIAL_SELECTOR = re.compile(
    r"^(?P<tag>[a-zA-Z0-9_-]*)"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?P<classes>(?:\.[a-zA-Z0-9_-]+)*)"
    r"(?P<attrs>(?:\[[^\]]+\])*)$"
)
_OFFICIAL_ATTRIBUTE = re.compile(
    r"\[(?P<name>[a-zA-Z0-9_-]+)"
    r"(?:(?P<operator>[\^*]?=)[\"']?(?P<value>[^\"'\]]+)[\"']?)?\]"
)
_OFFICIAL_CASE_DATA = {
    "小米": ("xiaomi", "小米 15", "12GB", "256GB", "黑色"),
    "HONOR": ("honor", "HONOR 400", "12GB", "256GB", "幻夜黑"),
    "华为": ("huawei", "华为 Pura 80", "12GB", "256GB", "曜金黑"),
    "维沃": ("vivo", "vivo X200", "12GB", "256GB", "钛色"),
    "欧珀": ("oppo", "OPPO Find X8", "12GB", "256GB", "浮光白"),
    "苹果": ("apple", "iPhone 16", "8GB", "256GB", "黑色"),
    "ZTE中兴": ("zte", "ZTE Axon 60", "12GB", "256GB", "曜石黑"),
}


class _OfficialNode:
    def __init__(
        self,
        tag: str,
        attrs: dict[str, str],
        parent: _OfficialNode | None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_OfficialNode] = []
        self.text_parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(
            self.text_parts + [child.text for child in self.children]
        ).strip()

    def descendants(self) -> list[_OfficialNode]:
        result: list[_OfficialNode] = []
        for child in self.children:
            result.append(child)
            result.extend(child.descendants())
        return result

    @property
    def visible(self) -> bool:
        current: _OfficialNode | None = self
        while current is not None:
            styles = _official_style(current.attrs.get("style", ""))
            if (
                "hidden" in current.attrs
                or current.attrs.get("aria-hidden") == "true"
                or styles.get("display") == "none"
                or styles.get("visibility") == "hidden"
            ):
                return False
            current = current.parent
        return True


class _OfficialDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = _OfficialNode("document", {}, None)
        self.stack = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        node = _OfficialNode(
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


class _OfficialLocator:
    def __init__(
        self,
        page: _OfficialFixturePage,
        nodes: list[_OfficialNode],
    ) -> None:
        self.page = page
        self.nodes = nodes

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> _OfficialLocator:
        return _OfficialLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> _OfficialLocator:
        matches: list[_OfficialNode] = []
        for node in self.nodes:
            matches.extend(_official_select(node.descendants(), selector))
        return _OfficialLocator(self.page, matches)

    def is_visible(self) -> bool:
        return len(self.nodes) == 1 and self.nodes[0].visible

    def inner_text(self) -> str:
        node = self.nodes[0]
        is_live_honor_stock = (
            "red" in node.attrs.get("class", "").split()
            and node.parent is not None
            and "product-address-prompt"
            in node.parent.attrs.get("class", "").split()
        )
        if (
            any(
                name.endswith("-stock")
                for name in node.attrs.get("class", "").split()
            )
            or is_live_honor_stock
        ):
            self.page.note_stock_read()
        return node.text

    def get_attribute(self, name: str) -> str | None:
        return self.nodes[0].attrs.get(name)

    def fill(self, value: str) -> None:
        self.nodes[0].attrs["value"] = value

    def input_value(self) -> str:
        return self.nodes[0].attrs.get("value", "")

    def press(self, key: str) -> None:
        self.page.press(key)

    def click(self) -> None:
        node = self.nodes[0]
        if node.attrs.get("data-action") == "search":
            if node.attrs.get("data-requires-enter") == "true":
                self.page.search_waiting_for_enter = True
            else:
                self.page.activate_results()
        option_kind = node.attrs.get("data-option-kind")
        if option_kind is not None:
            if option_kind.startswith("honor-"):
                self.page.select_honor_option(node, option_kind)
            else:
                self.page.select_option(node, option_kind)

    def scroll_into_view_if_needed(self) -> None:
        option_kind = self.nodes[0].attrs.get("data-option-kind")
        if option_kind is not None:
            self.page.option_scrolls.append(option_kind)

    def bounding_box(self) -> dict[str, float] | None:
        node = self.nodes[0]
        if node.attrs.get("data-invalid-box") == "true":
            return None
        styles = _official_style(node.attrs.get("style", ""))
        return {
            "x": _official_pixels(styles.get("left"), 10),
            "y": _official_pixels(styles.get("top"), 10),
            "width": _official_pixels(styles.get("width"), 100),
            "height": _official_pixels(styles.get("height"), 30),
        }

    def evaluate(self, _script: str) -> dict[str, object]:
        node = self.nodes[0]
        if (
            any(
                name.endswith("-price")
                for name in node.attrs.get("class", "").split()
            )
            or node.attrs.get("id") in {"pro-price-hand", "pro-price-old"}
        ):
            self.page.note_price_evaluation(node)
        color = "rgb(0, 0, 0)"
        effective_line_through = False
        context_text: str | None = None
        current: _OfficialNode | None = node
        while current is not None:
            styles = _official_style(current.attrs.get("style", ""))
            if current is node:
                color = styles.get("color", color)
            effective_line_through = effective_line_through or (
                "line-through" in styles.get("text-decoration", "")
                or "line-through" in styles.get("text-decoration-line", "")
            )
            if (
                context_text is None
                and "data-official-price-context" in current.attrs
            ):
                context_text = current.attrs["data-official-price-context"]
            current = current.parent
        return {
            "color": color,
            "effectiveLineThrough": effective_line_through,
            "contextText": context_text,
        }


class _OfficialFixturePage:
    def __init__(self, html: str, *, entry_url: str) -> None:
        parser = _OfficialDocumentParser()
        parser.feed(html)
        self.root = parser.root
        self.entry_url = entry_url
        self._url = "about:blank"
        self._active = "store"
        self.goto_calls: list[str] = []
        self.option_clicks: list[str] = []
        self.option_scrolls: list[str] = []
        self.option_labels: list[str] = []
        self.price_evaluations = 0
        self.stock_reads = 0
        self.wait_timeout_milliseconds: list[float] = []
        self.presses: list[str] = []
        self.search_waiting_for_enter = False
        self._product_urls = {
            urljoin(entry_url, node.attrs["href"])
            for node in self.root.descendants()
            if "href" in node.attrs and "/product/" in node.attrs["href"]
        }

    @property
    def url(self) -> str:
        return self._url

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)
        self._url = url
        if url in self._product_urls:
            self.activate("product")
            self._url = url
        else:
            self.activate("store")
            self._url = url

    def title(self) -> str:
        screen_titles = [
            node
            for node in self.root.descendants()
            if node.tag == "title"
            and node.attrs.get("data-screen-title") == self._active
        ]
        if screen_titles:
            return screen_titles[0].text
        titles = _official_select(self.root.descendants(), "title")
        return titles[0].text if titles else ""

    def locator(self, selector: str) -> _OfficialLocator:
        screens = [
            node
            for node in self.root.descendants()
            if node.attrs.get("data-screen") == self._active
        ]
        scope = (
            screens[0].descendants() + screens
            if screens
            else self.root.descendants()
        )
        return _OfficialLocator(self, _official_select(scope, selector))

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_timeout(self, milliseconds: float) -> None:
        self.wait_timeout_milliseconds.append(milliseconds)
        wait_count = len(self.wait_timeout_milliseconds)
        for node in self.root.descendants():
            risk_threshold = node.attrs.get("data-show-after-waits")
            if risk_threshold is not None and wait_count >= int(risk_threshold):
                node.attrs.pop("hidden", None)
                node.attrs.pop("data-show-after-waits", None)

            hydrate_threshold = node.attrs.get("data-hydrate-after-waits")
            if (
                hydrate_threshold is None
                or wait_count < int(hydrate_threshold)
            ):
                continue
            node.attrs.pop("hidden", None)
            node.attrs.pop("data-hydrate-after-waits", None)
            region = _OfficialNode(
                "a",
                {"class": "product-pulldown-btn"},
                node,
            )
            region.text_parts = [node.attrs.pop("data-hydrate-region")]
            prompt = _OfficialNode(
                "div",
                {"class": "product-address-prompt"},
                node,
            )
            prompt.text_parts = ["次日达 送货上门 "]
            stock = _OfficialNode(
                "span",
                {"class": "red"},
                prompt,
            )
            stock.text_parts = ["现货"]
            prompt.children.append(stock)
            prompt.text_parts.append("，预计明天送达")
            node.children.extend((region, prompt))

    def press(self, key: str) -> None:
        self.presses.append(key)
        if key == "Enter" and self.search_waiting_for_enter:
            self.search_waiting_for_enter = False
            self.activate_results()

    def note_price_evaluation(self, source: _OfficialNode) -> None:
        self.price_evaluations += 1
        for node in self.root.descendants():
            cycle_threshold = node.attrs.get(
                "data-cycle-text-after-hand-price-evaluations"
            )
            if (
                source.attrs.get("id") == "pro-price-hand"
                and cycle_threshold is not None
                and self.price_evaluations >= int(cycle_threshold)
            ):
                values = node.attrs["data-cycle-text-values"].split("||")
                index = (
                    (self.price_evaluations - int(cycle_threshold)) // 2
                ) % len(values)
                node.text_parts = [values[index]]
            text_threshold = node.attrs.get(
                "data-switch-text-after-price-evaluations"
            )
            if (
                text_threshold is not None
                and self.price_evaluations == int(text_threshold)
            ):
                node.text_parts = [
                    node.attrs["data-switch-text-to"]
                ]
            threshold = node.attrs.get(
                "data-switch-current-sku-after-price-evaluations"
            )
            if threshold is None or self.price_evaluations != int(threshold):
                continue
            binding_name = (
                "data-skuid"
                if "data-skuid" in node.attrs
                else "data-current-sku"
            )
            node.attrs[binding_name] = node.attrs[
                "data-switch-current-sku-to"
            ]

    def note_stock_read(self) -> None:
        self.stock_reads += 1
        for node in self.root.descendants():
            cycle_threshold = node.attrs.get(
                "data-cycle-text-after-stock-reads"
            )
            if (
                cycle_threshold is not None
                and self.stock_reads >= int(cycle_threshold)
            ):
                values = node.attrs["data-cycle-text-values"].split("||")
                index = (
                    self.stock_reads - int(cycle_threshold)
                ) % len(values)
                node.text_parts = [values[index]]
            threshold = node.attrs.get(
                "data-switch-current-sku-after-stock-reads"
            )
            if threshold is None or self.stock_reads != int(threshold):
                continue
            node.attrs["data-current-sku"] = node.attrs[
                "data-switch-current-sku-to"
            ]

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

    def activate_results(self) -> None:
        search_inputs = [
            node
            for node in self.root.descendants()
            if node.tag == "input"
            and node.visible
            and (
                node.attrs.get("class", "").endswith("-search-input")
                or node.attrs.get("id") == "search-kw"
            )
        ]
        keyword = search_inputs[0].attrs.get("value", "") if search_inputs else ""
        is_live_honor = any(
            node.attrs.get("id") == "search-kw" for node in search_inputs
        )
        parsed = urlsplit(self.entry_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        self.activate("results")
        self._url = (
            f"{origin}/cn/shop/v/search?keyword={quote(keyword)}"
            if is_live_honor
            else f"{origin}/search?q={quote(keyword)}"
        )

    def select_option(self, node: _OfficialNode, option_kind: str) -> None:
        for candidate in self.root.descendants():
            if candidate.attrs.get("data-option-kind") == option_kind:
                candidate.attrs["aria-selected"] = "false"
        node.attrs["aria-selected"] = "true"
        for candidate in self.root.descendants():
            if candidate.attrs.get("data-reveal-after") == option_kind:
                candidate.attrs.pop("hidden", None)
        self.option_clicks.append(option_kind)
        self.option_labels.append(node.text)

    def select_honor_option(self, node: _OfficialNode, option_kind: str) -> None:
        for candidate in self.root.descendants():
            if candidate.attrs.get("data-option-kind") != option_kind:
                continue
            classes = candidate.attrs.get("class", "").split()
            candidate.attrs["class"] = " ".join(
                item for item in classes if item != "selected"
            )
        classes = node.attrs.get("class", "").split()
        node.attrs["class"] = " ".join((*classes, "selected"))
        self.option_clicks.append(option_kind)
        self.option_labels.append(node.text)


class _RejectingOfficialCapture:
    def __init__(self) -> None:
        self.calls = 0

    def capture(self, _request: object) -> None:
        self.calls += 1
        raise AssertionError("site observers must not perform formal capture")


def _official_style(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in value.split(";"):
        if ":" in declaration:
            key, item = declaration.split(":", 1)
            result[key.strip().lower()] = item.strip()
    return result


def _official_pixels(value: str | None, default: float) -> float:
    return default if value is None else float(value.removesuffix("px"))


def _official_select(
    nodes: list[_OfficialNode],
    selector: str,
) -> list[_OfficialNode]:
    parts = selector.strip().split()
    if not parts:
        return []
    matched = [node for node in nodes if _official_matches(node, parts[-1])]
    if len(parts) == 1:
        return matched
    result: list[_OfficialNode] = []
    for node in matched:
        wanted = parts[:-1]
        current = node.parent
        index = len(wanted) - 1
        while current is not None and index >= 0:
            if _official_matches(current, wanted[index]):
                index -= 1
            current = current.parent
        if index < 0:
            result.append(node)
    return result


def _official_matches(node: _OfficialNode, selector: str) -> bool:
    parsed = _OFFICIAL_SELECTOR.fullmatch(selector)
    if parsed is None:
        raise AssertionError(
            f"official fixture harness does not support selector {selector!r}"
        )
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
    for attribute in _OFFICIAL_ATTRIBUTE.finditer(parsed.group("attrs")):
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


@dataclass(frozen=True, slots=True)
class ValidInputs:
    paths: InputPaths
    base_codes: tuple[str, ...]


@pytest.fixture
def valid_inputs(tmp_path: Path) -> ValidInputs:
    codes = ("9101", "9102")
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899], ["9102", "无", "无"]],
    )
    marketing_headers = [f"营销字段{index}" for index in range(1, 9)] + ["物料编码"]
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        marketing_headers,
        [[""] * 8 + ["9101"], [""] * 8 + ["9102"]],
    )
    bop_headers = [f"BOP字段{index}" for index in range(1, 11)] + ["集团一级库编码"]
    bop = save_workbook(tmp_path / "bop.xlsx", bop_headers, [[""] * 10 + ["9101"]])
    return ValidInputs(InputPaths(base, marketing, bop, tmp_path), codes)


@pytest.fixture
def official_case() -> Callable[..., tuple[Any, Any, _OfficialFixturePage, Any]]:
    """Build one saved official-site fixture case without browser or network I/O."""

    from quote_app.sites.catalog import load_site_catalog
    from quote_app.sites.official import OfficialSiteAdapter
    from quote_app.tasks.models import WebsiteChannel, WebsiteTask

    specs = {
        spec.brand: spec
        for spec in load_site_catalog()
        if spec.channel is WebsiteChannel.OFFICIAL
    }

    def build(
        brand: str,
        fixture_state: str,
        *,
        mutate: Callable[[str], str] | None = None,
    ) -> tuple[Any, WebsiteTask, _OfficialFixturePage, _RejectingOfficialCapture]:
        slug, model, ram, storage, color = _OFFICIAL_CASE_DATA[brand]
        fixture_path = _OFFICIAL_FIXTURES / slug / f"{fixture_state}.html"
        html = fixture_path.read_text(encoding="utf-8")
        if mutate is not None:
            html = mutate(html)
        spec = specs[brand]
        page = _OfficialFixturePage(html, entry_url=spec.entry_url)
        task = WebsiteTask(
            task_id=f"official-{slug}-{fixture_state}",
            run_id="run-official",
            source_row_number=2,
            output_row_number=2,
            material_code=f"CODE-{slug}",
            brand=brand,
            model_name=model,
            ram=ram,
            storage=storage,
            color=color,
            channel=WebsiteChannel.OFFICIAL,
        )
        return (
            # This fixture preserves the seven-brand legacy data-driven
            # OfficialSiteAdapter contract. Live brand factory dispatch has
            # independent contract fixtures under official_live/.
            OfficialSiteAdapter(spec),
            task,
            page,
            _RejectingOfficialCapture(),
        )

    return build

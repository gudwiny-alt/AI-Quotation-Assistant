from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from quote_app.sites.locators import visible_locators
from quote_app.sites.matching import (
    capacity_matches,
    color_matches,
    normalize_product_text,
)
from quote_app.sites.prices import (
    PriceCandidate,
    SellingPriceEvidence,
)
from quote_app.tasks.models import WebsiteTask
from quote_app.tasks.retry import LayoutRecognitionError

_HONOR_PRODUCT_PATH = re.compile(r"^/cn/shop/product/[1-9][0-9]*\.html$")
_HONOR_SKU = re.compile(r"^[1-9][0-9]*$")
_HONOR_ATTR_CODE = re.compile(r"^[1-9][0-9]*$")
_IMMEDIATE_MODEL_DELIMITERS = frozenset({"-", "–", "—", "_", "+"})
_IMMEDIATE_MODEL_VARIANTS = (
    "PRO+",
    "PRO",
    "PLUS",
    "ULTRA",
    "MAX",
    "SE",
    "LITE",
    "MINI",
    "GT",
    "NEO",
    "AIR",
    "EDGE",
    "FE",
    "青春版",
    "活力版",
    "竞速版",
    "至尊版",
)
_MAX_CONTEXT_LENGTH = 1000
_PRICE_STYLE_SCRIPT = """
(element) => {
  let current = element;
  let effectiveLineThrough = false;
  const color = window.getComputedStyle(element).color;
  while (current) {
    const style = window.getComputedStyle(current);
    if ((style.textDecorationLine || style.textDecoration || "")
        .includes("line-through")) {
      effectiveLineThrough = true;
    }
    current = current.parentElement;
  }
  return {color, effectiveLineThrough};
}
"""


@dataclass(frozen=True, slots=True)
class HonorStockSnapshot:
    state_text: str
    bounded_context: str
    region_text: str
    locator: Any


class HonorOfficialOverride:
    """Strict observer helpers for the discovered public HONOR shop DOM."""

    search_inputs = ("#search-kw",)
    search_actions = ('input.button.iconfont[type="submit"]',)
    result_regions = ("#mainSaleList",)
    product_cards = ("#mainSaleList li.grid-items",)
    product_links = ("a.thumb",)
    detail_titles = ("h1#pro-name",)
    address_roots = ("#pro-predict.product-address",)
    color_options = (
        '#pro-skus dl.product-choose li[data-attrname="颜色"]'
        "[data-attrcode][data-skuid]",
    )
    version_options = (
        '#pro-skus dl.product-choose li[data-attrname="版本"]'
        "[data-attrcode][data-skuid]",
    )

    @classmethod
    def uses_live_contract(cls, page: Any) -> bool:
        return bool(visible_locators(page, cls.search_inputs))

    @staticmethod
    def is_numeric_product_path(path: str) -> bool:
        return _HONOR_PRODUCT_PATH.fullmatch(path) is not None

    @classmethod
    def card_matches_model(cls, model_name: str, card_text: str) -> bool:
        model = normalize_product_text(model_name)
        candidate = normalize_product_text(card_text)
        if not model or not candidate:
            return False
        search_from = 0
        while (index := candidate.find(model, search_from)) >= 0:
            raw_prefix = candidate[:index]
            raw_remainder = candidate[index + len(model) :]
            if (
                not raw_prefix
                or not (raw_prefix[-1].isascii() and raw_prefix[-1].isalnum())
            ) and cls._card_model_remainder_is_allowed(raw_remainder):
                return True
            search_from = index + len(model)
        return False

    @staticmethod
    def _card_model_remainder_is_allowed(raw_remainder: str) -> bool:
        if not raw_remainder:
            return True
        if raw_remainder[0].isascii() and raw_remainder[0].isalnum():
            return False
        remainder = raw_remainder.lstrip()
        if not remainder:
            return True
        if remainder[0] in _IMMEDIATE_MODEL_DELIMITERS:
            return False
        return not remainder.startswith(_IMMEDIATE_MODEL_VARIANTS)

    @staticmethod
    def detail_matches_task(task: WebsiteTask, detail_text: str) -> bool:
        model = normalize_product_text(task.model_name)
        candidate = normalize_product_text(detail_text)
        if not model or not candidate.startswith(model):
            return False
        remainder = candidate[len(model) :].lstrip()
        if not remainder or remainder[0] in _IMMEDIATE_MODEL_DELIMITERS:
            return False
        return not remainder.upper().startswith(_IMMEDIATE_MODEL_VARIANTS)

    @classmethod
    def select_task_sku(cls, page: Any, task: WebsiteTask) -> None:
        """Select the quoted version and color before reading the live offer."""
        cls._select_unique_option(
            page,
            cls.version_options,
            lambda text: capacity_matches(
                normalize_product_text(text).removeprefix("5G全网通 "),
                task.ram,
                task.storage,
            ),
            semantic_name="HONOR exact version option",
        )
        cls._select_unique_option(
            page,
            cls.color_options,
            lambda text: color_matches(task.color, text),
            semantic_name="HONOR exact color option",
        )

    @staticmethod
    def _select_unique_option(
        page: Any,
        selectors: tuple[str, ...],
        matches: Any,
        *,
        semantic_name: str,
    ) -> None:
        exact = tuple(
            option
            for option in visible_locators(page, selectors)
            if matches(option.inner_text())
        )
        if len(exact) != 1:
            raise LayoutRecognitionError(
                f"{semantic_name} is missing or ambiguous"
            )
        option = exact[0]
        if "selected" in (option.get_attribute("class") or "").split():
            return
        option.scroll_into_view_if_needed()
        option.click()

    @staticmethod
    def require_store_title(page: Any) -> None:
        title = normalize_product_text(page.title())
        if not (
            title.startswith("荣耀商城")
            and title.endswith("荣耀HONOR手机官方网站")
        ):
            raise LayoutRecognitionError(
                "Official HONOR visible store identity does not match"
            )

    @staticmethod
    def require_detail_store_title(page: Any) -> None:
        title = normalize_product_text(page.title())
        if not title.endswith("| 荣耀商城"):
            raise LayoutRecognitionError(
                "Official HONOR product detail seller does not match"
            )

    @classmethod
    def selected_sku(cls, page: Any, task: WebsiteTask) -> str:
        color = _unique_visible(
            page,
            (
                '#pro-skus dl.product-choose '
                'li.selected[data-attrname="颜色"]'
                "[data-attrcode][data-skuid]",
            ),
            semantic_name="selected color",
        )
        version = _unique_visible(
            page,
            (
                '#pro-skus dl.product-choose '
                'li.selected[data-attrname="版本"]'
                "[data-attrcode][data-skuid]",
            ),
            semantic_name="selected version",
        )
        if not color_matches(task.color, color.inner_text()):
            raise LayoutRecognitionError(
                "Official HONOR selected color does not match the task"
            )
        version_text = normalize_product_text(version.inner_text())
        prefix = "5G全网通 "
        if (
            not version_text.startswith(prefix)
            or not capacity_matches(
                version_text.removeprefix(prefix),
                task.ram,
                task.storage,
            )
        ):
            raise LayoutRecognitionError(
                "Official HONOR selected version does not match the task"
            )
        color_skus = _required_sku_set(color, semantic_name="selected color")
        version_skus = _required_sku_set(
            version,
            semantic_name="selected version",
        )
        intersection = color_skus & version_skus
        if len(intersection) != 1:
            raise LayoutRecognitionError(
                "Official HONOR selected SKU intersection is not unique"
            )
        return next(iter(intersection))

    @staticmethod
    def stock_snapshot(page: Any) -> HonorStockSnapshot:
        address = _unique_visible(
            page,
            HonorOfficialOverride.address_roots,
            semantic_name="product address region",
        )
        region = _unique_visible(
            address,
            ("a.product-pulldown-btn",),
            semantic_name="delivery region",
        )
        prompt = _unique_visible(
            address,
            ("div.product-address-prompt",),
            semantic_name="stock context",
        )
        state = _unique_visible(
            prompt,
            ("span.red",),
            semantic_name="stock state",
        )
        state_text = normalize_product_text(state.inner_text())
        context = normalize_product_text(prompt.inner_text())
        region_text = normalize_product_text(region.inner_text())
        if (
            state_text != "现货"
            or not context
            or len(context) > _MAX_CONTEXT_LENGTH
            or context.count("现货") != 1
        ):
            raise LayoutRecognitionError(
                "Official HONOR stock state is unrecognized or conflicting"
            )
        if not region_text or len(region_text) > _MAX_CONTEXT_LENGTH:
            raise LayoutRecognitionError(
                "Official HONOR delivery region is invalid"
            )
        return HonorStockSnapshot(
            state_text=state_text,
            bounded_context=context,
            region_text=region_text,
            locator=state,
        )

    @classmethod
    def attached_address_root(cls, page: Any) -> Any:
        address = page.locator(cls.address_roots[0])
        if address.count() != 1:
            raise LayoutRecognitionError(
                "Official HONOR product address region is missing or ambiguous"
            )
        return address.nth(0)

    @staticmethod
    def price_candidates(page: Any) -> tuple[PriceCandidate, ...]:
        region = _unique_visible(
            page,
            ("div.product-price-info",),
            semantic_name="price context",
        )
        hand = _unique_visible(
            region,
            ("span#pro-price-hand.hand",),
            semantic_name="hand price",
        )
        old = _unique_visible(
            region,
            ("s#pro-price-old",),
            semantic_name="old price",
        )
        context = normalize_product_text(region.inner_text())
        if not context or len(context) > _MAX_CONTEXT_LENGTH:
            raise LayoutRecognitionError(
                "Official HONOR price context is invalid"
            )
        hand_text = normalize_product_text(hand.inner_text())
        hand_prefix = "预估到手价 "
        if not hand_text.startswith(hand_prefix):
            raise LayoutRecognitionError(
                "Official HONOR hand price label is invalid"
            )
        hand_candidate = _price_candidate(
            hand,
            context,
            candidate_text=(
                f"到手价 {hand_text.removeprefix(hand_prefix)}"
            ),
        )
        old_candidate = _price_candidate(old, context)
        if (
            hand_candidate.effective_line_through
            or not old_candidate.effective_line_through
        ):
            raise LayoutRecognitionError(
                "Official HONOR price roles are conflicting"
            )
        return hand_candidate, old_candidate


def _required_sku_set(locator: Any, *, semantic_name: str) -> frozenset[str]:
    attr_code = locator.get_attribute("data-attrcode")
    raw_skus = locator.get_attribute("data-skuid")
    if (
        not isinstance(attr_code, str)
        or _HONOR_ATTR_CODE.fullmatch(attr_code) is None
        or not isinstance(raw_skus, str)
    ):
        raise LayoutRecognitionError(
            f"Official HONOR {semantic_name} SKU binding is invalid"
        )
    skus = tuple(raw_skus.split(","))
    if (
        not skus
        or any(_HONOR_SKU.fullmatch(sku) is None for sku in skus)
        or len(set(skus)) != len(skus)
    ):
        raise LayoutRecognitionError(
            f"Official HONOR {semantic_name} SKU binding is invalid"
        )
    return frozenset(skus)


def _price_candidate(
    locator: Any,
    context: str,
    *,
    candidate_text: str | None = None,
) -> PriceCandidate:
    style = locator.evaluate(_PRICE_STYLE_SCRIPT)
    if not isinstance(style, dict):
        raise LayoutRecognitionError(
            "Official HONOR price style is unavailable"
        )
    color = style.get("color")
    line_through = style.get("effectiveLineThrough")
    if (
        not isinstance(color, str)
        or not color.strip()
        or type(line_through) is not bool
    ):
        raise LayoutRecognitionError(
            "Official HONOR price style is invalid"
        )
    return PriceCandidate(
        text=locator.inner_text() if candidate_text is None else candidate_text,
        context=context,
        visible=locator.is_visible(),
        computed_color=color,
        selling_evidence=(
            SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE
        ),
        effective_line_through=line_through,
    )


def _unique_visible(
    scope: Any,
    selectors: tuple[str, ...],
    *,
    semantic_name: str,
) -> Any:
    matches = visible_locators(scope, selectors)
    if len(matches) != 1:
        raise LayoutRecognitionError(
            f"Official HONOR {semantic_name} is missing or ambiguous"
        )
    return matches[0]

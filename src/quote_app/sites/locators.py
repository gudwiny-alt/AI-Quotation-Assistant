from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from quote_app.tasks.retry import LayoutRecognitionError

JD_STORE_MARKERS = (
    ".jLogo .logo-m",
    ".shop-name",
)
JD_STORE_SEARCH_CONTAINERS = (
    ".i-search .form",
)
JD_SEARCH_INPUTS = (
    "#key01",
    "#key",
)
JD_SEARCH_ACTIONS = (
    "input.button01[value=\"搜本店\"]",
)
JD_RESULT_REGIONS = (
    "#J_goodsList",
    "#comProlist",
    ".shop-search-result",
    ".jSearchListArea",
)
JD_PRODUCT_CARDS = (
    ".gl-item",
    ".goods-item",
    ".jItem",
)
JD_PRODUCT_TITLES = (
    ".p-name em",
    ".p-name",
)
JD_PRODUCT_LINKS = (
    ".p-name a",
    "a.goods-link",
    'a[href*="item.jd.com/"]',
)
JD_EMPTY_RESULTS = (
    ".search-empty",
    ".no-result",
)
JD_DETAIL_TITLES = (
    ".sku-name",
    ".itemInfo-wrap .sku-name",
)
JD_MODERN_DETAIL_TITLES = (
    ".page-right-skuname",
    ".sku-title-name",
)
JD_DETAIL_SELLER_MARKERS = (
    ".shop-name",
)
JD_MODERN_DETAIL_SELLER_MARKERS = (
    ".shop-plugin",
)
JD_MODERN_SKU_OPTIONS = (
    ".specification-item-sku, [data-sku-name]",
)
JD_MODERN_CURRENT_SKU_SELLING_PRICES = (
    ".product-price--main",
    "span.product-price--gray-line-through",
)
JD_MODERN_DELIVERY_REGIONS = (
    ".logistics-delivery-time",
)
JD_CURRENT_SKU_MARKERS = (
    ".choose-attrs[data-current-sku]",
)
JD_CAPACITY_OPTIONS = (
    ".choose-attrs .p-choose[data-type=\"capacity\"] .item",
    ".J-sku-capacity .item",
)
JD_COLOR_OPTIONS = (
    ".choose-attrs .p-choose[data-type=\"color\"] .item",
    ".J-sku-color .item",
)
JD_STOCK_STATES = (
    ".stock-state",
    "#store-prompt",
)
JD_DELIVERY_REGIONS = (
    ".jd-delivery-region",
)
JD_CURRENT_SKU_SELLING_PRICES = (
    ".summary-price .p-price",
    ".price-summary .current-price",
)
JD_LOGIN_MARKERS = (
    "#loginname",
    "form#formlogin",
    ".login-wrap",
)
JD_RISK_CONTROL_MARKERS = (
    ".JDJRV-wrap",
    ".risk-control",
    "iframe[src*=\"captcha\"]",
)

TMALL_STORE_MARKERS = (
    "a.slogo-shopname",
)
TMALL_STORE_SEARCH_CONTAINERS = (
    'form[name="searchTop"]',
)
TMALL_STORE_SEARCH_FORMS = (
    'form[name="SearchForm"]',
)
TMALL_SEARCH_INPUTS = (
    "#mq",
    'form[name="SearchForm"] input.navsearch-text[name="keyword"]',
)
TMALL_SEARCH_ACTIONS = (
    "#J_CurrShopBtn",
)
TMALL_RESULT_REGIONS = (
    "#J_ShopSearchResult",
)
TMALL_PRODUCT_CARDS = (
    "dl.item",
)
TMALL_PRODUCT_TITLES = (
    "a.item-name.J_TGoldData",
)
TMALL_PRODUCT_LINKS = (
    "a.item-name.J_TGoldData",
)
TMALL_EMPTY_RESULTS: tuple[str, ...] = ()
TMALL_DETAIL_TITLES = (
    '[class^="ItemTitle--"]',
    '[class*="ItemTitle--"]',
    ".tb-main-title",
    "#J_Title",
)
TMALL_DETAIL_SELLER_MARKERS = (
    'span[class^="shopName--"]',
)
TMALL_CURRENT_SKU_MARKERS = (
    "#SkuPanel_tbpcDetail_ssr2025",
)
TMALL_SKU_OPTION_ROOTS = (
    "#skuOptionsArea",
)
TMALL_SKU_GROUPS = (
    '[class^="skuItem--"]',
)
TMALL_SKU_GROUP_LABELS = (
    '[class^="ItemLabel--"]',
)
TMALL_SKU_VALUES = (
    '[class^="valueItem--"]',
)
TMALL_STOCK_STATES = (
    '[class^="skuWrapper--"]',
)
TMALL_DELIVERY_REGIONS = (
    ".tmall-delivery-region",
)
TMALL_CURRENT_SKU_SELLING_PRICES = (
    '#tbpcDetail_SkuPanelRightWrap [class^="highlightPrice--"]',
)
TMALL_LOGIN_MARKERS = (
    "#tmall-login-form",
    ".tmall-login-panel",
    'input[placeholder*="账号名"]',
    'input[placeholder*="登录密码"]',
    'iframe[src*="login.taobao.com"]',
)
TMALL_RISK_CONTROL_MARKERS = (
    ".tmall-security-check",
    "iframe[src*=\"captcha\"]",
)
TMALL_SYSTEM_ERROR_MARKERS = (
    ".tmall-system-error",
)


def visible_locators(scope: Any, selectors: Sequence[str]) -> tuple[Any, ...]:
    """Return the first bounded selector family's visible elements."""

    for selector in selectors:
        locator = scope.locator(selector)
        visible = tuple(
            candidate
            for index in range(locator.count())
            if (candidate := locator.nth(index)).is_visible()
        )
        if visible:
            return visible
    return ()


def unique_visible_locator(
    scope: Any,
    selectors: Sequence[str],
    *,
    semantic_name: str,
) -> Any:
    """Resolve one visible semantic element or fail closed."""

    visible = visible_locators(scope, selectors)
    if len(visible) != 1:
        raise LayoutRecognitionError(
            f"JD {semantic_name} structural element is missing or ambiguous"
        )
    return visible[0]

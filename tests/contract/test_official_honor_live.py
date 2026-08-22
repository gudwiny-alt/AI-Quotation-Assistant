from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

import pytest

from quote_app.tasks.models import BusinessOutcome
from quote_app.tasks.retry import LayoutRecognitionError
from quote_app.tasks.retry import NonRetryableTechnicalError
from quote_app.tasks.retry import SecurityVerificationRequired
from quote_app.sites.official_overrides.honor import HonorOfficialOverride

_PRODUCT_ID = "10086164863190"
_CURRENT_SKU = "10086516771847"
_OTHER_COLOR_SKU = "10086898967807"
_OTHER_VERSION_SKU = "10086836771094"
def _live_honor_html(
    *,
    detail_path: str = f"/cn/shop/product/{_PRODUCT_ID}.html",
    card_model: str = "荣耀Magic8",
    detail_model: str = "荣耀Magic8",
    result_keyword: str = "荣耀Magic8",
    selected_color_skus: str = f"{_CURRENT_SKU},{_OTHER_COLOR_SKU}",
    selected_version_skus: str = f"{_CURRENT_SKU},{_OTHER_VERSION_SKU}",
    selected_package_skus: str | None = None,
    selected_sku_switch_after_evaluations: int | None = None,
    stock_text: str = "现货",
    region_text: str | None = "福建 > 福州 > 台江",
    duplicate_region: bool = False,
    cycling_region: bool = False,
    cycling_region_during_final_price: bool = False,
    hand_price_in_context: bool = True,
    hand_price_switch_after_evaluations: int | None = None,
    address_root_count: int = 1,
    address_initially_hidden: bool = False,
    address_hydrate_after_waits: int | None = None,
    risk_after_waits: int | None = None,
    arrival_notice: bool = False,
    static_current_price: bool = False,
) -> str:
    switch_attrs = ""
    if hand_price_switch_after_evaluations is not None:
        switch_attrs = (
            " data-switch-text-after-price-evaluations="
            f'"{hand_price_switch_after_evaluations}"'
            ' data-switch-text-to="预估到手价 ¥4599.00"'
        )
    hand_price = (
        '<span id="pro-price-hand" class="hand" '
        f'style="color:rgb(202,20,29)"{switch_attrs}>'
        "预估到手价 ¥4499.00</span>"
    )
    selected_sku_switch_attrs = ""
    if selected_sku_switch_after_evaluations is not None:
        selected_sku_switch_attrs = (
            " data-switch-current-sku-after-price-evaluations="
            f'"{selected_sku_switch_after_evaluations}"'
            ' data-switch-current-sku-to="10000000000009"'
        )
    price_context = (
        f'<div class="product-price-info">{hand_price}'
        '<s id="pro-price-old" '
        'style="color:rgb(164,164,164);text-decoration:line-through">'
        "¥ 4999.00</s></div>"
    )
    if not hand_price_in_context:
        price_context = (
            f'<div class="promotion-price">{hand_price}</div>'
            '<div class="product-price-info">'
            '<s id="pro-price-old" '
            'style="color:rgb(164,164,164);text-decoration:line-through">'
            "¥ 4999.00</s></div>"
        )
    if static_current_price:
        price_context = (
            '<div class="product-price-info" '
            'style="color:rgb(0,0,0)">¥1799.00</div>'
        )
    region_attrs = ""
    if cycling_region:
        region_attrs = (
            ' data-cycle-text-after-stock-reads="2"'
            ' data-cycle-text-values="广东 > 深圳 > 龙岗区||福建 > 福州 > 台江"'
        )
    if cycling_region_during_final_price:
        region_attrs = (
            ' data-cycle-text-after-hand-price-evaluations="5"'
            ' data-cycle-text-values="广东 > 深圳 > 龙岗区||福建 > 福州 > 台江"'
        )
    region_markup = ""
    if region_text is not None:
        region_markup = (
            f'<a class="product-pulldown-btn"{region_attrs}>'
            f"{region_text}</a>"
        )
        if duplicate_region:
            region_markup += (
                '<a class="product-pulldown-btn">'
                f"{region_text}</a>"
            )
    if address_hydrate_after_waits is not None:
        hidden = " hidden" if address_initially_hidden else ""
        address_markup = (
            f'<div id="pro-predict" class="product-address"{hidden} '
            f'data-hydrate-after-waits="{address_hydrate_after_waits}" '
            f'data-hydrate-region="{region_text or ""}"></div>'
        )
    else:
        hidden = " hidden" if address_initially_hidden else ""
        address_markup = (
            f'<div id="pro-predict" class="product-address"{hidden}>'
            '<div class="product-pulldown-main relative">'
            f"{region_markup}</div>"
            '<div class="product-address-prompt">'
            "次日达 送货上门 "
            f'<span class="red">{stock_text}</span>，预计明天送达'
            "</div></div>"
        )
    address_markup *= address_root_count
    risk_markup = (
        '<div data-official-role="risk-control" hidden '
        f'data-show-after-waits="{risk_after_waits}">安全验证</div>'
        if risk_after_waits is not None
        else ""
    )
    arrival_notice_markup = (
        '<a class="product-button02">到货通知</a>'
        if arrival_notice
        else ""
    )
    return f"""<!doctype html>
<html>
  <head>
    <title data-screen-title="store">荣耀商城 - 当前产品 | 荣耀HONOR手机官方网站</title>
    <title data-screen-title="results">荣耀商城 - 当前产品 | 荣耀HONOR手机官方网站</title>
    <title data-screen-title="product">荣耀Magic8 - 自营官方商城，全国联保售后无忧 | 荣耀商城</title>
  </head>
  <body>
    <main data-screen="store">
      <input id="search-kw" type="text">
      <input class="button iconfont" type="submit" data-action="search">
    </main>
    <main data-screen="results" hidden>
      <input id="search-kw" type="text" value="{result_keyword}">
      <ul id="mainSaleList">
        <li class="grid-items">
          <a class="thumb" href="{detail_path}">
            {card_model} 第五代骁龙8至尊版 预估到手价¥ 4499 ¥ 4999
          </a>
        </li>
      </ul>
    </main>
    <main data-screen="product" hidden>
      <h1 id="pro-name">{detail_model} 12GB+256GB 绒黑色 双卡 全网通版</h1>
      <div id="pro-skus">
        <div class="online-attr">
          <dl id="colorPackage" class="product-choose">
            <label class="custom-label">选择颜色</label>
            <li class="attr1 selected" data-option-kind="honor-color" data-attrname="颜色"
                data-attrcode="152138"
                data-skuid="{selected_color_skus}"
                {selected_sku_switch_attrs}>绒黑色</li>
            <li class="attr13" data-option-kind="honor-color" data-attrname="颜色"
                data-attrcode="152138"
                data-skuid="{_OTHER_COLOR_SKU}">天青釉</li>
          </dl>
          <dl class="product-choose">
            <label class="custom-label">选择版本</label>
            <li class="attr2 selected" data-option-kind="honor-version" data-attrname="版本"
                data-attrcode="733605"
                data-skuid="{selected_version_skus}">
              5G全网通 12GB+256GB
            </li>
            <li class="attr5" data-option-kind="honor-version" data-attrname="版本"
                data-attrcode="733605"
                data-skuid="{_OTHER_COLOR_SKU}">5G全网通 12GB+512GB</li>
          </dl>
          {"" if selected_package_skus is None else f'''<dl class="product-choose">
            <label class="custom-label">选择套餐</label>
            <li class="attr3 selected" data-attrname="套餐"
                data-attrcode="733606"
                data-skuid="{selected_package_skus}">官方标配</li>
          </dl>'''}
        </div>
      </div>
      {address_markup}
      {risk_markup}
      {arrival_notice_markup}
      {price_context}
    </main>
  </body>
</html>
"""


def _live_case(
    official_case: Any,
    *,
    html: str,
    task_model: str = "荣耀Magic8",
) -> tuple[Any, Any, Any]:
    adapter, task, page, _capture = official_case(
        "HONOR",
        "normal",
        mutate=lambda _fixture: html,
    )
    task = replace(
        task,
        model_name=task_model,
        ram="12GB",
        storage="256GB",
        color="绒黑色",
    )
    return adapter, task, page


def test_honor_live_default_selection_returns_bound_hand_price(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499.00")
    assert observation.url == (
        f"https://www.honor.com/cn/shop/product/{_PRODUCT_ID}.html"
    )
    assert observation.semantic_state.canonical_url == observation.url
    assert observation.semantic_state.brand == "HONOR"
    assert observation.semantic_state.model_name == "荣耀Magic8"
    assert observation.semantic_state.capacity == "12GB+256GB"
    assert observation.semantic_state.color == "绒黑色"
    assert observation.semantic_state.current_sku == _CURRENT_SKU
    assert observation.semantic_state.region == "福建 > 福州 > 台江"
    assert observation.semantic_state.stock_state == "现货"
    assert observation.semantic_state.price == Decimal("4499.00")
    assert observation.semantic_state.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.semantic_state.css_rectangles == ()
    assert page.option_clicks == []


def test_honor_live_accepts_a_unique_marketing_color_for_a_base_color(
    official_case: Any,
) -> None:
    """A base-table colour may be a generic colour name used by one SKU."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace("绒黑色", "幻夜黑"),
    )
    task = replace(task, color="黑色")

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499.00")
    assert observation.semantic_state.color == "黑色"


def test_honor_live_prices_and_captures_an_arrival_notice_product(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            address_initially_hidden=True,
            arrival_notice=True,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499.00")
    assert observation.semantic_state.current_sku == _CURRENT_SKU
    assert observation.semantic_state.region == "福建 > 福州 > 台江"
    assert observation.semantic_state.stock_state == "到货通知"
    assert observation.css_rectangles == ()

    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )
    assert reader() == observation.semantic_state


def test_honor_live_reads_a_single_visible_price_when_arrival_notice_is_shown(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            address_initially_hidden=True,
            arrival_notice=True,
            static_current_price=True,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("1799.00")
    assert observation.semantic_state.stock_state == "到货通知"
    reader = adapter.verified_state_reader(
        task,
        cast(Any, page),
        observation.semantic_state,
    )
    assert reader() == observation.semantic_state


def test_honor_live_uses_the_selected_package_to_disambiguate_matching_skus(
    official_case: Any,
) -> None:
    """A selected package must not block an otherwise matching device quote."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            selected_color_skus=f"{_CURRENT_SKU},{_OTHER_COLOR_SKU}",
            selected_version_skus=f"{_CURRENT_SKU},{_OTHER_COLOR_SKU}",
            selected_package_skus=_CURRENT_SKU,
            address_initially_hidden=True,
            arrival_notice=True,
            static_current_price=True,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("1799.00")
    assert observation.semantic_state.current_sku == _CURRENT_SKU


def test_honor_live_selects_official_ai_package_when_the_task_requires_it(
    official_case: Any,
) -> None:
    package_options = f'''<dl class="product-choose">
            <label class="custom-label">选择套餐</label>
            <li class="attr3 selected" data-option-kind="honor-package"
                data-attrname="套餐" data-attrcode="733606"
                data-skuid="{_CURRENT_SKU}">官方标配</li>
            <li class="attr4" data-option-kind="honor-package"
                data-attrname="套餐" data-attrcode="733606"
                data-skuid="{_CURRENT_SKU}">官方标配(AI版)</li>
          </dl>'''
    html = _live_honor_html(
        selected_color_skus=_CURRENT_SKU,
        selected_version_skus=_CURRENT_SKU,
    ).replace(
        "          \n        </div>",
        f"          {package_options}\n        </div>",
    )
    adapter, task, page = _live_case(official_case, html=html)
    task = replace(task, requires_ai_package=True)

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_labels == ["官方标配(AI版)"]


def test_honor_live_opens_the_verified_product_in_the_controlled_page(
    official_case: Any,
) -> None:
    """The visible matching card drives same-page detail navigation."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(),
    )

    adapter.observe(task, cast(Any, page))

    assert page.thumb_clicks == []
    assert page.goto_calls[-1] == (
        f"https://www.honor.com/cn/shop/product/{_PRODUCT_ID}.html"
    )


def test_honor_live_keeps_search_on_the_controlled_page(
    official_case: Any,
) -> None:
    """The HONOR search must not depend on a site-created browser tab."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(),
    )

    adapter.observe(task, cast(Any, page))

    assert page.goto_calls[1] == (
        "https://www.honor.com/cn/shop/v/search?keyword=%E8%8D%A3%E8%80%80Magic8"
    )
    assert page.presses == []


def test_honor_live_enters_detail_from_current_product_link_card(
    official_case: Any,
) -> None:
    """Current HONOR search cards are product links, not grid-items thumbnails."""
    modern_card = f"""
      <div class="pro-panels">
        <a href="/cn/shop/product/{_PRODUCT_ID}.html">
          荣耀Power2 预估到手价 ¥2699 起 ¥3299 多款可选
        </a>
      </div>
    """
    html = _live_honor_html(
        card_model="荣耀Power2",
        detail_model="荣耀Power2",
        result_keyword="荣耀Power2",
    ).replace(
        f'''      <ul id="mainSaleList">
        <li class="grid-items">
          <a class="thumb" href="/cn/shop/product/{_PRODUCT_ID}.html">
            荣耀Power2 第五代骁龙8至尊版 预估到手价¥ 4499 ¥ 4999
          </a>
        </li>
      </ul>''',
        modern_card,
    )
    adapter, task, page = _live_case(
        official_case,
        task_model="荣耀Power2",
        html=html,
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


def test_honor_live_uses_the_baseline_contract_before_any_new_readiness_gate(
    official_case: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the proven HONOR storefront entry route as a release baseline."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(),
    )
    contract_checks: list[str] = []

    def _baseline_live_contract(_page: Any) -> bool:
        contract_checks.append("checked")
        return True

    monkeypatch.setattr(
        HonorOfficialOverride,
        "uses_live_contract",
        staticmethod(_baseline_live_contract),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert contract_checks == ["checked"]
    assert observation.outcome is BusinessOutcome.PRICE_FOUND


def test_honor_live_waits_for_store_search_to_render(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            '<input id="search-kw" type="text">',
            '<input id="search-kw" type="text" hidden '
            'data-show-after-waits="1">',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.wait_timeout_milliseconds[0] == 500


def test_honor_live_waits_for_search_results_to_render(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            '<ul id="mainSaleList">',
            '<ul id="mainSaleList" hidden data-show-after-waits="1">',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert 500 in page.wait_timeout_milliseconds


def test_honor_live_uses_same_page_search_when_store_submit_requires_enter(
    official_case: Any,
) -> None:
    """The official route removes the site submit button's tab-opening behavior."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            'data-action="search"',
            'data-action="search" data-requires-enter="true"',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.presses == []
    assert page.goto_calls[1].startswith(
        "https://www.honor.com/cn/shop/v/search?keyword="
    )


def test_honor_live_uses_verified_matching_card_without_search_url(
    official_case: Any,
) -> None:
    """A matching official product card is sufficient without a route change."""
    home_cards = f"""
      <ul id="mainSaleList">
        <li class="grid-items"><a class="thumb" href="/cn/shop/product/{_PRODUCT_ID}.html">
          荣耀Magic8 首页推荐商品
        </a></li>
      </ul>
    """
    html = _live_honor_html().replace(
        '      <input class="button iconfont" type="submit" data-action="search">\n'
        "    </main>",
        '      <input class="button iconfont" type="submit" data-action="search" '
        'data-requires-enter="true">\n'
        f"{home_cards}"
        "    </main>",
    )
    adapter, task, page = _live_case(official_case, html=html)
    original_activate_results = page.activate_results

    def activate_search_results() -> None:
        original_activate_results()
        page._url = "https://www.honor.com/cn/shop/v/search?keyword=荣耀Magic8"

    page.activate_results = activate_search_results

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.presses == []


def test_honor_live_accepts_space_normalized_brandless_search_keyword(
    official_case: Any,
) -> None:
    """HONOR renders `荣耀Magic8` as the search query `Magic 8`."""
    adapter, task, page = _live_case(official_case, html=_live_honor_html())
    original_activate_results = page.activate_results

    def activate_search_results() -> None:
        original_activate_results()
        page._url = "https://www.honor.com/cn/shop/v/search?keyword=Magic%208"

    page.activate_results = activate_search_results

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


def test_honor_live_accepts_root_host_search_redirect(
    official_case: Any,
) -> None:
    """HONOR may redirect its official search page from www to its root host."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            detail_path=(
                f"https://honor.com/cn/shop/product/{_PRODUCT_ID}.html"
            ),
        ),
    )
    page._product_urls.add(
        f"https://www.honor.com/cn/shop/product/{_PRODUCT_ID}.html"
    )
    original_activate_results = page.activate_results

    def activate_search_results() -> None:
        original_activate_results()
        page._url = "https://honor.com/cn/shop/v/search?keyword=Magic8"

    page.activate_results = activate_search_results

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url == (
        f"https://www.honor.com/cn/shop/product/{_PRODUCT_ID}.html"
    )


def test_honor_live_enters_detail_when_target_card_renders_without_search_url(
    official_case: Any,
) -> None:
    """A rendered exact product card is sufficient when HONOR keeps its URL."""
    adapter, task, page = _live_case(official_case, html=_live_honor_html())
    original_activate_results = page.activate_results

    def activate_search_results_without_url_change() -> None:
        original_activate_results()
        page._url = adapter.spec.entry_url

    page.activate_results = activate_search_results_without_url_change

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


def test_honor_live_deduplicates_repeated_base_card_among_variant_results(
    official_case: Any,
) -> None:
    """Search results may contain repeats plus Plus/Pro variants of a model."""
    extra_cards = f"""
        <li class="grid-items">
          <a class="thumb" href="/cn/shop/product/{_PRODUCT_ID}.html">
            荣耀畅玩80 6GB+128GB 碧空蓝
          </a>
        </li>
        <li class="grid-items">
          <a class="thumb" href="/cn/shop/product/10086164863191.html">
            荣耀畅玩80 Plus 6GB+128GB
          </a>
        </li>
        <li class="grid-items">
          <a class="thumb" href="/cn/shop/product/10086164863192.html">
            荣耀畅玩80 Pro 8GB+256GB
          </a>
        </li>
    """
    html = _live_honor_html(
        card_model="荣耀畅玩80",
        detail_model="荣耀畅玩80",
        result_keyword="荣耀畅玩80",
    ).replace("      </ul>", f"{extra_cards}      </ul>")
    adapter, task, page = _live_case(
        official_case,
        task_model="荣耀畅玩80",
        html=html,
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


def test_honor_live_waits_for_product_title_to_render(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            '<h1 id="pro-name">',
            '<h1 id="pro-name" hidden data-show-after-waits="1">',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert 500 in page.wait_timeout_milliseconds


def test_honor_live_accepts_sku_suffix_in_detail_title_and_selects_task_sku(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(),
    )
    task = replace(
        task,
        ram="12GB",
        storage="512GB",
        color="天青釉",
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_clicks == ["honor-version", "honor-color"]
    assert page.option_labels == ["5G全网通 12GB+512GB", "天青釉"]


def test_honor_live_enters_detail_for_capacity_after_base_model(
    official_case: Any,
) -> None:
    """A whitespace-separated capacity is product metadata, not a new model."""
    adapter, task, page = _live_case(
        official_case,
        task_model="荣耀Power2",
        html=_live_honor_html(
            card_model="荣耀Power2 12GB+256GB",
            detail_model="荣耀Power2",
            result_keyword="荣耀Power2",
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.url == (
        f"https://www.honor.com/cn/shop/product/{_PRODUCT_ID}.html"
    )
    assert page.goto_calls[-1] == observation.url


def test_honor_live_matches_caption_outside_the_thumbnail_link(
    official_case: Any,
) -> None:
    """HONOR product labels need not be descendants of the image link."""
    card_model = "荣耀Power2 12GB+256GB"
    card_markup = (
        f'<a class="thumb" href="/cn/shop/product/{_PRODUCT_ID}.html">'
        '<img src="power2.png"></a>'
        f'<a class="product-name">新品 {card_model} 幻夜黑 双卡 全网通版</a>'
    )
    original_markup = (
        f'<a class="thumb" href="/cn/shop/product/{_PRODUCT_ID}.html">\n'
        f"            {card_model} 第五代骁龙8至尊版 预估到手价¥ 4499 ¥ 4999\n"
        "          </a>"
    )
    adapter, task, page = _live_case(
        official_case,
        task_model="荣耀Power2",
        html=_live_honor_html(
            card_model=card_model,
            detail_model="荣耀Power2",
            result_keyword="荣耀Power2",
        ).replace(original_markup, card_markup),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


def test_honor_live_rejects_variant_in_detail_title(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(detail_model="荣耀Magic8 Pro"),
    )

    with pytest.raises(LayoutRecognitionError, match="detail model"):
        adapter.observe(task, cast(Any, page))


def test_honor_live_waits_for_attached_address_root_to_hydrate(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            address_initially_hidden=True,
            address_hydrate_after_waits=3,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499.00")
    assert page.wait_timeout_milliseconds[:3] == [500, 500, 500]


def test_honor_live_bounds_wait_for_address_root_hydration(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(address_initially_hidden=True),
    )

    with pytest.raises(LayoutRecognitionError, match="hydrate|stable|address"):
        adapter.observe(task, cast(Any, page))

    assert page.wait_timeout_milliseconds == [500] * 12


@pytest.mark.parametrize("address_root_count", [0, 2])
def test_honor_live_rejects_missing_or_multiple_attached_address_roots_without_wait(
    address_root_count: int,
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(address_root_count=address_root_count),
    )

    with pytest.raises(LayoutRecognitionError, match="address|region"):
        adapter.observe(task, cast(Any, page))

    assert page.wait_timeout_milliseconds == []


def test_honor_live_rechecks_risk_control_after_each_hydration_wait(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            address_initially_hidden=True,
            address_hydrate_after_waits=3,
            risk_after_waits=1,
        ),
    )

    with pytest.raises(SecurityVerificationRequired):
        adapter.observe(task, cast(Any, page))

    assert page.wait_timeout_milliseconds == [500]


def test_honor_live_rechecks_price_before_return_after_stable_snapshot(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            hand_price_switch_after_evaluations=4,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.price == Decimal("4599.00")
    assert observation.semantic_state.price == Decimal("4599.00")


def test_honor_live_clamps_region_around_final_price_read(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            cycling_region_during_final_price=True,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="stable"):
        adapter.observe(task, cast(Any, page))


def test_honor_live_clamps_selected_sku_around_final_price_read(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            selected_sku_switch_after_evaluations=5,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="SKU|intersection"):
        adapter.observe(task, cast(Any, page))


@pytest.mark.parametrize(
    ("region_text", "duplicate_region"),
    [
        (None, False),
        ("   ", False),
        ("福建 > 福州 > 台江", True),
        ("福" * 1001, False),
    ],
    ids=("missing", "blank", "multiple", "overlong"),
)
def test_honor_live_requires_one_bounded_delivery_region(
    region_text: str | None,
    duplicate_region: bool,
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            region_text=region_text,
            duplicate_region=duplicate_region,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="region|location"):
        adapter.observe(task, cast(Any, page))


def test_honor_live_rejects_delivery_region_that_never_stabilizes(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(cycling_region=True),
    )

    with pytest.raises(LayoutRecognitionError, match="stable|region"):
        adapter.observe(task, cast(Any, page))


def test_honor_live_skips_non_numeric_product_route(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            detail_path="/cn/shop/product/magic8.html",
        ),
    )

    with pytest.raises(NonRetryableTechnicalError, match="正式商品页"):
        adapter.observe(task, cast(Any, page))

    assert page.thumb_clicks == []
    assert len(page.goto_calls) == 2
    assert page.presses == []


def test_honor_live_rejects_variant_card_for_base_model(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            card_model="荣耀Magic8 Pro",
            detail_model="荣耀Magic8 Pro",
        ),
    )

    with pytest.raises(NonRetryableTechnicalError) as failure:
        adapter.observe(task, cast(Any, page))

    assert failure.value.code == "HONOR_PRODUCT_MATCH_MISSING"
    trace = getattr(failure.value, "honor_search_trace")
    assert [record["stage"] for record in trace] == ["after_same_page_search"]
    assert all(record["search_url"] == page.url for record in trace)
    assert all(record["visible_card_count"] == 1 for record in trace)
    assert all(record["model_card_count"] == 0 for record in trace)
    assert all(record["ready_card_count"] == 0 for record in trace)
    assert trace[0]["cards"] == [
        {
            "text": "荣耀Magic8 Pro 第五代骁龙8至尊版 预估到手价¥ 4499 ¥ 4999",
            "model_matches": False,
            "thumb_count": 1,
            "hrefs": [f"/cn/shop/product/{_PRODUCT_ID}.html"],
        }
    ]
    assert "/cn/shop/v/search?keyword=" in page.url


def test_honor_live_stops_once_when_multiple_cards_match_base_model(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            "</ul>",
            """
        <li class=\"grid-items\">
          <a class=\"thumb\" href=\"/cn/shop/product/10086164863191.html\">
            荣耀Magic8 第五代骁龙8至尊版 预估到手价¥ 4499 ¥ 4999
          </a>
        </li>
      </ul>""",
        ),
    )

    with pytest.raises(NonRetryableTechnicalError) as failure:
        adapter.observe(task, cast(Any, page))

    assert failure.value.code == "HONOR_PRODUCT_MATCH_AMBIGUOUS"
    assert "/cn/shop/v/search?keyword=" in page.url


def test_honor_live_prefers_an_available_exact_model_card_over_temporary_unavailable(
    official_case: Any,
) -> None:
    """A temporary-unavailable duplicate must not block an available base model."""
    html = _live_honor_html().replace(
        "</ul>",
        """
        <li class="grid-items">
          <a class="thumb" href="/cn/shop/product/10086164863191.html">
            暂时缺货 荣耀Magic8 ¥6299 起 多款可选
          </a>
        </li>
        <li class="grid-items">
          <a class="thumb" href="/cn/shop/product/10086164863192.html">
            荣耀Magic8 RSR 保时捷设计 ¥7999 起 多款可选
          </a>
        </li>
      </ul>""",
    )
    adapter, task, page = _live_case(official_case, html=html)

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


def test_honor_live_enters_a_temporarily_unavailable_card_when_it_is_the_only_match(
    official_case: Any,
) -> None:
    """A sole temporary-unavailable model remains eligible for price evidence."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            "荣耀Magic8 第五代骁龙8至尊版 预估到手价¥ 4499 ¥ 4999",
            "暂时缺货 荣耀Magic8 ¥6299 起 多款可选",
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")


@pytest.mark.parametrize(
    ("task_model", "card_model"),
    [
        ("荣耀Magic8", "荣耀Magic8-Pro"),
        ("荣耀Magic8 Pro", "荣耀Magic8 Pro+"),
        ("荣耀Magic8", "荣耀Magic8 RSR 保时捷设计"),
    ],
)
def test_honor_live_rejects_immediate_variant_delimiters_at_card_stage(
    task_model: str,
    card_model: str,
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        task_model=task_model,
        html=_live_honor_html(
            card_model=card_model,
            detail_model=card_model,
            result_keyword=task_model,
        ),
    )

    with pytest.raises(NonRetryableTechnicalError) as failure:
        adapter.observe(task, cast(Any, page))

    assert failure.value.code == "HONOR_PRODUCT_MATCH_MISSING"


@pytest.mark.parametrize(
    ("selected_color_skus", "selected_version_skus"),
    [
        ("10000000000001", "10000000000002"),
        (
            "10000000000001,10000000000002",
            "10000000000001,10000000000002",
        ),
    ],
    ids=("empty-intersection", "multiple-intersection"),
)
def test_honor_live_requires_unique_selected_sku_intersection(
    selected_color_skus: str,
    selected_version_skus: str,
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(
            selected_color_skus=selected_color_skus,
            selected_version_skus=selected_version_skus,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="SKU|intersection"):
        adapter.observe(task, cast(Any, page))


def test_honor_live_rejects_conflicting_exact_stock_text(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(stock_text="现货 售罄"),
    )

    with pytest.raises(LayoutRecognitionError, match="stock"):
        adapter.observe(task, cast(Any, page))


def test_honor_live_rejects_hand_price_outside_bounded_context(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(hand_price_in_context=False),
    )

    with pytest.raises(LayoutRecognitionError, match="price|context"):
        adapter.observe(task, cast(Any, page))

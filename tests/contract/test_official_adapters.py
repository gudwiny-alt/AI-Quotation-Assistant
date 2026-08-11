from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pytest

from quote_app.sites.catalog import SUPPORTED_BRANDS, load_site_catalog
from quote_app.sites.official import (
    OFFICIAL_CAPACITY_STRATEGIES,
    OFFICIAL_POLICIES,
    OfficialSiteAdapter,
)
from quote_app.sites.prices import PricePolicy
from quote_app.sites.protocol import SiteObservationAdapter
from quote_app.sites.registry import RegisteredSiteAdapter, adapter_for
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
)
from quote_app.tasks.retry import (
    LayoutRecognitionError,
    LoginRequired,
    NonRetryableTechnicalError,
    SecurityVerificationRequired,
)

_NORMAL_PRICES = {
    "小米": Decimal("4399"),
    "HONOR": Decimal("3999"),
    "华为": Decimal("4999"),
    "维沃": Decimal("4499"),
    "欧珀": Decimal("4599"),
    "苹果": Decimal("7999"),
    "ZTE中兴": Decimal("2999"),
}
_NO_ROLES = {
    "no_model": ("search_keyword", "result_region"),
    "capacity_disabled": ("capacity",),
    "color_disabled": ("color",),
}


@pytest.mark.parametrize(
    ("brand", "fixture_state", "expected_outcome"),
    [
        (brand, state, outcome)
        for brand in SUPPORTED_BRANDS
        for state, outcome in (
            ("normal", BusinessOutcome.PRICE_FOUND),
            ("no_model", BusinessOutcome.NO_MODEL),
            ("capacity_disabled", BusinessOutcome.CAPACITY_UNAVAILABLE),
            ("color_disabled", BusinessOutcome.COLOR_UNAVAILABLE),
        )
    ],
)
def test_official_brand_contract(
    brand: str,
    fixture_state: str,
    expected_outcome: BusinessOutcome,
    official_case: Any,
) -> None:
    adapter, task, page, capture = official_case(brand, fixture_state)

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is expected_outcome
    assert observation.url == page.url
    assert capture.calls == 0
    if fixture_state == "normal":
        assert observation.price == _NORMAL_PRICES[brand]
        assert observation.css_rectangles == ()
    else:
        assert observation.price is None
        assert tuple(
            rectangle.role for rectangle in observation.css_rectangles
        ) == _NO_ROLES[fixture_state]
        assert all(
            rectangle.width > 0 and rectangle.height > 0
            for rectangle in observation.css_rectangles
        )


def test_official_adapter_binds_catalog_and_both_runtime_protocols(
    official_case: Any,
) -> None:
    for brand in SUPPORTED_BRANDS:
        adapter, _task, _page, _capture = official_case(brand, "normal")
        assert isinstance(adapter, RegisteredSiteAdapter)
        assert isinstance(adapter, SiteObservationAdapter)
        assert adapter.channel is WebsiteChannel.OFFICIAL
        assert adapter.spec.brand == brand
        assert adapter.spec.channel is WebsiteChannel.OFFICIAL


@pytest.mark.parametrize(
    ("role", "error_type"),
    [
        ("login", LoginRequired),
        ("risk-control", SecurityVerificationRequired),
    ],
)
def test_official_intervention_uses_brand_scoped_session_identity(
    role: str,
    error_type: type[LoginRequired],
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            '<main data-screen="store">',
            (
                '<main data-screen="store">'
                f'<div data-official-role="{role}">需要人工处理</div>'
            ),
            1,
        ),
    )

    with pytest.raises(error_type) as caught:
        adapter.observe(task, cast(Any, page))

    assert caught.value.site == "official:小米"


def test_official_policies_and_capacity_strategies_are_exactly_approved() -> None:
    assert OFFICIAL_POLICIES == {
        "小米": PricePolicy.RED_SELLING,
        "HONOR": PricePolicy.LOWEST,
        "华为": PricePolicy.LOWEST,
        "维沃": PricePolicy.LOWEST,
        "欧珀": PricePolicy.LOWEST,
        "苹果": PricePolicy.HIGHEST,
        "ZTE中兴": PricePolicy.LOWEST,
    }
    assert OFFICIAL_CAPACITY_STRATEGIES == {
        "小米": "ram_plus_storage",
        "HONOR": "ram_plus_storage",
        "华为": "ram_plus_storage_or_storage",
        "维沃": "ram_plus_storage",
        "欧珀": "ram_plus_storage",
        "苹果": "storage_only",
        "ZTE中兴": "ram_plus_storage",
    }


def test_xiaomi_uses_only_red_verified_selling_context(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case("小米", "normal")

    observation = adapter.observe(task, cast(Any, page))

    assert observation.price == Decimal("4399")
    assert observation.semantic_state.current_sku == "1001"
    assert observation.semantic_state.region == "福建>福州>台江"
    assert observation.semantic_state.stock_state == "有货"


def test_apple_follows_model_purchase_color_storage_sequence(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case("苹果", "normal")

    observation = adapter.observe(task, cast(Any, page))

    assert observation.price == Decimal("7999")
    assert page.option_clicks == ["color", "capacity"]
    assert page.goto_calls == [
        adapter.spec.entry_url,
        "https://www.apple.com.cn/product/iphone-16",
    ]


@pytest.mark.parametrize(
    "brand",
    ["小米", "HONOR", "华为", "维沃", "欧珀", "ZTE中兴"],
)
def test_non_apple_official_sites_select_capacity_before_color(
    brand: str,
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(brand, "normal")

    adapter.observe(task, cast(Any, page))

    assert page.option_clicks == ["capacity", "color"]


def test_honor_official_resume_goes_directly_to_saved_detail(
    official_case: Any,
) -> None:
    adapter, task, original_page, _capture = official_case("HONOR", "normal")
    original = adapter.observe(task, cast(Any, original_page))
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=original.outcome,
        price=original.price,
        url=original.url,
        observed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    _same_adapter, _same_task, resumed_page, _capture = official_case(
        "HONOR",
        "normal",
    )

    resumed = adapter.resume(task, cast(Any, resumed_page), checkpoint)

    assert resumed == original
    assert resumed_page.goto_calls == [checkpoint.url]


def test_official_scrolls_each_exact_option_into_view_before_clicking(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case("小米", "normal")

    adapter.observe(task, cast(Any, page))

    assert page.option_scrolls == ["capacity", "color"]
    assert page.option_clicks == ["capacity", "color"]


def test_huawei_uses_exact_storage_only_fallback_after_combined_label_is_absent(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case("华为", "normal")

    adapter.observe(task, cast(Any, page))

    assert page.option_labels[0] == "256GB"


def test_direct_execute_never_fabricates_formal_evidence(
    official_case: Any,
) -> None:
    adapter, task, page, capture = official_case("小米", "normal")

    with pytest.raises(NonRetryableTechnicalError) as error:
        adapter.execute(task, cast(Any, page), cast(Any, capture))

    assert error.value.code == "ADAPTER_DIRECT_EXECUTION_UNSUPPORTED"
    assert capture.calls == 0


def test_default_registry_resolves_all_seven_official_specs() -> None:
    official_specs = tuple(
        spec
        for spec in load_site_catalog()
        if spec.channel is WebsiteChannel.OFFICIAL
    )

    resolved = tuple(
        adapter_for(spec.brand, WebsiteChannel.OFFICIAL)
        for spec in official_specs
    )

    assert len(resolved) == 7
    assert all(isinstance(adapter, RegisteredSiteAdapter) for adapter in resolved)
    assert all(
        isinstance(adapter, OfficialSiteAdapter)
        for adapter in resolved
        if adapter.spec.brand in {"HONOR", "ZTE中兴"}
    )
    assert tuple(adapter.spec for adapter in resolved) == official_specs


def test_wrong_store_identity_is_technical_not_business_no(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "no_model",
        mutate=lambda html: html.replace(
            '<span class="official-xiaomi-store">小米商城</span>',
            '<span class="official-xiaomi-store">未批准商城</span>',
            1,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="store"):
        adapter.observe(task, cast(Any, page))


def test_missing_cards_and_explicit_empty_state_is_layout_drift(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "no_model",
        mutate=lambda html: html.replace(
            '<div class="official-xiaomi-empty">未找到商品</div>',
            "",
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="empty state"):
        adapter.observe(task, cast(Any, page))


def test_duplicate_exact_model_cards_fail_closed(
    official_case: Any,
) -> None:
    duplicate = (
        '<article class="official-xiaomi-card">'
        '<a class="official-xiaomi-product-link" '
        'href="https://www.mi.com/product/xiaomi-15">'
        '<span class="official-xiaomi-product-title">小米 15</span>'
        "</a></article>"
    )
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            "</section>\n    </main>",
            f"{duplicate}</section>\n    </main>",
            1,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="ambiguous"):
        adapter.observe(task, cast(Any, page))


def test_unapproved_product_host_fails_closed(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            "https://www.mi.com/product/xiaomi-15",
            "https://evil.example/product/xiaomi-15",
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="URL"):
        adapter.observe(task, cast(Any, page))


@pytest.mark.parametrize(
    ("brand", "dependent_group", "first_option", "expected_price"),
    [
        ("小米", "colors", "capacity", Decimal("4399")),
        ("苹果", "capacities", "color", Decimal("7999")),
    ],
)
def test_official_resolves_each_dependent_option_only_after_prior_selection(
    brand: str,
    dependent_group: str,
    first_option: str,
    expected_price: Decimal,
    official_case: Any,
) -> None:
    slug = "xiaomi" if brand == "小米" else "apple"
    marker = f'<div class="official-{slug}-{dependent_group}">'
    replacement = (
        f'<div class="official-{slug}-{dependent_group}" '
        f'data-reveal-after="{first_option}" hidden>'
    )
    adapter, task, page, _capture = official_case(
        brand,
        "normal",
        mutate=lambda html: html.replace(marker, replacement, 1),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == expected_price


def test_official_current_sku_sold_out_returns_exact_legal_evidence(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            ">有货</span>",
            ">已售罄</span>",
            1,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.SOLD_OUT
    assert observation.price is None
    assert len(observation.css_rectangles) == 1
    assert observation.css_rectangles[0].role == "stock_status"
    assert observation.css_rectangles[0].width == 120
    assert observation.css_rectangles[0].height == 36


def test_official_sold_out_revalidates_exact_selection_immediately_before_return(
    official_case: Any,
) -> None:
    def mutate(html: str) -> str:
        return html.replace(
            'class="official-xiaomi-sku" data-current-sku="1001"',
            (
                'class="official-xiaomi-sku" data-current-sku="1001" '
                'data-switch-current-sku-after-stock-reads="1" '
                'data-switch-current-sku-to="stale-sku"'
            ),
            1,
        ).replace(">有货</span>", ">已售罄</span>", 1)

    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=mutate,
    )

    with pytest.raises(LayoutRecognitionError, match="SKU|bind|selected"):
        adapter.observe(task, cast(Any, page))


@pytest.mark.parametrize("sku", ["", "stale-sku"])
def test_official_stock_requires_one_exact_current_sku_binding(
    sku: str,
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            'class="official-xiaomi-stock" data-sku="1001"',
            f'class="official-xiaomi-stock" data-sku="{sku}"',
            1,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="stock|SKU"):
        adapter.observe(task, cast(Any, page))


@pytest.mark.parametrize(
    ("brand", "fixture_state", "binding"),
    [
        (
            "小米",
            "capacity_disabled",
            'data-option-kind="capacity" data-sku="1001"',
        ),
        (
            "小米",
            "color_disabled",
            'data-option-kind="capacity" data-sku="1001"',
        ),
        (
            "小米",
            "color_disabled",
            'data-option-kind="color" data-sku="1001"',
        ),
        (
            "苹果",
            "capacity_disabled",
            'data-option-kind="color" data-sku="1006"',
        ),
    ],
)
def test_official_disabled_target_requires_complete_current_sku_binding(
    brand: str,
    fixture_state: str,
    binding: str,
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        brand,
        fixture_state,
        mutate=lambda html: html.replace(
            binding,
            binding.replace("100", "stale-"),
            1,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="SKU|bind|selected"):
        adapter.observe(task, cast(Any, page))


def test_official_contradictory_stock_markers_fail_closed(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            ">有货</span>",
            ">有货 已售罄</span>",
            1,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="stock"):
        adapter.observe(task, cast(Any, page))


def test_official_exact_not_purchasable_stock_is_legal_sold_out(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            ">有货</span>",
            ">不可购买</span>",
            1,
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.SOLD_OUT
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        "stock_status",
    )


@pytest.mark.parametrize("switch_after", [4, 8])
def test_official_price_poll_revalidates_selected_sku_each_round_and_before_return(
    switch_after: int,
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            'class="official-xiaomi-sku" data-current-sku="1001"',
            (
                'class="official-xiaomi-sku" data-current-sku="1001" '
                f'data-switch-current-sku-after-price-evaluations="{switch_after}" '
                'data-switch-current-sku-to="stale-sku"'
            ),
            1,
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="SKU|bind|selected"):
        adapter.observe(task, cast(Any, page))


@pytest.mark.parametrize(
    ("option_kind", "exact_label", "other_label", "group_style"),
    [
        (
            "capacity",
            "12GB + 256GB",
            "8GB + 128GB",
            "left:25px;top:160px;width:150px;height:36px",
        ),
        (
            "color",
            "黑色",
            "白色",
            "left:190px;top:210px;width:120px;height:36px",
        ),
    ],
)
def test_official_unique_bound_complete_option_group_proves_target_unavailable(
    option_kind: str,
    exact_label: str,
    other_label: str,
    group_style: str,
    official_case: Any,
) -> None:
    group_name = "capacities" if option_kind == "capacity" else "colors"

    def mutate(html: str) -> str:
        group = f'<div class="official-xiaomi-{group_name}">'
        bound_group = (
            f'<div class="official-xiaomi-{group_name}" '
            'data-context-sku="1001" data-options-complete="true" '
            f'style="{group_style}">'
        )
        return html.replace(group, bound_group, 1).replace(
            f">{exact_label}</button>",
            f">{other_label}</button>",
            1,
        )

    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=mutate,
    )

    observation = adapter.observe(task, cast(Any, page))

    expected = (
        BusinessOutcome.CAPACITY_UNAVAILABLE
        if option_kind == "capacity"
        else BusinessOutcome.COLOR_UNAVAILABLE
    )
    assert observation.outcome is expected
    assert tuple(rect.role for rect in observation.css_rectangles) == (
        option_kind,
    )


@pytest.mark.parametrize(
    ("brand", "missing_kind", "exact_label", "other_label", "stale_binding"),
    [
        (
            "小米",
            "color",
            "黑色",
            "白色",
            'data-option-kind="capacity" data-sku="1001"',
        ),
        (
            "苹果",
            "capacity",
            "256GB",
            "128GB",
            'data-option-kind="color" data-sku="1006"',
        ),
    ],
)
def test_official_absent_target_rejects_stale_prior_selection_binding(
    brand: str,
    missing_kind: str,
    exact_label: str,
    other_label: str,
    stale_binding: str,
    official_case: Any,
) -> None:
    slug = "xiaomi" if brand == "小米" else "apple"
    group_name = "colors" if missing_kind == "color" else "capacities"

    def mutate(html: str) -> str:
        group = f'<div class="official-{slug}-{group_name}">'
        bound_group = (
            f'<div class="official-{slug}-{group_name}" '
            f'data-context-sku="{"1001" if brand == "小米" else "1006"}" '
            'data-options-complete="true">'
        )
        return (
            html.replace(group, bound_group, 1)
            .replace(f">{exact_label}</button>", f">{other_label}</button>", 1)
            .replace(stale_binding, stale_binding.replace("100", "stale-"), 1)
        )

    adapter, task, page, _capture = official_case(
        brand,
        "normal",
        mutate=mutate,
    )

    with pytest.raises(LayoutRecognitionError, match="bind|SKU|selected"):
        adapter.observe(task, cast(Any, page))


def test_official_same_host_non_product_path_and_query_fails_closed(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            "https://www.mi.com/product/xiaomi-15",
            "https://www.mi.com/account/login?next=/product/xiaomi-15",
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="URL"):
        adapter.observe(task, cast(Any, page))


def test_official_product_path_must_bind_to_requested_model_identity(
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            "/product/xiaomi-15",
            "/product/redmi-note-14",
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="URL"):
        adapter.observe(task, cast(Any, page))


@pytest.mark.parametrize(
    ("fixture_state", "style_fragment"),
    [
        ("no_model", "width:780px"),
        ("capacity_disabled", "width:150px"),
        ("color_disabled", "width:120px"),
    ],
)
def test_invalid_rectangle_prevents_legal_no_observation(
    fixture_state: str,
    style_fragment: str,
    official_case: Any,
) -> None:
    adapter, task, page, _capture = official_case(
        "小米",
        fixture_state,
        mutate=lambda html: html.replace(style_fragment, "width:0px", 1),
    )

    with pytest.raises(LayoutRecognitionError, match="rectangle"):
        adapter.observe(task, cast(Any, page))


def test_official_adapter_rejects_wrong_spec_and_task_binding(
    official_case: Any,
) -> None:
    jd_spec = next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.JD
    )
    with pytest.raises(ValueError, match="OFFICIAL"):
        OfficialSiteAdapter(jd_spec)

    adapter, task, page, _capture = official_case("小米", "normal")
    with pytest.raises(ValueError, match="channel"):
        adapter.observe(
            replace(task, channel=WebsiteChannel.JD),
            cast(Any, page),
        )
    with pytest.raises(ValueError, match="brand"):
        adapter.observe(
            replace(task, brand="HONOR"),
            cast(Any, page),
        )

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.evidence.models import EvidenceRecord, EvidenceState
from quote_app.sites.prices import (
    PriceCandidate,
    PricePolicy,
    SellingPriceEvidence,
    choose_price,
    filter_valid_prices,
    parse_price,
)
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteResult,
)


def _candidate(
    text: str = "¥4299",
    context: str = "销售价",
    *,
    visible: bool = True,
    color: str = "rgb(0, 0, 0)",
    evidence: SellingPriceEvidence = SellingPriceEvidence.UNVERIFIED,
    effective_line_through: bool = False,
) -> PriceCandidate:
    return PriceCandidate(
        text=text,
        context=context,
        visible=visible,
        computed_color=color,
        selling_evidence=evidence,
        effective_line_through=effective_line_through,
    )


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("512", "容量 512GB"),
        ("500", "HONOR 500 型号"),
    ],
)
def test_unverified_bare_capacity_and_model_numbers_are_rejected(
    text: str,
    context: str,
) -> None:
    assert filter_valid_prices([_candidate(text, context)]) == ()


def test_verified_current_sku_selling_node_accepts_a_bare_price() -> None:
    candidate = _candidate(
        "4299",
        "当前已选 SKU",
        evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
    )

    assert filter_valid_prices([candidate]) == (candidate,)


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("¥500", "最高可省"),
        ("¥300", "直降优惠"),
        ("¥3999", "换购价"),
        ("¥3999", "低至"),
        ("¥5999", "价格区间"),
    ],
)
def test_unverified_promotion_and_range_amounts_are_rejected(
    text: str,
    context: str,
) -> None:
    assert filter_valid_prices([_candidate(text, context)]) == ()


def test_highest_rejects_effective_line_through_original_price() -> None:
    candidates = [
        _candidate(
            "¥4999",
            "当前 SKU 售价",
            evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
            effective_line_through=True,
        ),
        _candidate(
            "¥4299",
            "当前 SKU 售价",
            evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
        ),
    ]

    assert choose_price(candidates, PricePolicy.HIGHEST) == Decimal("4299")


def test_effective_line_through_fact_is_load_bearing() -> None:
    clear = _candidate(
        "¥4299",
        "当前 SKU 售价",
        evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
    )
    struck = replace(clear, effective_line_through=True)

    assert filter_valid_prices([clear]) == (clear,)
    assert filter_valid_prices([struck]) == ()


def test_explicitly_verified_current_sku_struck_through_price_is_accepted() -> None:
    candidate = _candidate(
        "¥5499",
        "京东当前已选配置",
        evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_STRUCK_THROUGH_PRICE,
        effective_line_through=True,
    )

    assert filter_valid_prices([candidate]) == (candidate,)


@pytest.mark.parametrize(
    "context",
    ["销售价", "售价", "到手价", "现价", "活动价", "会员价"],
)
def test_approved_strong_selling_labels_are_accepted(context: str) -> None:
    candidate = _candidate(
        "¥4299",
        context,
        evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
    )

    assert filter_valid_prices([candidate]) == (candidate,)


@pytest.mark.parametrize(
    "context",
    [
        "价格",
        "商品价格",
        "人民币",
        "CNY",
        "RMB",
        '[class*="price"]',
    ],
)
def test_weak_or_generic_labels_cannot_claim_strong_evidence(
    context: str,
) -> None:
    candidate = _candidate(
        "¥4299",
        context,
        evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
    )

    assert filter_valid_prices([candidate]) == ()


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("售价 ¥500", "最高可省"),
        ("售价 ¥300", "直降优惠"),
        ("售价 ¥3999", "换购价"),
        ("售价 ¥3999", "低至"),
        ("售价 ¥5999", "价格区间"),
        ("¥5999", "建议售价"),
    ],
)
def test_strong_evidence_requires_an_independent_exact_transaction_label(
    text: str,
    context: str,
) -> None:
    candidate = _candidate(
        text,
        context,
        evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
    )

    assert filter_valid_prices([candidate]) == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¥4,299.50", Decimal("4299.50")),
        ("￥ 4,299", Decimal("4299")),
        ("4299.9元", Decimal("4299.9")),
        ("销售价 ¥4299.01", Decimal("4299.01")),
        ("到手价￥0.01", Decimal("0.01")),
        ("售价\u00a0￥４２９９．５０", Decimal("4299.50")),
        (
            "¥999999999999999999999999.99",
            Decimal("999999999999999999999999.99"),
        ),
    ],
)
def test_parse_price_supports_currency_grouping_and_exact_cents(
    text: str,
    expected: Decimal,
) -> None:
    result = parse_price(text)

    assert result == expected
    assert isinstance(result, Decimal)


@pytest.mark.parametrize(
    "text",
    [
        None,
        4299,
        "",
        "  ",
        "NaN",
        "Inf",
        "Infinity",
        "-1",
        "¥-1",
        "￥ -0.01",
        "−1",
        "￥−0",
        "0",
        "¥0.00",
        "+4299",
        "¥+4299",
        "¥4299-¥4499",
        "¥4299~4499",
        "¥4299～￥4499",
        "¥4299至4499",
        "¥4299 / ¥4499",
        "¥4299 ¥4499",
        "4299起",
        "起价 ¥4299",
        "FROM ¥4299",
        "¥4,29",
        "¥1,23,456",
        "4299.",
        ".99",
        "4,299.0.0",
        "4299,",
        ",4299",
        "¥4299.999",
        "24",
        "12期 ¥399",
        "iPhone 16 ¥4299",
        "iPhone16 ¥4299",
        "HONOR500 ￥4299",
        "¥٤٢٩٩",
        "¥४२९९",
        "¥42٩9",
        "￥４２9٩",
        "¥:4299",
        "¥¥4299",
        "￥ ￥4299",
        "4299元元",
        "元4299",
        "￥abc 4299",
        "$4299",
        "US$4299",
        "€4299",
        "£4299",
        "HK$4299",
        "USD 4299",
        "HKD 4299",
        "EUR 4299",
        "GBP 4299",
        "¥4299以上",
        "¥4299以下",
        "¥4299以内",
        "不超过 ¥4299",
        "不高于 ¥4299",
        "不低于 ¥4299",
        "高于 ¥4299",
        "低于 ¥4299",
        "最多 ¥4299",
        "最少 ¥4299",
        "指导价 ¥4299",
        "1e3",
        "¥4.299e3",
        "12GB+256GB",
    ],
)
def test_parse_price_rejects_invalid_nonpositive_and_ambiguous_values(
    text: object,
) -> None:
    assert parse_price(text) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", ""),
        ("text", None),
        ("context", ""),
        ("context", 1),
        ("visible", 1),
        ("visible", "yes"),
        ("computed_color", ""),
        ("computed_color", None),
        ("selling_evidence", "verified"),
        ("effective_line_through", 1),
    ],
)
def test_price_candidate_rejects_invalid_fields(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "text": "¥4299",
        "context": "销售价",
        "visible": True,
        "computed_color": "rgb(0, 0, 0)",
        "selling_evidence": SellingPriceEvidence.STRONG_SELLING_LABEL,
        "effective_line_through": False,
    }
    values[field] = value

    with pytest.raises(ValueError):
        PriceCandidate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("销售价 ¥4299", Decimal("4299")),
        ("销售价：￥ 4,299.50", Decimal("4299.50")),
        ("到手价 4299元", Decimal("4299")),
        ("价格 ¥4299", Decimal("4299")),
        ("现价 ￥4299", Decimal("4299")),
        ("活动价 4299元", Decimal("4299")),
        ("会员价 ¥4299", Decimal("4299")),
        ("商品价格：￥4299", Decimal("4299")),
        ("¥4299 销售价", Decimal("4299")),
    ],
)
def test_parse_price_allows_ordinary_labels_outside_one_complete_amount(
    text: str,
    expected: Decimal,
) -> None:
    assert parse_price(text) == expected


def test_price_candidate_is_immutable_and_normalizes_outer_whitespace() -> None:
    candidate = PriceCandidate(
        text="  ¥4299  ",
        context="  销售价  ",
        visible=True,
        computed_color="  rgb(255, 0, 0)  ",
        selling_evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
        effective_line_through=False,
    )

    assert candidate.text == "¥4299"
    assert candidate.context == "销售价"
    assert candidate.computed_color == "rgb(255, 0, 0)"
    with pytest.raises(FrozenInstanceError):
        candidate.text = "¥1"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("24期 ¥187.46", "销售价"),
        ("月供 ¥187.46", "销售价"),
        ("月付 ¥187.46", "销售价"),
        ("每期 ¥187.46", "销售价"),
        ("期付 ¥187.46", "销售价"),
        ("¥187.46/月", "销售价"),
        ("¥187.46／月", "销售价"),
        ("187.46元/月", "销售价"),
        ("¥187.46/期", "销售价"),
        ("每个月 ¥187.46", "销售价"),
        ("每一期 ¥187.46", "销售价"),
        ("¥187.46/个月", "销售价"),
        ("¥187.46／个月", "销售价"),
        ("¥187.46/一期", "销售价"),
        ("首付 ¥999", "分期方案"),
        ("优惠券后 ¥4299", "销售价"),
        ("券后价 ¥4299", "销售价"),
        ("补贴后 ¥4299", "销售价"),
        ("国补价 ¥4299", "销售价"),
        ("以旧换新 ¥4299", "销售价"),
        ("定金 ¥100", "预售"),
        ("划线价 ¥4999", "商品价格"),
        ("原价 ¥4999", "商品价格"),
        ("¥4299", "24期 每月187.46元"),
        ("¥4299", "可领优惠券"),
        ("¥4299", "政府补贴"),
        ("¥4299", "以旧换新价"),
        ("¥4299", "定金预售"),
        ("¥4299", "划线价/原价"),
        ("分 期 ¥187.46", "销售价"),
        ("以旧／换新 ¥4299", "销售价"),
        ("定-金 ¥100", "预售"),
        ("¥4299", "优\u00a0惠\u00a0券"),
        ("¥4299", "补\n贴"),
        ("领券价 ¥4299", "销售价"),
        ("用 券 价 ¥4299", "销售价"),
        ("店铺／券 ¥4299", "销售价"),
        ("订-金 ¥4299", "预售"),
        ("尾 款 ¥4299", "预售"),
        ("首／款 ¥4299", "预售"),
        ("预 付 款 ¥4299", "预售"),
        ("满-减后 ¥4299", "销售价"),
        ("返\n现后 ¥4299", "销售价"),
        ("¥4299", "领 券 价"),
        ("¥4299", "店铺／券"),
        ("¥4299", "订-金"),
        ("¥4299", "尾 款"),
        ("¥4299", "首／款"),
        ("¥4299", "预 付 款"),
        ("¥4299", "满-减优惠"),
        ("¥4299", "返\n现活动"),
        ("约 ¥4299", "销售价"),
        ("大约 ￥4299", "销售价"),
        ("约合 4299元", "销售价"),
        ("约为 ¥4299", "销售价"),
        ("大约为 ￥4299", "销售价"),
        ("约人民币 4299元", "销售价"),
        ("约合人民币 4299元", "销售价"),
        ("约等于 ¥4299", "销售价"),
        ("约等同于 ￥4299", "销售价"),
        ("大概 ¥4299", "销售价"),
        ("将近 ￥4299", "销售价"),
        ("接近 4299元", "销售价"),
        ("¥4299 左右", "销售价"),
        ("¥4299上下", "销售价"),
        ("参考价 ¥4299", "商品价格"),
        ("预估价 ¥4299", "商品价格"),
        ("建议零售价 ¥4299", "商品价格"),
        ("¥4299", "约 ￥4299"),
        ("¥4299", "每 期"),
        ("¥4299", "月-付"),
        ("¥4299", "399元／月"),
        ("¥4299", "价格仅供参考"),
        ("¥4299", "预估价"),
        ("¥4299", "建议零售价"),
    ],
)
def test_filter_checks_text_and_context_for_non_transaction_prices(
    text: str,
    context: str,
) -> None:
    assert filter_valid_prices(
        [
            _candidate(
                text,
                context,
                evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
            )
        ]
    ) == ()


def test_filter_keeps_visible_parseable_transaction_prices_only() -> None:
    evidence = SellingPriceEvidence.STRONG_SELLING_LABEL
    valid = _candidate("¥4,299.50", "销售价", evidence=evidence)
    candidates = [
        valid,
        _candidate("¥4,399", "到手价", visible=False, evidence=evidence),
        _candidate("价格待定", "销售价", evidence=evidence),
        _candidate("¥4,499", "原价", evidence=evidence),
    ]

    assert filter_valid_prices(candidates) == (valid,)


@pytest.mark.parametrize(
    "context",
    [
        "美元 $4299",
        "US$ 4299",
        "欧元 €4299",
        "英镑 £4299",
        "港币 HK$4299",
        "USD",
        "HKD",
        "EUR",
        "GBP",
        "JPY",
        "KRW",
        "AUD",
        "CAD",
        "SGD",
        "TWD",
        "MOP",
        "CHF",
        "NZD",
        "THB",
        "MYR",
        "PHP",
        "INR",
        "RUB",
        "日元",
        "韩元",
        "澳元",
        "加元",
        "新加坡元",
        "台币",
        "澳门元",
        "美金",
        "港元",
        "日币",
        "韩币",
        "澳币",
        "加币",
        "新币",
        "新元",
        "澳门币",
        "价格以上",
        "价格以下",
        "价格以内",
        "不超过",
        "不高于",
        "不低于",
        "高于",
        "低于",
        "最多",
        "最少",
        "上限",
        "下限",
        "至少",
        "至多",
        "不少于",
        "不多于",
        "大于",
        "小于",
        "指导价",
        "约",
        "约为",
        "约等于",
        "约等同于",
        "约合",
        "大约",
        "大致",
        "大概",
        "将近",
        "接近",
        "左右",
        "上下",
        "前后",
        "每一个月",
        "每一月",
        "按月支付",
        "按月付款",
        "一个月 ¥399",
        "3个月",
        "１２个月",
    ],
)
def test_split_dom_context_rejects_high_risk_price_semantics(context: str) -> None:
    assert filter_valid_prices(
        [
            _candidate(
                "¥4299",
                context,
                evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
            )
        ]
    ) == ()


def test_filter_preserves_input_order_without_modifying_the_input() -> None:
    evidence = SellingPriceEvidence.STRONG_SELLING_LABEL
    first = _candidate("¥4299", "销售价", evidence=evidence)
    second = _candidate("¥4399", "到手价", evidence=evidence)
    hidden = _candidate(
        "¥4499",
        "销售价",
        visible=False,
        evidence=evidence,
    )
    candidates = [first, hidden, second]
    original = list(candidates)

    result = filter_valid_prices(candidates)

    assert result == (first, second)
    assert candidates == original


@pytest.mark.parametrize(
    "context",
    [
        "销售价",
        "到手价",
        "会员价",
        "活动价",
        "本月活动价",
        "预约活动价",
        "预约销售价",
        "特约会员价",
        "合约到手价",
        "官方商品说明",
        "人民币售价",
        "CNY",
        "RMB",
        "左右滑动查看更多商品",
        "上下滑动选择颜色",
        "接近传感器说明",
        "最多支持双卡",
        "型号 ABC-123",
        "AUDI 商品说明",
        "CADENCE 商品说明",
        "上下滑动查看 HONOR 500",
        "左右滑动查看商品500",
        "以上内容适用于 HONOR 500",
        "以下是 HONOR 500 参数",
        "接近传感器 型号500",
        "最多支持 512 GB",
        "以上是用户评价",
        "接近传感器元器件说明",
        "型号 ٥٠٠",
        "传感器编号 ५००",
        "屏幕≥6.7英寸",
        "存储≥512GB",
        "最高支持512GB",
        "美金色外观",
        "美金色版本",
        "全新元件",
        "全新元素",
        "创新元素",
        "左右滑动查看价格说明",
        "接近传感器与元件说明",
        "上下滑动查看元器件",
        "性价比高左右滑动",
        "支持3个月保修",
        "元器件一个月保修",
        "支持荣耀500以上机型",
        "适用于型号500以上",
        "荣耀500以上型号",
        "HONOR500以上机型",
        "以上500型号",
        "以上型号500",
        "分辨率≥1080P",
        "相机≥200MP",
        "屏幕≥1440PX",
    ],
)
def test_verified_selling_node_allows_ordinary_nonprice_context(
    context: str,
) -> None:
    candidate = _candidate(
        "¥4299",
        context,
        evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
    )

    assert filter_valid_prices([candidate]) == (candidate,)


@pytest.mark.parametrize(
    "context",
    [
        "价格上限",
        "售价下限",
        "金额至少",
        "价格至多",
        "价格不少于",
        "售价不多于",
        "价格大于",
        "金额小于",
        "售价约为",
        "金额大致",
        "价格前后",
        "CHF 4299",
        "商品售价为新西兰元",
        "支持 CHF 结算",
        "支持美金结算",
        "不超过4299",
        "4299以上",
        "约人民币",
        "约合人民币",
        "约CNY",
        "大约RMB",
        "会员价上限",
        "4299元左右",
        "不\u200b超过4299",
        "U\u200bSD",
        "美\u200b金",
        "4299\u200b以上",
        "每\u200b一个月",
        "不\u2060超过4299",
        "U\u2060SD",
        "美\u2060金",
        "4299\u2060以上",
        "每\u2060一个月",
        "不+超过4299",
        "4299−以上",
        "U−SD",
        "美+金",
        "不超过٤٢٩٩",
        "४२९९以上",
        "不\u034f超过4299",
        "U\ufe0fSD",
        "美😀金",
        "美★金",
        "UˆSD",
        "UЅD",
        "价格≥",
        "≥4299",
        "4299≤",
        "≈4299",
        ">",
        "最高价",
        "最低价",
        "美金版本",
        "新元计价",
        "价格约为",
        "价格说明支持荣耀500以上机型",
        "价格说明以上500型号",
        "价格说明以上型号500",
        "≥4299PRICE",
    ],
)
def test_context_rejects_risk_terms_with_price_anchor_or_amount(
    context: str,
) -> None:
    assert filter_valid_prices(
        [
            _candidate(
                "¥4299",
                context,
                evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
            )
        ]
    ) == ()


@pytest.mark.parametrize(
    "context",
    ["元一个月", "元/一个月", "元3个月", "399元一个月"],
)
def test_periodic_payment_rejects_reverse_yuan_structures(context: str) -> None:
    assert choose_price(
        [
            _candidate(
                "399",
                context,
                evidence=SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE,
            )
        ],
        PricePolicy.LOWEST,
    ) is None


def test_highest_and_lowest_are_decimal_and_input_order_independent() -> None:
    evidence = SellingPriceEvidence.STRONG_SELLING_LABEL
    candidates = [
        _candidate("¥4,299.50", "销售价", evidence=evidence),
        _candidate("¥4,499.01", "到手价", evidence=evidence),
        _candidate("¥9,999", "销售价", visible=False, evidence=evidence),
        _candidate("¥187.46", "24期 月供", evidence=evidence),
    ]

    assert choose_price(candidates, PricePolicy.HIGHEST) == Decimal("4499.01")
    assert choose_price(candidates, PricePolicy.LOWEST) == Decimal("4299.50")
    assert choose_price(list(reversed(candidates)), PricePolicy.HIGHEST) == Decimal(
        "4499.01"
    )
    assert isinstance(choose_price(candidates, PricePolicy.LOWEST), Decimal)


def test_equal_price_ties_are_deterministic() -> None:
    evidence = SellingPriceEvidence.STRONG_SELLING_LABEL
    candidates = [
        _candidate("¥4299.50", "销售价", evidence=evidence),
        _candidate("￥4,299.50", "到手价", evidence=evidence),
    ]

    assert choose_price(candidates, PricePolicy.HIGHEST) == Decimal("4299.50")
    assert choose_price(candidates, PricePolicy.LOWEST) == Decimal("4299.50")
    assert choose_price(list(reversed(candidates)), PricePolicy.HIGHEST) == Decimal(
        "4299.50"
    )


def test_choose_price_returns_none_when_no_valid_candidate_exists() -> None:
    assert choose_price([], PricePolicy.HIGHEST) is None
    assert choose_price(
        [
            _candidate(
                "¥4299",
                "原价",
                evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
            )
        ],
        PricePolicy.LOWEST,
    ) is None


def test_choose_price_rejects_unknown_policy_and_invalid_candidate_items() -> None:
    with pytest.raises(ValueError, match="policy"):
        choose_price([_candidate()], "highest")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="PriceCandidate"):
        choose_price([object()], PricePolicy.HIGHEST)  # type: ignore[list-item]


@pytest.mark.parametrize(
    "color",
    [
        "rgb(255, 0, 0)",
        "RGB(255,0,0)",
        "rgba(255, 0, 0, 1)",
        "rgba(255,51,0,1.0)",
        "#ff0000",
        "#FF3300",
        "#f00",
        "#f30",
    ],
)
def test_red_selling_accepts_only_approved_opaque_red_css(color: str) -> None:
    result = choose_price(
        [
            _candidate(
                "¥4,299.50",
                "销售价",
                color=color,
                evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
            )
        ],
        PricePolicy.RED_SELLING,
    )

    assert result == Decimal("4299.50")


@pytest.mark.parametrize(
    "color",
    [
        "red",
        "hsl(0 100% 50%)",
        "rgb(254, 0, 0)",
        "rgb(300, 0, 0)",
        "rgba(255, 0, 0, 0)",
        "rgba(255, 0, 0, 0.9)",
        "#fe0000",
        "#ff000080",
        "var(--selling-red)",
        "transparent",
        "linear-gradient(#ff0000, #000000)",
        "rgb(255, 0, 0) !important",
        "rgba(255, 0, 0, 01)",
        "rgb(٢٥٥,٠,٠)",
        "rgb(25٥,0,0)",
    ],
)
def test_red_selling_rejects_unknown_or_nonopaque_css(color: str) -> None:
    assert (
        choose_price(
            [
                _candidate(
                    "¥4299",
                    "销售价",
                    color=color,
                    evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
                )
            ],
            PricePolicy.RED_SELLING,
        )
        is None
    )


def test_red_selling_uses_highest_valid_red_transaction_price() -> None:
    evidence = SellingPriceEvidence.STRONG_SELLING_LABEL
    candidates = [
        _candidate("¥4299", "销售价", color="#f00", evidence=evidence),
        _candidate(
            "¥4499.25",
            "到手价",
            color="rgb(255, 51, 0)",
            evidence=evidence,
        ),
        _candidate("¥4999", "原价", color="#ff0000", evidence=evidence),
        _candidate(
            "¥4599",
            "销售价",
            color="rgb(0, 0, 0)",
            evidence=evidence,
        ),
    ]

    assert choose_price(candidates, PricePolicy.RED_SELLING) == Decimal("4499.25")


def test_red_selling_nfkc_normalizes_fullwidth_ascii_css() -> None:
    assert choose_price(
        [
            _candidate(
                "¥4299",
                "销售价",
                color="ｒｇｂ（２５５，０，０）",
                evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
            )
        ],
        PricePolicy.RED_SELLING,
    ) == Decimal("4299")


def test_chosen_decimal_price_is_compatible_with_website_result(
    tmp_path: Path,
) -> None:
    chosen = choose_price(
        [
            _candidate(
                "¥4,299.50",
                "销售价",
                evidence=SellingPriceEvidence.STRONG_SELLING_LABEL,
            )
        ],
        PricePolicy.HIGHEST,
    )
    evidence = EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=tmp_path / "price.png",
        sha256="a" * 64,
        pixel_width=1920,
        pixel_height=1080,
        captured_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        validation_code="CAPTURE_OK",
        annotations=(),
    )

    result = WebsiteResult(
        task_id="task-price",
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=chosen,
        url="https://example.test/product",
        evidence=evidence,
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )

    assert result.price == Decimal("4299.50")

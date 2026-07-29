from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

_AMOUNT = re.compile(
    r"(?<![A-Z0-9.,])"
    r"(?P<currency>[¥￥])?\s*"
    r"(?P<number>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?)"
    r"(?P<yuan>\s*元)?"
    r"(?![A-Z0-9.,])",
)
_AMBIGUOUS_MARKERS = (
    "~",
    "-",
    "−",
    "–",
    "—",
    "+",
    "起",
    "至",
    "FROM",
)
_EXCLUDED_TERMS = (
    "分期",
    "月供",
    "月付",
    "每期",
    "每个月",
    "每一期",
    "期付",
    "每月",
    "首付",
    "优惠券",
    "券",
    "券后",
    "补贴",
    "国补",
    "以旧换新",
    "定金",
    "订金",
    "尾款",
    "首款",
    "预付款",
    "满减",
    "返现",
    "参考价",
    "仅供参考",
    "预估价",
    "建议零售价",
    "划线价",
    "原价",
    "INSTALLMENT",
    "COUPON",
    "TRADEIN",
    "DEPOSIT",
    "ORIGINALPRICE",
    "LISTPRICE",
)
_INSTALLMENT_COUNT = re.compile(r"\d+期")
_PERIODIC_SUFFIX = re.compile(r"(?:元)?/(?:个月|一期|月|期)")
_APPROXIMATE_PREFIX = re.compile(
    r"(?:大约等同于|大约等于|约等同于|约等于|大约|约合|大概|将近|接近|"
    r"(?<![预特合])约)(?:为)?(?:人民币)?(?=[¥￥0-9])"
)
_APPROXIMATE_SUFFIX = re.compile(
    r"(?:[¥￥]?[0-9][0-9,]*(?:\.[0-9]{1,2})?(?:元)?)(?:左右|上下)"
)
_FOREIGN_CURRENCY_CODES = frozenset(
    {
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
    }
)
_FOREIGN_CURRENCY_CODE = re.compile(
    rf"(?<![A-Z])(?:{'|'.join(sorted(_FOREIGN_CURRENCY_CODES))})(?![A-Z])"
)
_FOREIGN_CURRENCY_LONG_ALIASES = frozenset(
    {
        "英镑",
        "新加坡元",
        "澳门元",
        "瑞士法郎",
        "新西兰元",
        "泰铢",
        "马来西亚林吉特",
        "马来西亚令吉",
        "菲律宾比索",
        "印度卢比",
        "俄罗斯卢布",
    }
)
_FOREIGN_CURRENCY_SHORT_ALIASES = frozenset(
    {
        "美元",
        "美金",
        "欧元",
        "港币",
        "港元",
        "日元",
        "日币",
        "韩元",
        "韩币",
        "澳元",
        "澳币",
        "加元",
        "加币",
        "新币",
        "新元",
        "台币",
        "澳门币",
    }
)
_FOREIGN_CURRENCY_ALIASES = (
    _FOREIGN_CURRENCY_LONG_ALIASES | _FOREIGN_CURRENCY_SHORT_ALIASES
)
_FOREIGN_CURRENCY_CONTEXT_TERMS = (
    "结算",
    "版本",
    "计价",
    "换算",
    "支持",
    "兑换",
    "币种",
    "货币",
)
_CONTEXT_PERIOD_MONTHS = re.compile(
    r"(?:[0-9]+|[一二三四五六七八九十百两]+)个月"
)
_CONTEXT_RISK_TERMS = (
    "以上",
    "以下",
    "以内",
    "不超过",
    "不高于",
    "不低于",
    "高于",
    "低于",
    "最多",
    "最少",
    "最高",
    "最低",
    "上限",
    "下限",
    "至少",
    "至多",
    "不少于",
    "不多于",
    "大于",
    "小于",
    "指导价",
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
)
_CONTEXT_STANDALONE_RISK_LABELS = (
    frozenset(_CONTEXT_RISK_TERMS)
    | _FOREIGN_CURRENCY_CODES
    | _FOREIGN_CURRENCY_ALIASES
    | frozenset({"约", "约为"})
)
_CONTEXT_APPROXIMATE_YUE = re.compile(r"(?<![预特合])约(?:为)?")
_CONTEXT_RISK_PATTERN = (
    r"(?:"
    + "|".join(
        re.escape(term)
        for term in sorted(_CONTEXT_RISK_TERMS, key=len, reverse=True)
    )
    + r"|(?<![预特合])约(?:为)?)"
)
_CONTEXT_BARE_AMOUNT = (
    r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?"
)
_CONTEXT_RISK_THEN_AMOUNT = re.compile(
    rf"(?P<risk>{_CONTEXT_RISK_PATTERN})(?:为|人民币)?"
    rf"(?P<amount>{_CONTEXT_BARE_AMOUNT})(?![A-Z0-9.,])"
)
_CONTEXT_AMOUNT_THEN_RISK = re.compile(
    rf"(?<![0-9.,])(?P<amount>{_CONTEXT_BARE_AMOUNT})"
    rf"(?:人民币)?(?P<risk>{_CONTEXT_RISK_PATTERN})"
)
_CONTEXT_AMOUNT_WITH_YUAN = re.compile(
    rf"(?:{_CONTEXT_BARE_AMOUNT}元|元{_CONTEXT_BARE_AMOUNT})"
)
_CONTEXT_PRICE_ANCHORS = (
    "价格",
    "售价",
    "金额",
    "销售价",
    "到手价",
    "现价",
    "活动价",
    "会员价",
    "商品价格",
    "报价",
    "价款",
    "标价",
    "指导价",
    "参考价",
    "预估价",
    "建议零售价",
    "划线价",
    "原价",
    "最高价",
    "最低价",
)
_CONTEXT_EXPLICIT_NON_TRANSACTION_TERMS = ("指导价", "最高价", "最低价")
_CONTEXT_PRICE_ANCHOR_PATTERN = (
    r"(?:"
    + "|".join(
        re.escape(anchor)
        for anchor in sorted(_CONTEXT_PRICE_ANCHORS, key=len, reverse=True)
    )
    + r")"
)
_CONTEXT_RISK_PRICE_ADJACENT = re.compile(
    rf"(?:{_CONTEXT_RISK_PATTERN}{_CONTEXT_PRICE_ANCHOR_PATTERN}"
    rf"|{_CONTEXT_PRICE_ANCHOR_PATTERN}{_CONTEXT_RISK_PATTERN})"
)
_CONTEXT_DIRECT_CURRENCY_PATTERN = r"(?:人民币|CNY|RMB|[¥￥])"
_CONTEXT_RISK_CURRENCY_ADJACENT = re.compile(
    rf"(?:{_CONTEXT_RISK_PATTERN}{_CONTEXT_DIRECT_CURRENCY_PATTERN}"
    rf"|{_CONTEXT_DIRECT_CURRENCY_PATTERN}{_CONTEXT_RISK_PATTERN})"
)
_CONTEXT_AMOUNT_WITH_YUAN_PATTERN = (
    rf"(?:{_CONTEXT_BARE_AMOUNT}元|元{_CONTEXT_BARE_AMOUNT})"
)
_CONTEXT_RISK_YUAN_AMOUNT_ADJACENT = re.compile(
    rf"(?:{_CONTEXT_RISK_PATTERN}{_CONTEXT_AMOUNT_WITH_YUAN_PATTERN}"
    rf"|{_CONTEXT_AMOUNT_WITH_YUAN_PATTERN}{_CONTEXT_RISK_PATTERN})"
)
_CONTEXT_PERIOD_PAYMENT_SIDE = (
    rf"(?:{_CONTEXT_DIRECT_CURRENCY_PATTERN}|元|{_CONTEXT_BARE_AMOUNT}元?)"
)
_CONTEXT_PERIOD_TRANSACTION_ADJACENT = re.compile(
    rf"(?:{_CONTEXT_PERIOD_MONTHS.pattern})"
    rf"{_CONTEXT_PERIOD_PAYMENT_SIDE}"
    rf"|{_CONTEXT_PERIOD_PAYMENT_SIDE}"
    rf"(?:{_CONTEXT_PERIOD_MONTHS.pattern})"
)
_COMPARISON_SYMBOLS = frozenset({">", "<", "≥", "≤", "≈", "≃", "≲", "≳"})
_COMPARISON_AMOUNT = re.compile(_CONTEXT_BARE_AMOUNT)
_COMPARISON_AMOUNT_AT_END = re.compile(rf"{_CONTEXT_BARE_AMOUNT}$")
_COMPARISON_SPEC_UNITS = (
    "MAH",
    "GB",
    "TB",
    "MB",
    "KB",
    "PX",
    "MP",
    "WH",
    "HZ",
    "英寸",
    "P",
    "寸",
    "W",
    "G",
    "克",
)
_MODEL_AMOUNT_PREFIXES = (
    "型号",
    "机型",
    "小米",
    "XIAOMI",
    "MI",
    "HONOR",
    "荣耀",
    "华为",
    "HUAWEI",
    "维沃",
    "VIVO",
    "欧珀",
    "OPPO",
    "苹果",
    "APPLE",
    "IPHONE",
    "ZTE",
    "中兴",
)
_MODEL_AMOUNT_SUFFIXES = ("型号", "机型")
_CONTEXT_RISK_MODEL_AMOUNT = re.compile(
    rf"{_CONTEXT_RISK_PATTERN}(?:型号|机型){_CONTEXT_BARE_AMOUNT}"
)
_CONFUSABLE_ASCII_TRANSLATION = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "І": "I",
        "Ј": "J",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Ѕ": "S",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
    }
)
_RGB = re.compile(
    r"rgb\(\s*(?P<red>[0-9]{1,3})\s*,\s*"
    r"(?P<green>[0-9]{1,3})\s*,\s*"
    r"(?P<blue>[0-9]{1,3})\s*\)",
    re.IGNORECASE,
)
_RGBA = re.compile(
    r"rgba\(\s*(?P<red>[0-9]{1,3})\s*,\s*"
    r"(?P<green>[0-9]{1,3})\s*,\s*"
    r"(?P<blue>[0-9]{1,3})\s*,\s*"
    r"(?P<alpha>1(?:\.0+)?)\s*\)",
    re.IGNORECASE,
)
_APPROVED_RED_RGB = frozenset({(255, 0, 0), (255, 51, 0)})
_APPROVED_RED_HEX = frozenset({"#FF0000", "#FF3300", "#F00", "#F30"})
_MINIMUM_BARE_PHONE_PRICE = Decimal("100")
_APPROVED_PRICE_LABELS = frozenset(
    {
        "",
        "销售价",
        "售价",
        "到手价",
        "价格",
        "现价",
        "活动价",
        "会员价",
        "商品价格",
    }
)
_STRONG_SELLING_LABELS = frozenset(
    {
        "销售价",
        "售价",
        "到手价",
        "现价",
        "活动价",
        "会员价",
    }
)


class PricePolicy(StrEnum):
    LOWEST = "lowest"
    HIGHEST = "highest"
    RED_SELLING = "red_selling"


class SellingPriceEvidence(StrEnum):
    UNVERIFIED = "unverified"
    STRONG_SELLING_LABEL = "strong_selling_label"
    VERIFIED_CURRENT_SKU_SELLING_NODE = "verified_current_sku_selling_node"
    VERIFIED_CURRENT_SKU_STRUCK_THROUGH_PRICE = (
        "verified_current_sku_struck_through_price"
    )


@dataclass(frozen=True, slots=True)
class PriceCandidate:
    text: str
    context: str
    visible: bool
    computed_color: str
    selling_evidence: SellingPriceEvidence
    effective_line_through: bool

    def __post_init__(self) -> None:
        for field_name in ("text", "context", "computed_color"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string")
            normalized = value.strip()
            if not normalized:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, normalized)
        if type(self.visible) is not bool:
            raise ValueError("visible must be a boolean")
        if not isinstance(self.selling_evidence, SellingPriceEvidence):
            raise ValueError("selling_evidence must be a SellingPriceEvidence")
        if type(self.effective_line_through) is not bool:
            raise ValueError("effective_line_through must be a boolean")


def parse_price(text: str) -> Decimal | None:
    """Parse one unambiguous positive transaction amount without float conversion."""

    if not isinstance(text, str):
        return None
    normalized = unicodedata.normalize("NFKC", text).strip().upper()
    if not normalized:
        return None
    if any(marker in normalized for marker in _AMBIGUOUS_MARKERS):
        return None

    matches = tuple(_AMOUNT.finditer(normalized))
    if len(matches) != 1:
        return None
    match = matches[0]
    before_amount = normalized[: match.start()]
    after_amount = normalized[match.end() :]
    outside_amount = before_amount + after_amount
    if any(character.isdigit() for character in outside_amount):
        return None
    if any(marker in outside_amount for marker in ("¥", "￥", "元")):
        return None
    if not _is_approved_price_label(
        before_amount
    ) or not _is_approved_price_label(after_amount):
        return None
    try:
        amount = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0:
        return None

    has_explicit_money_marker = (
        match.group("currency") is not None or match.group("yuan") is not None
    )
    if not has_explicit_money_marker and amount < _MINIMUM_BARE_PHONE_PRICE:
        return None
    return amount


def filter_valid_prices(
    candidates: Sequence[PriceCandidate],
) -> tuple[PriceCandidate, ...]:
    """Keep visible, parseable transaction prices in their original order."""

    _validate_candidates(candidates)
    valid: list[PriceCandidate] = []
    for candidate in candidates:
        if not candidate.visible:
            continue
        if (
            candidate.effective_line_through
            and candidate.selling_evidence
            is not SellingPriceEvidence.VERIFIED_CURRENT_SKU_STRUCK_THROUGH_PRICE
        ):
            continue
        if not _has_positive_selling_price_evidence(candidate):
            continue
        combined = _normalize_business_text(
            f"{candidate.text} {candidate.context}"
        )
        if any(term in combined for term in _EXCLUDED_TERMS):
            continue
        if _INSTALLMENT_COUNT.search(combined) is not None:
            continue
        if _has_structural_exclusion(candidate.text) or _has_structural_exclusion(
            candidate.context
        ):
            continue
        if _context_has_fail_closed_semantics(candidate.context):
            continue
        if parse_price(candidate.text) is None:
            continue
        valid.append(candidate)
    return tuple(valid)


def _has_positive_selling_price_evidence(
    candidate: PriceCandidate,
) -> bool:
    if (
        candidate.selling_evidence
        is SellingPriceEvidence.VERIFIED_CURRENT_SKU_SELLING_NODE
    ):
        return True
    if (
        candidate.selling_evidence
        is SellingPriceEvidence.VERIFIED_CURRENT_SKU_STRUCK_THROUGH_PRICE
    ):
        return candidate.effective_line_through
    if (
        candidate.selling_evidence
        is not SellingPriceEvidence.STRONG_SELLING_LABEL
    ):
        return False
    structured_label = unicodedata.normalize("NFKC", candidate.context).strip()
    return structured_label in _STRONG_SELLING_LABELS


def choose_price(
    candidates: Sequence[PriceCandidate],
    policy: PricePolicy,
) -> Decimal | None:
    """Choose a deterministic Decimal price under the approved site policy."""

    if not isinstance(policy, PricePolicy):
        raise ValueError("policy must be a PricePolicy")
    valid = filter_valid_prices(candidates)
    if policy is PricePolicy.RED_SELLING:
        valid = tuple(
            candidate
            for candidate in valid
            if _is_approved_red(candidate.computed_color)
        )

    values = tuple(
        amount
        for candidate in valid
        if (amount := parse_price(candidate.text)) is not None
    )
    if not values:
        return None
    if policy is PricePolicy.LOWEST:
        return min(values)
    return max(values)


def _validate_candidates(candidates: Sequence[PriceCandidate]) -> None:
    if isinstance(candidates, str | bytes) or not isinstance(candidates, Sequence):
        raise ValueError("candidates must be a sequence of PriceCandidate values")
    if not all(isinstance(candidate, PriceCandidate) for candidate in candidates):
        raise ValueError("candidates must contain only PriceCandidate values")


def _normalize_business_text(value: str) -> str:
    return _canonicalize_risk_text(value, preserve_comparisons=False)


def _canonicalize_risk_text(
    value: str,
    *,
    preserve_comparisons: bool,
) -> str:
    normalized = unicodedata.normalize("NFKC", value).upper()
    return "".join(
        character
        for character in normalized
        if (
            preserve_comparisons and character in _COMPARISON_SYMBOLS
        ) or not _is_risk_separator(character)
    )


def _is_risk_separator(character: str) -> bool:
    category = unicodedata.category(character)
    return (
        character.isspace()
        or category.startswith(("M", "P", "Z"))
        or category in {"Cf", "Lm", "Sk", "Sm", "So"}
    )


def _has_structural_exclusion(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).upper()
    compact = "".join(
        character for character in normalized if not character.isspace()
    )
    semantic = "".join(
        character
        for character in compact
        if not unicodedata.category(character).startswith("P")
    )
    return (
        _PERIODIC_SUFFIX.search(compact) is not None
        or _APPROXIMATE_PREFIX.search(semantic) is not None
        or _APPROXIMATE_SUFFIX.search(semantic) is not None
    )


def _context_has_fail_closed_semantics(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).upper()
    compact = _normalize_business_text(normalized)
    comparison_compact = _canonicalize_risk_text(
        normalized,
        preserve_comparisons=True,
    )
    if any(
        unicodedata.category(character) == "Sc"
        and character not in {"¥", "￥"}
        for character in normalized
    ):
        return True
    currency_skeleton = _currency_code_skeleton(compact)
    if _FOREIGN_CURRENCY_CODE.search(currency_skeleton) is not None:
        return True
    if any(alias in compact for alias in _FOREIGN_CURRENCY_LONG_ALIASES):
        return True
    if _context_has_ambiguous_currency_alias(compact):
        return True
    if _context_has_invalid_comparison(comparison_compact):
        return True
    if any(
        term in compact for term in _CONTEXT_EXPLICIT_NON_TRANSACTION_TERMS
    ):
        return True
    if (
        compact in _CONTEXT_STANDALONE_RISK_LABELS
        or _CONTEXT_PERIOD_MONTHS.fullmatch(compact) is not None
    ):
        return True
    contains_risk_term = _context_contains_risk_term(compact)
    if _has_non_ascii_decimal(normalized) and contains_risk_term:
        return True
    if not contains_risk_term:
        return False
    return (
        _context_has_adjacent_price_anchor(compact)
        or _context_has_adjacent_currency_anchor(compact)
        or _context_has_adjacent_bare_amount(compact)
    )


def _context_contains_risk_term(compact: str) -> bool:
    return (
        _CONTEXT_PERIOD_MONTHS.search(compact) is not None
        or any(term in compact for term in _CONTEXT_RISK_TERMS)
        or _CONTEXT_APPROXIMATE_YUE.search(compact) is not None
    )


def _context_has_adjacent_price_anchor(compact: str) -> bool:
    return _CONTEXT_RISK_PRICE_ADJACENT.search(compact) is not None


def _context_has_adjacent_currency_anchor(compact: str) -> bool:
    return (
        _CONTEXT_RISK_CURRENCY_ADJACENT.search(compact) is not None
        or _CONTEXT_RISK_YUAN_AMOUNT_ADJACENT.search(compact) is not None
        or _CONTEXT_PERIOD_TRANSACTION_ADJACENT.search(compact) is not None
    )


def _context_has_adjacent_bare_amount(compact: str) -> bool:
    has_price_anchor = any(
        anchor in compact for anchor in _CONTEXT_PRICE_ANCHORS
    )
    if (
        has_price_anchor
        and _CONTEXT_RISK_MODEL_AMOUNT.search(compact) is not None
    ):
        return True
    for pattern in (_CONTEXT_RISK_THEN_AMOUNT, _CONTEXT_AMOUNT_THEN_RISK):
        for match in pattern.finditer(compact):
            if not has_price_anchor and _is_model_amount_context(
                compact,
                match,
            ):
                continue
            return True
    return False


def _is_model_amount_context(
    compact: str,
    match: re.Match[str],
) -> bool:
    before_amount = compact[: match.start("amount")]
    after_match = compact[match.end() :]
    return any(
        before_amount.endswith(prefix) for prefix in _MODEL_AMOUNT_PREFIXES
    ) or any(
        after_match.startswith(suffix) for suffix in _MODEL_AMOUNT_SUFFIXES
    )


def _has_non_ascii_decimal(value: str) -> bool:
    return any(
        character.isdecimal() and character not in "0123456789"
        for character in value
    )


def _currency_code_skeleton(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.category(character).startswith("M")
    )
    return without_marks.translate(_CONFUSABLE_ASCII_TRANSLATION)


def _context_has_ambiguous_currency_alias(compact: str) -> bool:
    for alias in _FOREIGN_CURRENCY_SHORT_ALIASES:
        start = compact.find(alias)
        while start >= 0:
            end = start + len(alias)
            before = compact[:start]
            after = compact[end:]
            if (
                not before
                and not after
                or _currency_alias_has_explicit_context(before, after)
            ):
                return True
            start = compact.find(alias, start + 1)
    return False


def _currency_alias_has_explicit_context(before: str, after: str) -> bool:
    if any(
        before.endswith(term) or after.startswith(term)
        for term in _FOREIGN_CURRENCY_CONTEXT_TERMS
    ):
        return True
    if any(
        before.endswith(anchor) or after.startswith(anchor)
        for anchor in _CONTEXT_PRICE_ANCHORS
    ):
        return True
    return _price_amount_at_end(before) or _price_amount_at_start(after)


def _context_has_invalid_comparison(compact: str) -> bool:
    if compact and all(character in _COMPARISON_SYMBOLS for character in compact):
        return True
    for index, character in enumerate(compact):
        if character not in _COMPARISON_SYMBOLS:
            continue
        before = compact[:index]
        after = compact[index + 1 :]
        if any(
            before.endswith(anchor) or after.startswith(anchor)
            for anchor in _CONTEXT_PRICE_ANCHORS
        ):
            return True
        if _price_amount_at_end(before, following=after) or _price_amount_at_start(
            after
        ):
            return True
    return False


def _price_amount_at_start(value: str) -> bool:
    match = _COMPARISON_AMOUNT.match(value)
    if match is None:
        return False
    trailing = value[match.end() :]
    if trailing and trailing[0] in "0123456789.,":
        return False
    if _starts_with_comparison_spec_unit(trailing):
        return False
    return _is_phone_price_amount(match.group())


def _price_amount_at_end(value: str, *, following: str = "") -> bool:
    match = _COMPARISON_AMOUNT_AT_END.search(value)
    if match is None:
        return False
    if match.start() > 0 and value[match.start() - 1] in (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,"
    ):
        return False
    if _starts_with_comparison_spec_unit(following):
        return False
    return _is_phone_price_amount(match.group())


def _starts_with_comparison_spec_unit(value: str) -> bool:
    for unit in _COMPARISON_SPEC_UNITS:
        if not value.startswith(unit):
            continue
        if unit.isascii() and unit.isalpha():
            following_index = len(unit)
            if (
                following_index < len(value)
                and value[following_index] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            ):
                continue
        return True
    return False


def _is_phone_price_amount(value: str) -> bool:
    try:
        amount = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return False
    return amount.is_finite() and amount >= _MINIMUM_BARE_PHONE_PRICE


def _is_approved_price_label(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(character) == "Sc" for character in normalized
    ):
        return False
    compact = "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )
    return compact in _APPROVED_PRICE_LABELS


def _is_approved_red(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    if normalized in _APPROVED_RED_HEX:
        return True
    match = _RGB.fullmatch(normalized) or _RGBA.fullmatch(normalized)
    if match is None:
        return False
    rgb = (
        int(match.group("red")),
        int(match.group("green")),
        int(match.group("blue")),
    )
    return rgb in _APPROVED_RED_RGB

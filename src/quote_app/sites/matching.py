from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_WHITESPACE = re.compile(r"[\s\u00a0]+")
_MODEL_TOKEN = re.compile(r"[A-Z]+|\d+(?:\.\d+)?|[\u3400-\u9fff]+|[+-]")
_CAPACITY = re.compile(
    r"(?<![0-9A-Z.])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)(?![A-Z0-9])"
)
_TITLE_CAPACITY_ROLE = r"(?:RAM|ROM|STORAGE|运行内存|存储)"
_TITLE_CAPACITY = re.compile(
    rf"(?:{_TITLE_CAPACITY_ROLE}\s*[:：]?\s*)?"
    r"(?<![0-9A-Z.])\d+(?:\.\d+)?\s*(?:GB|TB)(?![A-Z0-9])"
    rf"(?:\s*{_TITLE_CAPACITY_ROLE})?"
)
_REQUESTED_CAPACITY = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)")
_RAM_ROLE = r"(?:RAM|运行内存)"
_STORAGE_ROLE = r"(?:ROM|STORAGE|存储)"
_CAPACITY_PREFIX = re.compile(rf"\s*(?:{_RAM_ROLE}\s*[:：]?\s*)?")
_CAPACITY_MIDDLE = re.compile(
    rf"\s*(?:{_RAM_ROLE}\s*)?"
    rf"[+/，,、|;；]+"
    rf"\s*(?:{_STORAGE_ROLE}\s*[:：]?\s*)?"
)
_CAPACITY_SUFFIX = re.compile(rf"\s*(?:{_STORAGE_ROLE}\s*)?")
_NETWORK_MARKER = re.compile(r"(?<![A-Z0-9])(?:4G|5G)(?![A-Z0-9])")
_MODEL_VARIANTS = frozenset(
    {
        "PRO",
        "PLUS",
        "ULTRA",
        "MAX",
        "SE",
        "S",
        "E",
        "MINI",
        "LITE",
        "NEO",
        "AIR",
        "EDGE",
        "FE",
        "GT",
        "青春版",
        "活力版",
        "竞速版",
        "至尊版",
    }
)
_ACCESSORY_MARKERS = (
    "手机壳",
    "保护壳",
    "保护套",
    "手机套",
    "钢化膜",
    "屏幕膜",
    "贴膜",
    "充电器",
    "数据线",
    "耳机",
    "配件",
    "适用",
    "支架",
    "CASE",
    "COVER",
    "PROTECTOR",
    "FILM",
    "CHARGER",
    "CABLE",
    "HEADPHONES",
    "EARBUDS",
    "ACCESSORY",
)
_GENERIC_TITLE_WORDS = frozenset(
    {
        "新品",
        "手机",
        "智能手机",
        "官方",
        "正品",
        "官方正品",
        "旗舰",
        "旗舰手机",
        "全网通",
        "全网通手机",
        "移动版",
        "公开版",
        "PHONE",
        "SMARTPHONE",
        "NEW",
        "OFFICIAL",
        "GENUINE",
    }
)
_SUPPORTED_BRAND_WORDS = frozenset(
    {
        "HONOR",
        "HUAWEI",
        "VIVO",
        "OPPO",
        "APPLE",
        "ZTE",
        "荣耀",
        "华为",
        "维沃",
        "欧珀",
        "苹果",
        "小米",
        "中兴",
        "XIAOMI",
        "REDMI",
        "红米",
        "中兴通讯",
    }
)


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Capacity:
    number: Decimal
    unit: str


def normalize_product_text(value: str) -> str:
    """Apply deterministic display normalization without erasing punctuation."""

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).upper()
    return _WHITESPACE.sub(" ", normalized).strip()


def model_matches(target: str, candidate: str) -> bool:
    """Match one exact model in model/capacity text, excluding color option text."""

    wanted = normalize_product_text(target)
    actual = normalize_product_text(candidate)
    if not wanted or not actual:
        return False
    if any(marker in actual for marker in _ACCESSORY_MARKERS):
        return False

    wanted_tokens = _model_tokens(wanted)
    actual_tokens = _model_tokens(actual)
    if not wanted_tokens or not actual_tokens:
        return False

    matches = _token_sequence_matches(wanted_tokens, actual_tokens)
    if len(matches) != 1:
        return False
    first_index, last_index = matches[0]
    first = actual_tokens[first_index]
    last = actual_tokens[last_index]

    if _has_attached_ascii(actual, first.start - 1):
        return False
    if _has_attached_ascii(actual, last.end):
        return False
    suffix = actual[last.end :]
    first_suffix_character = next(
        (character for character in suffix if not character.isspace()),
        None,
    )
    if wanted_tokens[-1].value != "+" and first_suffix_character == "+":
        return False

    if not _title_remainder_is_allowed(
        actual[: first.start],
        _GENERIC_TITLE_WORDS | _SUPPORTED_BRAND_WORDS,
    ):
        return False
    if not _title_remainder_is_allowed(
        suffix,
        _GENERIC_TITLE_WORDS,
    ):
        return False

    following = _first_semantic_token(actual_tokens[last_index + 1 :])
    if following is not None and following.value in _MODEL_VARIANTS:
        return False

    anchor = _model_anchor(wanted_tokens)
    if anchor is not None:
        anchor_count = sum(token.value == anchor for token in actual_tokens)
        if anchor_count != 1:
            return False
    return True


def capacity_matches(label: str, ram: str, storage: str) -> bool:
    """Match one explicit RAM-to-storage capacity pair, failing closed."""

    normalized = normalize_product_text(label)
    wanted_ram = _requested_capacity(ram)
    wanted_storage = _requested_capacity(storage)
    if not normalized or wanted_ram is None or wanted_storage is None:
        return False

    matches = tuple(_CAPACITY.finditer(normalized))
    if len(matches) != 2:
        return False
    parsed = tuple(_capacity_from_match(match) for match in matches)
    if parsed != (wanted_ram, wanted_storage):
        return False

    before_ram = normalized[: matches[0].start()]
    between = normalized[matches[0].end() : matches[1].start()]
    after_storage = normalized[matches[1].end() :]
    return (
        _CAPACITY_PREFIX.fullmatch(before_ram) is not None
        and _CAPACITY_MIDDLE.fullmatch(between) is not None
        and _CAPACITY_SUFFIX.fullmatch(after_storage) is not None
    )


def color_matches(target: str, candidate: str) -> bool:
    """Match colors exactly after only NFKC, case, and whitespace normalization."""

    wanted = _normalize_exact_text(target)
    actual = _normalize_exact_text(candidate)
    return bool(wanted and actual and wanted == actual)


def _model_tokens(text: str) -> tuple[_Token, ...]:
    return tuple(
        _Token(match.group(), match.start(), match.end())
        for match in _MODEL_TOKEN.finditer(text)
    )


def _token_sequence_matches(
    wanted: tuple[_Token, ...],
    actual: tuple[_Token, ...],
) -> list[tuple[int, int]]:
    width = len(wanted)
    wanted_values = tuple(token.value for token in wanted)
    matches: list[tuple[int, int]] = []
    for start in range(len(actual) - width + 1):
        values = tuple(token.value for token in actual[start : start + width])
        if values == wanted_values:
            matches.append((start, start + width - 1))
    return matches


def _has_attached_ascii(text: str, index: int) -> bool:
    return 0 <= index < len(text) and text[index].isascii() and text[index].isalnum()


def _first_semantic_token(tokens: tuple[_Token, ...]) -> _Token | None:
    return next((token for token in tokens if token.value not in {"+", "-"}), None)


def _model_anchor(tokens: tuple[_Token, ...]) -> str | None:
    return next(
        (
            token.value
            for token in tokens
            if not token.value[0].isdigit()
            and token.value not in {"+", "-"}
            and token.value not in _MODEL_VARIANTS
        ),
        None,
    )


def _title_remainder_is_allowed(
    text: str,
    allowed_words: frozenset[str],
) -> bool:
    without_capacities = _TITLE_CAPACITY.sub(" ", text)
    without_network = _NETWORK_MARKER.sub(" ", without_capacities)
    return all(
        token.value in allowed_words or token.value in {"+", "-"}
        for token in _model_tokens(without_network)
    )


def _requested_capacity(value: str) -> _Capacity | None:
    normalized = normalize_product_text(value)
    if not normalized:
        return None
    match = _REQUESTED_CAPACITY.fullmatch(normalized)
    if match is None:
        return None
    return _capacity_from_match(match)


def _capacity_from_match(match: re.Match[str]) -> _Capacity:
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation:  # pragma: no cover - regex permits decimal numbers only
        number = Decimal("NaN")
    return _Capacity(number=number, unit=match.group("unit"))


def _normalize_exact_text(value: str) -> str:
    normalized = normalize_product_text(value)
    return _WHITESPACE.sub("", normalized)

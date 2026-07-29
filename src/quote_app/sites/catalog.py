from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from quote_app.sites.prices import PricePolicy
from quote_app.tasks.models import WebsiteChannel

SUPPORTED_BRANDS: tuple[str, ...] = (
    "小米",
    "HONOR",
    "华为",
    "维沃",
    "欧珀",
    "苹果",
    "ZTE中兴",
)

_CATALOG_FIELDS = frozenset({"brand", "channel", "entry_url", "store_name", "price_policy"})
_CATALOG_RELATIVE_PATH = Path("resources/sites/catalog.json")

_APPROVED_RECORDS: tuple[tuple[str, WebsiteChannel, str, str, PricePolicy], ...] = (
    (
        "小米",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000004123.html?from=pc",
        "小米京东自营旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "HONOR",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000000904.html",
        "荣耀京东自营旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "华为",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000004259.html?from=pc",
        "华为京东自营官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "维沃",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000085868.html?from=pc",
        "vivo京东自营官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "欧珀",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000004065.html?from=pc",
        "OPPO京东自营官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "苹果",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000000127.html?from=pc",
        "Apple产品京东自营旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "ZTE中兴",
        WebsiteChannel.JD,
        "https://mall.jd.com/index-1000001971.html?from=pc",
        "中兴京东自营官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "小米",
        WebsiteChannel.TMALL,
        "https://xiaomi.tmall.com/",
        "小米官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "HONOR",
        WebsiteChannel.TMALL,
        "https://hihonor.tmall.com/",
        "荣耀官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "华为",
        WebsiteChannel.TMALL,
        "https://huaweistore.tmall.com/",
        "华为官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "维沃",
        WebsiteChannel.TMALL,
        "https://vivo.tmall.com/",
        "vivo官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "欧珀",
        WebsiteChannel.TMALL,
        "https://oppo.tmall.com/",
        "OPPO官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "苹果",
        WebsiteChannel.TMALL,
        "https://apple.tmall.com/",
        "Apple Store官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "ZTE中兴",
        WebsiteChannel.TMALL,
        "https://zte.tmall.com/",
        "ZTE中兴官方旗舰店",
        PricePolicy.HIGHEST,
    ),
    (
        "小米",
        WebsiteChannel.OFFICIAL,
        "https://www.mi.com/shop",
        "小米商城",
        PricePolicy.RED_SELLING,
    ),
    (
        "HONOR",
        WebsiteChannel.OFFICIAL,
        "https://www.honor.com/cn/shop/?cid=132355",
        "荣耀商城",
        PricePolicy.LOWEST,
    ),
    (
        "华为",
        WebsiteChannel.OFFICIAL,
        "https://www.vmall.com/",
        "华为商城",
        PricePolicy.LOWEST,
    ),
    (
        "维沃",
        WebsiteChannel.OFFICIAL,
        "https://shop.vivo.com.cn/",
        "vivo官方商城",
        PricePolicy.LOWEST,
    ),
    (
        "欧珀",
        WebsiteChannel.OFFICIAL,
        "https://www.opposhop.cn/cn/web/",
        "OPPO商城",
        PricePolicy.LOWEST,
    ),
    (
        "苹果",
        WebsiteChannel.OFFICIAL,
        "https://www.apple.com.cn/iphone/",
        "Apple iPhone",
        PricePolicy.HIGHEST,
    ),
    (
        "ZTE中兴",
        WebsiteChannel.OFFICIAL,
        "https://www.ztemall.com/",
        "中兴商城",
        PricePolicy.LOWEST,
    ),
)

_APPROVED_BY_PAIR = {
    (brand, channel): (entry_url, store_name, price_policy)
    for brand, channel, entry_url, store_name, price_policy in _APPROVED_RECORDS
}
_EXPECTED_PAIRS = frozenset(_APPROVED_BY_PAIR)


class SiteCatalogError(ValueError):
    """The local site catalog is missing, malformed, or not approved."""


class _DuplicateJsonKeyError(ValueError):
    pass


def site_session_family(brand: str, channel: WebsiteChannel) -> str:
    """Return the exact persistent-page/login scope for one website task."""
    if not isinstance(brand, str) or not brand.strip():
        raise ValueError("brand must not be blank")
    if not isinstance(channel, WebsiteChannel):
        raise TypeError("channel must be a WebsiteChannel")
    if channel is WebsiteChannel.OFFICIAL:
        return f"official:{brand.strip()}"
    return channel.value


@dataclass(frozen=True, slots=True)
class SiteSpec:
    brand: str
    channel: WebsiteChannel
    entry_url: str
    store_name: str
    price_policy: PricePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.brand, str) or self.brand not in SUPPORTED_BRANDS:
            raise ValueError("brand must be an approved canonical brand")
        if not isinstance(self.channel, WebsiteChannel):
            raise TypeError("channel must be a WebsiteChannel")
        if not isinstance(self.price_policy, PricePolicy):
            raise TypeError("price_policy must be a PricePolicy")
        for field_name in ("entry_url", "store_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        _validate_url(self.entry_url)
        self.validate_approved()

    def validate_approved(self) -> None:
        expected = _APPROVED_BY_PAIR.get((self.brand, self.channel))
        actual = (self.entry_url, self.store_name, self.price_policy)
        if expected is None or actual != expected:
            raise ValueError("SiteSpec values must match the approved catalog")


def load_site_catalog(
    path: str | Path | None = None,
) -> tuple[SiteSpec, ...]:
    """Read and fully validate the bundled, local-only site catalog."""

    catalog_path = _resolve_catalog_path(path)
    try:
        raw_text = catalog_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError) as exc:
        raise SiteCatalogError(f"unable to load site catalog: {exc}") from exc

    if not isinstance(raw, list):
        raise SiteCatalogError("catalog root must be a JSON array")

    records: dict[tuple[str, WebsiteChannel], SiteSpec] = {}
    for index, value in enumerate(raw):
        spec = _parse_record(value, index)
        pair = (spec.brand, spec.channel)
        if pair in records:
            raise SiteCatalogError(f"duplicate brand/channel pair at record {index}")
        records[pair] = spec

    actual_pairs = frozenset(records)
    if actual_pairs != _EXPECTED_PAIRS:
        raise SiteCatalogError("catalog must contain the exact 21 approved pairs")

    for pair, spec in records.items():
        expected_url, expected_store, expected_policy = _APPROVED_BY_PAIR[pair]
        if (
            spec.entry_url != expected_url
            or spec.store_name != expected_store
            or spec.price_policy is not expected_policy
        ):
            raise SiteCatalogError(f"catalog values do not match the approved values for {pair}")

    return tuple(records[(brand, channel)] for brand, channel, *_ in _APPROVED_RECORDS)


def _parse_record(value: Any, index: int) -> SiteSpec:
    if not isinstance(value, dict):
        raise SiteCatalogError(f"catalog record {index} must be a JSON object")
    if set(value) != _CATALOG_FIELDS:
        raise SiteCatalogError(f"catalog record {index} has missing or extra fields")
    if not all(isinstance(value[field], str) for field in _CATALOG_FIELDS):
        raise SiteCatalogError(f"catalog record {index} fields must be strings")

    brand = value["brand"]
    if brand not in SUPPORTED_BRANDS:
        raise SiteCatalogError(f"catalog record {index} has an unsupported brand")
    try:
        channel = WebsiteChannel(value["channel"])
    except ValueError as exc:
        raise SiteCatalogError(f"catalog record {index} has an unsupported channel") from exc
    try:
        price_policy = PricePolicy(value["price_policy"])
    except ValueError as exc:
        raise SiteCatalogError(f"catalog record {index} has an unsupported price policy") from exc

    try:
        return SiteSpec(
            brand=brand,
            channel=channel,
            entry_url=value["entry_url"],
            store_name=value["store_name"],
            price_policy=price_policy,
        )
    except (TypeError, ValueError) as exc:
        raise SiteCatalogError(f"catalog record {index} is invalid") from exc


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("entry_url must be HTTPS with a hostname and no credentials")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _resolve_catalog_path(path: str | Path | None) -> Path:
    if path is None:
        return _resource_root() / _CATALOG_RELATIVE_PATH
    if not isinstance(path, str | Path):
        raise TypeError("catalog path must be a string or Path")
    requested = Path(path)
    if requested.is_absolute():
        return requested
    return _resource_root() / requested


def _resource_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_root, str) and bundle_root:
        return Path(bundle_root)
    return Path(__file__).resolve().parents[3]

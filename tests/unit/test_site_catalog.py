from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from quote_app.sites.catalog import (
    SUPPORTED_BRANDS,
    SiteCatalogError,
    SiteSpec,
    load_site_catalog,
)
from quote_app.sites.prices import PricePolicy
from quote_app.tasks.models import WebsiteChannel


EXPECTED_RECORDS = (
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
        "https://www.apple.com.cn/shop/buy-iphone",
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

EXPECTED_RAW: list[dict[str, object]] = [
    {
        "brand": brand,
        "channel": channel.value,
        "entry_url": entry_url,
        "store_name": store_name,
        "price_policy": price_policy.value,
    }
    for brand, channel, entry_url, store_name, price_policy in EXPECTED_RECORDS
]


def _write_catalog(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _mutated_catalog(index: int, field: str, value: object) -> list[dict[str, object]]:
    records = [dict(record) for record in EXPECTED_RAW]
    records[index][field] = value
    return records


def test_catalog_contains_exact_approved_21_records() -> None:
    catalog = load_site_catalog()

    assert isinstance(catalog, tuple)
    assert (
        tuple(
            (
                item.brand,
                item.channel,
                item.entry_url,
                item.store_name,
                item.price_policy,
            )
            for item in catalog
        )
        == EXPECTED_RECORDS
    )
    assert {(item.brand, item.channel) for item in catalog} == {
        (brand, channel) for brand in SUPPORTED_BRANDS for channel in WebsiteChannel
    }


def test_catalog_value_objects_are_frozen_slotted_and_strongly_typed() -> None:
    item = load_site_catalog()[0]

    assert isinstance(item, SiteSpec)
    assert isinstance(item.channel, WebsiteChannel)
    assert isinstance(item.price_policy, PricePolicy)
    assert not hasattr(item, "__dict__")
    with pytest.raises(FrozenInstanceError):
        item.brand = "华为"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("entry_url", "https://example.com/not-approved"),
        ("store_name", "未批准店铺"),
        ("price_policy", PricePolicy.LOWEST),
    ],
)
def test_site_spec_cannot_be_constructed_with_unapproved_pair_values(
    field: str,
    replacement: object,
) -> None:
    values: dict[str, object] = {
        "brand": "小米",
        "channel": WebsiteChannel.JD,
        "entry_url": "https://mall.jd.com/index-1000004123.html?from=pc",
        "store_name": "小米京东自营旗舰店",
        "price_policy": PricePolicy.HIGHEST,
    }
    values[field] = replacement

    with pytest.raises(ValueError, match="approved"):
        SiteSpec(**values)  # type: ignore[arg-type]


def test_default_catalog_loading_does_not_depend_on_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert len(load_site_catalog()) == 21
    assert len(load_site_catalog("resources/sites/catalog.json")) == 21


def test_default_catalog_loads_from_pyinstaller_bundle_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundled_catalog = tmp_path / "resources" / "sites" / "catalog.json"
    bundled_catalog.parent.mkdir(parents=True)
    bundled_catalog.write_text(
        json.dumps(EXPECTED_RAW, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.chdir(tmp_path.parent)

    assert len(load_site_catalog()) == 21
    assert len(load_site_catalog("resources/sites/catalog.json")) == 21


@pytest.mark.parametrize("root", [{}, "records", 21, None, True])
def test_catalog_root_must_be_a_json_array(tmp_path: Path, root: object) -> None:
    path = _write_catalog(tmp_path, root)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(path)


@pytest.mark.parametrize("record", [None, [], "record", 1, True])
def test_each_catalog_record_must_be_a_json_object(
    tmp_path: Path,
    record: object,
) -> None:
    records: list[object] = [dict(item) for item in EXPECTED_RAW]
    records[0] = record

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


def test_duplicate_json_object_key_is_rejected(tmp_path: Path) -> None:
    original = json.dumps(EXPECTED_RAW, ensure_ascii=False)
    malicious_first = (
        '{"brand":"小米","brand":"华为","channel":"jd",'
        '"entry_url":"https://mall.jd.com/index-1000004123.html?from=pc",'
        '"store_name":"小米京东自营旗舰店","price_policy":"highest"}'
    )
    suffix = original[original.find("},") + 2 :]
    path = tmp_path / "duplicate-key.json"
    path.write_text(f"[{malicious_first},{suffix}", encoding="utf-8")

    with pytest.raises(SiteCatalogError, match="duplicate"):
        load_site_catalog(path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("brand", None),
        ("channel", 1),
        ("entry_url", True),
        ("store_name", []),
        ("price_policy", {}),
    ],
)
def test_catalog_fields_require_json_strings(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    path = _write_catalog(tmp_path, _mutated_catalog(0, field, replacement))

    with pytest.raises(SiteCatalogError):
        load_site_catalog(path)


@pytest.mark.parametrize("field", ["brand", "channel", "entry_url", "store_name", "price_policy"])
def test_missing_required_field_is_rejected(tmp_path: Path, field: str) -> None:
    records = [dict(item) for item in EXPECTED_RAW]
    records[0].pop(field)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


def test_additional_field_is_rejected(tmp_path: Path) -> None:
    records = _mutated_catalog(0, "notes", "not approved")

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


@pytest.mark.parametrize("brand", ["荣耀", "HUAWEI", "VIVO", "OPPO", "Apple", "ZTE", " 小米 "])
def test_catalog_does_not_normalize_or_alias_brands(tmp_path: Path, brand: str) -> None:
    records = _mutated_catalog(0, "brand", brand)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


@pytest.mark.parametrize("channel", ["JD", "taobao", "", " official "])
def test_unapproved_channel_is_rejected(tmp_path: Path, channel: str) -> None:
    records = _mutated_catalog(0, "channel", channel)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


@pytest.mark.parametrize("policy", ["minimum", "maximum", "red", "", " highest "])
def test_unapproved_price_policy_is_rejected(tmp_path: Path, policy: str) -> None:
    records = _mutated_catalog(0, "price_policy", policy)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


def test_duplicate_brand_channel_pair_is_rejected(tmp_path: Path) -> None:
    records = [dict(item) for item in EXPECTED_RAW]
    records[-1] = dict(records[0])

    with pytest.raises(SiteCatalogError, match="duplicate"):
        load_site_catalog(_write_catalog(tmp_path, records))


def test_incomplete_cartesian_product_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, EXPECTED_RAW[:-1]))


@pytest.mark.parametrize(
    ("index", "field", "replacement"),
    [
        (0, "entry_url", "https://mall.jd.com/different"),
        (0, "store_name", "小米旗舰店"),
        (14, "price_policy", "lowest"),
        (15, "price_policy", "highest"),
    ],
)
def test_each_pair_must_retain_its_approved_values(
    tmp_path: Path,
    index: int,
    field: str,
    replacement: str,
) -> None:
    records = _mutated_catalog(index, field, replacement)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


@pytest.mark.parametrize(
    "url",
    [
        "http://mall.jd.com/index.html",
        "ftp://mall.jd.com/index.html",
        "https:///missing-host",
        "https://user@mall.jd.com/index.html",
        "https://user:secret@mall.jd.com/index.html",
    ],
)
def test_entry_url_requires_https_hostname_and_no_credentials(
    tmp_path: Path,
    url: str,
) -> None:
    records = _mutated_catalog(0, "entry_url", url)

    with pytest.raises(SiteCatalogError):
        load_site_catalog(_write_catalog(tmp_path, records))


def test_invalid_json_and_missing_file_are_wrapped_as_catalog_errors(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[", encoding="utf-8")

    with pytest.raises(SiteCatalogError):
        load_site_catalog(invalid)
    with pytest.raises(SiteCatalogError):
        load_site_catalog(tmp_path / "missing.json")

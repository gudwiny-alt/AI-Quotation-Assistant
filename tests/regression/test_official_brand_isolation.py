from __future__ import annotations

import pytest

from quote_app.sites.catalog import SiteSpec, load_site_catalog
from quote_app.sites.jd import JDAdapter
from quote_app.sites.official import OfficialSiteAdapter
from quote_app.sites.official_brands.factory import (
    AppleOfficialAdapter,
    HuaweiOfficialAdapter,
    OppoOfficialAdapter,
    VivoOfficialAdapter,
    XiaomiOfficialAdapter,
)
from quote_app.sites.registry import AdapterRegistry
from quote_app.sites.tmall import TmallAdapter
from quote_app.tasks.models import WebsiteChannel


def _spec(
    catalog: tuple[SiteSpec, ...],
    brand: str,
    channel: WebsiteChannel,
) -> SiteSpec:
    return next(
        spec
        for spec in catalog
        if spec.brand == brand and spec.channel is channel
    )


@pytest.mark.parametrize("brand", ["HONOR", "ZTE中兴"])
def test_official_dispatch_does_not_replace_frozen_official_adapters(
    brand: str,
) -> None:
    catalog = load_site_catalog()
    registry = AdapterRegistry(catalog=catalog)
    expected_spec = _spec(catalog, brand, WebsiteChannel.OFFICIAL)

    adapter = registry.adapter_for(brand, WebsiteChannel.OFFICIAL)

    assert type(adapter) is OfficialSiteAdapter
    assert adapter.spec is expected_spec


@pytest.mark.parametrize(
    ("brand", "expected_type"),
    [
        ("小米", XiaomiOfficialAdapter),
        ("欧珀", OppoOfficialAdapter),
        ("维沃", VivoOfficialAdapter),
        ("华为", HuaweiOfficialAdapter),
        ("苹果", AppleOfficialAdapter),
    ],
)
def test_default_registry_routes_new_official_brands_without_copying_specs(
    brand: str,
    expected_type: type[OfficialSiteAdapter],
) -> None:
    catalog = load_site_catalog()
    registry = AdapterRegistry(catalog=catalog)
    expected_spec = _spec(catalog, brand, WebsiteChannel.OFFICIAL)

    adapter = registry.adapter_for(brand, WebsiteChannel.OFFICIAL)

    assert type(adapter) is expected_type
    assert adapter.spec is expected_spec


@pytest.mark.parametrize("brand", ["小米", "HONOR", "华为", "维沃", "欧珀", "苹果", "ZTE中兴"])
def test_official_dispatch_leaves_jd_and_tmall_default_classes_unchanged(
    brand: str,
) -> None:
    catalog = load_site_catalog()
    registry = AdapterRegistry(catalog=catalog)

    jd_adapter = registry.adapter_for(brand, WebsiteChannel.JD)
    tmall_adapter = registry.adapter_for(brand, WebsiteChannel.TMALL)

    assert type(jd_adapter) is JDAdapter
    assert jd_adapter.spec is _spec(catalog, brand, WebsiteChannel.JD)
    assert type(tmall_adapter) is TmallAdapter
    assert tmall_adapter.spec is _spec(catalog, brand, WebsiteChannel.TMALL)

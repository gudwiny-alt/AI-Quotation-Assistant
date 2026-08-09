from __future__ import annotations

import pytest

import quote_app.sites.official_brands.factory as factory_module
from quote_app.sites.catalog import SiteSpec, load_site_catalog
from quote_app.sites.official import OfficialSiteAdapter
from quote_app.sites.official_brands.factory import (
    AppleOfficialAdapter,
    HuaweiOfficialAdapter,
    OppoOfficialAdapter,
    VivoOfficialAdapter,
    XiaomiOfficialAdapter,
    create_official_adapter,
)
from quote_app.sites.registry import RegisteredSiteAdapter
from quote_app.tasks.models import WebsiteChannel


def _official_spec(brand: str) -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == brand and spec.channel is WebsiteChannel.OFFICIAL
    )


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
def test_factory_dispatches_each_new_brand_to_its_own_adapter_type(
    brand: str,
    expected_type: type[RegisteredSiteAdapter],
) -> None:
    spec = _official_spec(brand)

    adapter = create_official_adapter(spec)

    assert type(adapter) is expected_type
    assert isinstance(adapter, RegisteredSiteAdapter)
    assert adapter.spec is spec


@pytest.mark.parametrize("brand", ["HONOR", "ZTE中兴"])
def test_factory_keeps_frozen_brands_on_the_existing_official_adapter(
    brand: str,
) -> None:
    spec = _official_spec(brand)

    adapter = create_official_adapter(spec)

    assert type(adapter) is OfficialSiteAdapter
    assert adapter.spec is spec


def test_factory_rejects_non_official_specs() -> None:
    jd_spec = next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.JD
    )

    with pytest.raises(ValueError, match="OFFICIAL"):
        create_official_adapter(jd_spec)


def test_factory_fails_closed_when_a_non_legacy_brand_has_no_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _official_spec("小米")
    monkeypatch.setattr(
        factory_module,
        "_BRAND_ADAPTER_FACTORIES",
        {},
        raising=False,
    )

    with pytest.raises(LookupError, match="小米"):
        create_official_adapter(spec)

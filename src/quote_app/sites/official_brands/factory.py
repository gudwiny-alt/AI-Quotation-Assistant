from __future__ import annotations

from collections.abc import Callable, Mapping

from quote_app.sites.catalog import SiteSpec
from quote_app.sites.official import OfficialSiteAdapter
from quote_app.sites.official_brands.apple import AppleOfficialAdapter
from quote_app.sites.official_brands.huawei import HuaweiOfficialAdapter
from quote_app.sites.official_brands.oppo import OppoOfficialAdapter
from quote_app.sites.registry import RegisteredSiteAdapter
from quote_app.sites.official_brands.vivo import VivoOfficialAdapter
from quote_app.sites.official_brands.xiaomi import XiaomiOfficialAdapter
from quote_app.tasks.models import WebsiteChannel

OfficialAdapterFactory = Callable[[SiteSpec], RegisteredSiteAdapter]


_BRAND_ADAPTER_FACTORIES: Mapping[str, OfficialAdapterFactory] = {
    "小米": XiaomiOfficialAdapter,
    "欧珀": OppoOfficialAdapter,
    "维沃": VivoOfficialAdapter,
    "华为": HuaweiOfficialAdapter,
    "苹果": AppleOfficialAdapter,
}
_LEGACY_OFFICIAL_BRANDS = frozenset({"HONOR", "ZTE中兴"})


class OfficialBrandAdapterNotRegistered(LookupError):
    def __init__(self, brand: str) -> None:
        self.brand = brand
        super().__init__(f"official adapter is not registered for brand={brand!r}")


def create_official_adapter(spec: SiteSpec) -> RegisteredSiteAdapter:
    """Dispatch approved brands without changing frozen HONOR/ZTE behavior."""
    if not isinstance(spec, SiteSpec):
        raise TypeError("spec must be a SiteSpec")
    spec.validate_approved()
    if spec.channel is not WebsiteChannel.OFFICIAL:
        raise ValueError("spec channel must be OFFICIAL")
    adapter_factory = _BRAND_ADAPTER_FACTORIES.get(spec.brand)
    if adapter_factory is not None:
        return adapter_factory(spec)
    if spec.brand in _LEGACY_OFFICIAL_BRANDS:
        return OfficialSiteAdapter(spec)
    raise OfficialBrandAdapterNotRegistered(spec.brand)

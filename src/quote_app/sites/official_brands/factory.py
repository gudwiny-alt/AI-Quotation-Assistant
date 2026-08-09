from __future__ import annotations

from collections.abc import Mapping

from quote_app.sites.catalog import SiteSpec
from quote_app.sites.official import OfficialSiteAdapter
from quote_app.tasks.models import WebsiteChannel


class XiaomiOfficialAdapter(OfficialSiteAdapter):
    """Routing placeholder for the future Xiaomi live adapter."""


class OppoOfficialAdapter(OfficialSiteAdapter):
    """Routing placeholder for the future OPPO live adapter."""


class VivoOfficialAdapter(OfficialSiteAdapter):
    """Routing placeholder for the future vivo live adapter."""


class HuaweiOfficialAdapter(OfficialSiteAdapter):
    """Routing placeholder for the future Huawei live adapter."""


class AppleOfficialAdapter(OfficialSiteAdapter):
    """Routing placeholder for the future Apple live adapter."""


_BRAND_ADAPTER_CLASSES: Mapping[str, type[OfficialSiteAdapter]] = {
    "小米": XiaomiOfficialAdapter,
    "欧珀": OppoOfficialAdapter,
    "维沃": VivoOfficialAdapter,
    "华为": HuaweiOfficialAdapter,
    "苹果": AppleOfficialAdapter,
}


def create_official_adapter(spec: SiteSpec) -> OfficialSiteAdapter:
    """Dispatch approved brands without changing frozen HONOR/ZTE behavior."""
    if not isinstance(spec, SiteSpec):
        raise TypeError("spec must be a SiteSpec")
    spec.validate_approved()
    if spec.channel is not WebsiteChannel.OFFICIAL:
        raise ValueError("spec channel must be OFFICIAL")
    adapter_type = _BRAND_ADAPTER_CLASSES.get(spec.brand, OfficialSiteAdapter)
    return adapter_type(spec)

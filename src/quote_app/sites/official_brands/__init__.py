"""Brand-isolated official-site adapter dispatch."""

from quote_app.sites.official_brands.factory import (
    AppleOfficialAdapter,
    OfficialBrandAdapterNotRegistered,
    OppoOfficialAdapter,
    VivoOfficialAdapter,
    XiaomiOfficialAdapter,
    create_official_adapter,
)
from quote_app.sites.official_brands.huawei import HuaweiOfficialAdapter

__all__ = [
    "AppleOfficialAdapter",
    "HuaweiOfficialAdapter",
    "OfficialBrandAdapterNotRegistered",
    "OppoOfficialAdapter",
    "VivoOfficialAdapter",
    "XiaomiOfficialAdapter",
    "create_official_adapter",
]

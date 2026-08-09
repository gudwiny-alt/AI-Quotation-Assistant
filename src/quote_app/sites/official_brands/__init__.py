"""Brand-isolated official-site adapter dispatch."""

from quote_app.sites.official_brands.factory import (
    AppleOfficialAdapter,
    HuaweiOfficialAdapter,
    OfficialBrandAdapterNotRegistered,
    OppoOfficialAdapter,
    VivoOfficialAdapter,
    XiaomiOfficialAdapter,
    create_official_adapter,
)

__all__ = [
    "AppleOfficialAdapter",
    "HuaweiOfficialAdapter",
    "OfficialBrandAdapterNotRegistered",
    "OppoOfficialAdapter",
    "VivoOfficialAdapter",
    "XiaomiOfficialAdapter",
    "create_official_adapter",
]

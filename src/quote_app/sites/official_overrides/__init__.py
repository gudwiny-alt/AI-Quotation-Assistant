"""Explicit official-site flow overrides that do not belong in shared logic."""

from quote_app.sites.official_overrides.apple import AppleOfficialOverride
from quote_app.sites.official_overrides.honor import HonorOfficialOverride
from quote_app.sites.official_overrides.xiaomi import XiaomiOfficialOverride

__all__ = (
    "AppleOfficialOverride",
    "HonorOfficialOverride",
    "XiaomiOfficialOverride",
)

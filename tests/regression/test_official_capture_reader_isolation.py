from __future__ import annotations

from quote_app.evidence.macos_runtime import MacFormalCaptureRuntime
from quote_app.tasks.models import WebsiteChannel

_LIVE_OFFICIAL_BRANDS = frozenset({"小米", "欧珀", "维沃", "华为", "苹果"})
_MARKETPLACE_BRANDS = frozenset(
    {"HONOR", "小米", "欧珀", "维沃", "华为", "苹果"}
)


class _UnusedRegistry:
    def adapter_for(
        self,
        brand: str,
        channel: WebsiteChannel,
    ) -> object:
        raise AssertionError(f"unexpected adapter request: {brand}/{channel}")


def test_default_capture_reader_keys_cover_six_brand_marketplaces_and_officials() -> None:
    runtime = MacFormalCaptureRuntime(adapter_registry=_UnusedRegistry())

    expected = {
        *((brand, channel) for brand in _MARKETPLACE_BRANDS for channel in (
            WebsiteChannel.JD,
            WebsiteChannel.TMALL,
        )),
        ("HONOR", WebsiteChannel.OFFICIAL),
        *((brand, WebsiteChannel.OFFICIAL) for brand in _LIVE_OFFICIAL_BRANDS),
    }

    assert set(runtime._reader_factories) == expected
    assert all(
        runtime._reader_factories[(brand, channel)].__func__
        is runtime._injected_marketplace_reader_factory.__func__
        for brand in _MARKETPLACE_BRANDS
        for channel in (WebsiteChannel.JD, WebsiteChannel.TMALL)
    )
    assert all(
        runtime._reader_factories[(brand, WebsiteChannel.OFFICIAL)].__func__
        is runtime._injected_live_official_reader_factory.__func__
        for brand in _LIVE_OFFICIAL_BRANDS
    )


def test_marketplace_capture_readers_do_not_expand_to_unsupported_zte() -> None:
    runtime = MacFormalCaptureRuntime(adapter_registry=_UnusedRegistry())

    assert not any(
        ("ZTE中兴", channel) in runtime._reader_factories
        for channel in (WebsiteChannel.JD, WebsiteChannel.TMALL)
    )

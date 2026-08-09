from __future__ import annotations

from quote_app.evidence.macos_runtime import MacFormalCaptureRuntime
from quote_app.tasks.models import WebsiteChannel

_LIVE_OFFICIAL_BRANDS = frozenset({"小米", "欧珀", "维沃", "华为", "苹果"})


class _UnusedRegistry:
    def adapter_for(
        self,
        brand: str,
        channel: WebsiteChannel,
    ) -> object:
        raise AssertionError(f"unexpected adapter request: {brand}/{channel}")


def test_default_capture_reader_keys_are_exactly_frozen_honor_plus_live_official() -> None:
    runtime = MacFormalCaptureRuntime(adapter_registry=_UnusedRegistry())

    expected = {
        *(("HONOR", channel) for channel in WebsiteChannel),
        *((brand, WebsiteChannel.OFFICIAL) for brand in _LIVE_OFFICIAL_BRANDS),
    }

    assert set(runtime._reader_factories) == expected
    assert all(
        runtime._reader_factories[("HONOR", channel)].__func__
        is runtime._injected_honor_reader_factory.__func__
        for channel in WebsiteChannel
    )
    assert all(
        runtime._reader_factories[(brand, WebsiteChannel.OFFICIAL)].__func__
        is runtime._injected_live_official_reader_factory.__func__
        for brand in _LIVE_OFFICIAL_BRANDS
    )


def test_live_official_capture_readers_do_not_expand_marketplace_channels() -> None:
    runtime = MacFormalCaptureRuntime(adapter_registry=_UnusedRegistry())

    assert not any(
        (brand, channel) in runtime._reader_factories
        for brand in _LIVE_OFFICIAL_BRANDS
        for channel in (WebsiteChannel.JD, WebsiteChannel.TMALL)
    )

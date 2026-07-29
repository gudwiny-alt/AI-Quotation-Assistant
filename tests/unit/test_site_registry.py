from __future__ import annotations

from types import ModuleType
from typing import cast

import pytest

import quote_app.sites.registry as registry_module
from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.sites.catalog import SiteSpec, load_site_catalog
from quote_app.sites.protocol import BrowserPage
from quote_app.sites.registry import (
    AdapterFactory,
    AdapterNotRegistered,
    AdapterRegistry,
    UnsupportedBrand,
    adapter_for,
)
from quote_app.tasks.models import WebsiteChannel, WebsiteResult, WebsiteTask


class FakeAdapter:
    def __init__(self, spec: SiteSpec) -> None:
        self.spec = spec
        self.channel = spec.channel

    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult:
        raise AssertionError("unit test does not execute adapters")


def _overwrite_as_honor_jd(spec: SiteSpec) -> None:
    honor_jd = load_site_catalog()[1]
    for field_name in (
        "brand",
        "channel",
        "entry_url",
        "store_name",
        "price_policy",
    ):
        object.__setattr__(spec, field_name, getattr(honor_jd, field_name))


@pytest.mark.parametrize(
    "brand",
    ["其他品牌", "荣耀", "HUAWEI", "VIVO", "OPPO", "Apple", "ZTE", " 小米 ", ""],
)
def test_unknown_brand_is_explicitly_unsupported_and_preserves_input(
    brand: str,
) -> None:
    with pytest.raises(UnsupportedBrand) as caught:
        adapter_for(brand, WebsiteChannel.JD)

    assert caught.value.brand == brand


def test_unknown_brand_is_checked_before_channel_type() -> None:
    with pytest.raises(UnsupportedBrand) as caught:
        adapter_for("其他品牌", cast(WebsiteChannel, "jd"))

    assert caught.value.brand == "其他品牌"


@pytest.mark.parametrize("channel", ["jd", "JD", None, 1])
def test_supported_brand_requires_strong_channel_type(channel: object) -> None:
    with pytest.raises(TypeError, match="WebsiteChannel"):
        adapter_for("小米", cast(WebsiteChannel, channel))


def test_supported_official_pairs_resolve_exact_default_adapters() -> None:
    official_specs = tuple(
        spec
        for spec in load_site_catalog()
        if spec.channel is WebsiteChannel.OFFICIAL
    )

    adapters = tuple(
        adapter_for(spec.brand, WebsiteChannel.OFFICIAL)
        for spec in official_specs
    )

    assert tuple(adapter.spec for adapter in adapters) == official_specs  # type: ignore[attr-defined]
    assert all(
        adapter.channel is WebsiteChannel.OFFICIAL for adapter in adapters
    )


def test_injected_factories_receive_exact_spec_for_all_21_pairs() -> None:
    received: list[SiteSpec] = []

    def factory(spec: SiteSpec) -> FakeAdapter:
        received.append(spec)
        return FakeAdapter(spec)

    registry = AdapterRegistry(factories={channel: factory for channel in WebsiteChannel})

    adapters = [registry.adapter_for(spec.brand, spec.channel) for spec in load_site_catalog()]

    assert len(adapters) == 21
    assert tuple(received) == load_site_catalog()
    assert all(isinstance(adapter, FakeAdapter) for adapter in adapters)
    fake_adapters = [cast(FakeAdapter, adapter) for adapter in adapters]
    assert all(adapter.channel is adapter.spec.channel for adapter in fake_adapters)


def test_public_two_argument_entrypoint_accepts_explicit_registry() -> None:
    registry = AdapterRegistry(factories={WebsiteChannel.JD: FakeAdapter})

    adapter = adapter_for("小米", WebsiteChannel.JD, registry=registry)

    assert isinstance(adapter, FakeAdapter)
    assert adapter.spec.brand == "小米"
    assert adapter.spec.channel is WebsiteChannel.JD


def test_factory_is_lazy_and_only_requested_channel_is_called() -> None:
    calls: list[WebsiteChannel] = []

    def factory(spec: SiteSpec) -> FakeAdapter:
        calls.append(spec.channel)
        return FakeAdapter(spec)

    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: factory,
            WebsiteChannel.TMALL: factory,
        }
    )

    registry.adapter_for("苹果", WebsiteChannel.TMALL)

    assert calls == [WebsiteChannel.TMALL]


def test_missing_channel_factory_does_not_fall_back_to_another_factory() -> None:
    registry = AdapterRegistry(factories={WebsiteChannel.JD: FakeAdapter})

    with pytest.raises(AdapterNotRegistered) as caught:
        registry.adapter_for("小米", WebsiteChannel.TMALL)

    assert caught.value.brand == "小米"
    assert caught.value.channel is WebsiteChannel.TMALL


@pytest.mark.parametrize(
    "factory",
    [
        lambda spec: spec,
        lambda spec: object(),
    ],
)
def test_factory_must_return_a_real_site_adapter(factory: object) -> None:
    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: cast(AdapterFactory, factory),
        }
    )

    with pytest.raises(TypeError, match="SiteAdapter"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_adapter_channel_must_match_requested_spec() -> None:
    def wrong_channel_factory(spec: SiteSpec) -> FakeAdapter:
        adapter = FakeAdapter(spec)
        adapter.channel = WebsiteChannel.TMALL
        return adapter

    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: wrong_channel_factory,
        }
    )

    with pytest.raises(TypeError, match="channel"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_adapter_spec_must_match_exact_requested_brand_and_channel() -> None:
    honor_jd_spec = load_site_catalog()[1]
    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: lambda spec: FakeAdapter(honor_jd_spec),
        }
    )

    with pytest.raises(TypeError, match="SiteSpec"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_factory_error_is_not_hidden_by_a_fallback() -> None:
    error = RuntimeError("factory failed")

    def broken_factory(spec: SiteSpec) -> FakeAdapter:
        raise error

    registry = AdapterRegistry(factories={WebsiteChannel.JD: broken_factory})

    with pytest.raises(RuntimeError) as caught:
        registry.adapter_for("小米", WebsiteChannel.JD)

    assert caught.value is error


def test_injected_catalog_is_revalidated_against_approved_values() -> None:
    specs = list(load_site_catalog())
    object.__setattr__(specs[0], "entry_url", "https://example.com/not-approved")

    with pytest.raises(ValueError, match="approved"):
        AdapterRegistry(catalog=tuple(specs))


def test_cached_spec_is_revalidated_before_factory_call() -> None:
    specs = list(load_site_catalog())
    calls: list[SiteSpec] = []

    def factory(spec: SiteSpec) -> FakeAdapter:
        calls.append(spec)
        return FakeAdapter(spec)

    registry = AdapterRegistry(
        factories={WebsiteChannel.JD: factory},
        catalog=tuple(specs),
    )
    object.__setattr__(specs[0], "store_name", "被篡改店铺")

    with pytest.raises(ValueError, match="approved"):
        registry.adapter_for("小米", WebsiteChannel.JD)

    assert calls == []


def test_spec_mutated_during_factory_call_is_rejected() -> None:
    def mutating_factory(spec: SiteSpec) -> FakeAdapter:
        object.__setattr__(spec, "store_name", "被篡改店铺")
        return FakeAdapter(spec)

    registry = AdapterRegistry(
        factories={WebsiteChannel.JD: mutating_factory},
    )

    with pytest.raises(ValueError, match="approved"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_cached_spec_changed_to_another_approved_pair_is_rejected() -> None:
    specs = list(load_site_catalog())
    registry = AdapterRegistry(
        factories={WebsiteChannel.JD: FakeAdapter},
        catalog=tuple(specs),
    )
    _overwrite_as_honor_jd(specs[0])

    with pytest.raises(ValueError, match="requested"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_factory_cannot_change_spec_to_another_approved_pair() -> None:
    def pair_switching_factory(spec: SiteSpec) -> FakeAdapter:
        _overwrite_as_honor_jd(spec)
        return FakeAdapter(spec)

    registry = AdapterRegistry(
        factories={WebsiteChannel.JD: pair_switching_factory},
    )

    with pytest.raises(ValueError, match="requested"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_adapter_spec_getter_cannot_switch_to_another_approved_pair() -> None:
    class SpecGetterMutatingAdapter:
        def __init__(self, spec: SiteSpec) -> None:
            self._spec = spec
            self.channel = spec.channel

        @property
        def spec(self) -> SiteSpec:
            _overwrite_as_honor_jd(self._spec)
            return self._spec

        def execute(
            self,
            task: WebsiteTask,
            page: BrowserPage,
            capture: PlatformEvidenceCapture,
        ) -> WebsiteResult:
            raise AssertionError("unit test does not execute adapters")

    registry = AdapterRegistry(
        factories={WebsiteChannel.JD: SpecGetterMutatingAdapter},
    )

    with pytest.raises(ValueError, match="requested"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_adapter_channel_getter_cannot_switch_to_another_approved_pair() -> None:
    class ChannelGetterMutatingAdapter:
        def __init__(self, spec: SiteSpec) -> None:
            self.spec = spec

        @property
        def channel(self) -> WebsiteChannel:
            _overwrite_as_honor_jd(self.spec)
            return WebsiteChannel.JD

        def execute(
            self,
            task: WebsiteTask,
            page: BrowserPage,
            capture: PlatformEvidenceCapture,
        ) -> WebsiteResult:
            raise AssertionError("unit test does not execute adapters")

    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: cast(
                AdapterFactory,
                ChannelGetterMutatingAdapter,
            )
        },
    )

    with pytest.raises(ValueError, match="requested"):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_missing_lazy_target_module_is_adapter_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_target(module_name: str) -> ModuleType:
        raise ModuleNotFoundError(
            f"No module named {module_name!r}",
            name=module_name,
        )

    monkeypatch.setattr(registry_module, "import_module", missing_target)
    registry = AdapterRegistry()

    with pytest.raises(AdapterNotRegistered):
        registry.adapter_for("小米", WebsiteChannel.JD)


def test_missing_dependency_inside_lazy_module_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_error = ModuleNotFoundError(
        "No module named 'adapter_dependency'",
        name="adapter_dependency",
    )

    def missing_dependency(module_name: str) -> ModuleType:
        raise dependency_error

    monkeypatch.setattr(registry_module, "import_module", missing_dependency)
    registry = AdapterRegistry()

    with pytest.raises(ModuleNotFoundError) as caught:
        registry.adapter_for("小米", WebsiteChannel.JD)

    assert caught.value is dependency_error


def test_lazy_import_failure_is_not_cached_and_later_retry_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    adapter_module = ModuleType("quote_app.sites.jd")
    adapter_module.JDAdapter = FakeAdapter  # type: ignore[attr-defined]

    def flaky_import(module_name: str) -> ModuleType:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModuleNotFoundError(
                f"No module named {module_name!r}",
                name=module_name,
            )
        return adapter_module

    monkeypatch.setattr(registry_module, "import_module", flaky_import)
    registry = AdapterRegistry()

    with pytest.raises(AdapterNotRegistered):
        registry.adapter_for("小米", WebsiteChannel.JD)
    adapter = registry.adapter_for("小米", WebsiteChannel.JD)

    assert isinstance(adapter, FakeAdapter)
    assert adapter.spec is load_site_catalog()[0] or adapter.spec == load_site_catalog()[0]
    assert attempts == 2

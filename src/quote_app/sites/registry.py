from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Protocol, cast, runtime_checkable

from quote_app.sites.catalog import (
    SUPPORTED_BRANDS,
    SiteSpec,
    load_site_catalog,
)
from quote_app.sites.protocol import SiteAdapter
from quote_app.tasks.models import WebsiteChannel


@runtime_checkable
class RegisteredSiteAdapter(SiteAdapter, Protocol):
    """A site adapter observably bound to the exact approved catalog spec."""

    @property
    def spec(self) -> SiteSpec: ...


AdapterFactory = Callable[[SiteSpec], RegisteredSiteAdapter]

_DEFAULT_ADAPTER_CLASSES: Mapping[WebsiteChannel, tuple[str, str]] = {
    WebsiteChannel.JD: ("quote_app.sites.jd", "JDAdapter"),
    WebsiteChannel.TMALL: ("quote_app.sites.tmall", "TmallAdapter"),
    WebsiteChannel.OFFICIAL: (
        "quote_app.sites.official_brands.factory",
        "create_official_adapter",
    ),
}


class UnsupportedBrand(LookupError):
    def __init__(self, brand: object) -> None:
        self.brand = brand
        super().__init__(f"unsupported brand: {brand!r}")


class AdapterNotRegistered(LookupError):
    def __init__(self, brand: str, channel: WebsiteChannel) -> None:
        self.brand = brand
        self.channel = channel
        super().__init__(
            f"adapter is not registered for brand={brand!r}, channel={channel.value!r}"
        )


class AdapterRegistry:
    """Resolve approved catalog pairs to lazily constructed site adapters."""

    def __init__(
        self,
        *,
        factories: Mapping[WebsiteChannel, AdapterFactory] | None = None,
        catalog: tuple[SiteSpec, ...] | None = None,
        use_default_factories: bool | None = None,
    ) -> None:
        specs = catalog if catalog is not None else load_site_catalog()
        self._specs = _index_catalog(specs)
        self._factories = dict(factories or {})
        if not all(isinstance(channel, WebsiteChannel) for channel in self._factories):
            raise TypeError("factory keys must be WebsiteChannel values")
        if not all(callable(factory) for factory in self._factories.values()):
            raise TypeError("adapter factories must be callable")
        self._use_default_factories = (
            factories is None if use_default_factories is None else use_default_factories
        )

    def adapter_for(
        self,
        brand: str,
        channel: WebsiteChannel,
    ) -> SiteAdapter:
        if brand not in SUPPORTED_BRANDS:
            raise UnsupportedBrand(brand)
        if not isinstance(channel, WebsiteChannel):
            raise TypeError("channel must be a WebsiteChannel")

        spec = self._specs[(brand, channel)]
        _validate_requested_spec(spec, brand, channel)
        factory = self._factories.get(channel)
        if factory is None and self._use_default_factories:
            factory = _load_default_factory(brand, channel)
        if factory is None:
            raise AdapterNotRegistered(brand, channel)

        adapter = factory(spec)
        _validate_requested_spec(spec, brand, channel)
        if not isinstance(adapter, RegisteredSiteAdapter):
            raise TypeError("adapter factory must return a SiteAdapter bound to a SiteSpec")
        bound_spec = adapter.spec
        _validate_requested_spec(spec, brand, channel)
        if bound_spec is not spec:
            raise TypeError("adapter must bind the exact requested SiteSpec")
        adapter_channel = adapter.channel
        _validate_requested_spec(spec, brand, channel)
        if adapter_channel is not spec.channel:
            raise TypeError("adapter channel must match its SiteSpec")
        return adapter


_DEFAULT_REGISTRY: AdapterRegistry | None = None


def adapter_for(
    brand: str,
    channel: WebsiteChannel,
    *,
    registry: AdapterRegistry | None = None,
) -> SiteAdapter:
    """Return the adapter for an exact canonical brand/channel pair."""

    if brand not in SUPPORTED_BRANDS:
        raise UnsupportedBrand(brand)
    if not isinstance(channel, WebsiteChannel):
        raise TypeError("channel must be a WebsiteChannel")
    selected = registry if registry is not None else _default_registry()
    return selected.adapter_for(brand, channel)


def _default_registry() -> AdapterRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = AdapterRegistry()
    return _DEFAULT_REGISTRY


def _load_default_factory(
    brand: str,
    channel: WebsiteChannel,
) -> AdapterFactory:
    module_name, class_name = _DEFAULT_ADAPTER_CLASSES[channel]
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise AdapterNotRegistered(brand, channel) from exc
        raise

    factory = getattr(module, class_name, None)
    if factory is None or not callable(factory):
        raise AdapterNotRegistered(brand, channel)
    return cast(AdapterFactory, factory)


def _index_catalog(
    catalog: tuple[SiteSpec, ...],
) -> dict[tuple[str, WebsiteChannel], SiteSpec]:
    if not isinstance(catalog, tuple):
        raise TypeError("catalog must be a tuple")
    specs: dict[tuple[str, WebsiteChannel], SiteSpec] = {}
    for spec in catalog:
        if not isinstance(spec, SiteSpec):
            raise TypeError("catalog must contain SiteSpec values")
        spec.validate_approved()
        pair = (spec.brand, spec.channel)
        if pair in specs:
            raise ValueError("catalog contains a duplicate brand/channel pair")
        specs[pair] = spec
    expected = {(brand, channel) for brand in SUPPORTED_BRANDS for channel in WebsiteChannel}
    if set(specs) != expected:
        raise ValueError("catalog must contain all 21 approved pairs")
    return specs


def _validate_requested_spec(
    spec: SiteSpec,
    brand: str,
    channel: WebsiteChannel,
) -> None:
    spec.validate_approved()
    if spec.brand != brand or spec.channel is not channel:
        raise ValueError("SiteSpec must match the requested brand and channel")

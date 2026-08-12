from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any


def _configured_hidden_imports() -> tuple[str, ...]:
    spec_path = Path(__file__).parents[2] / "packaging" / "quotation_app.spec"
    captured: dict[str, Any] = {}

    class AnalysisStub:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            self.pure: tuple[object, ...] = ()
            self.scripts: tuple[object, ...] = ()
            self.binaries: tuple[object, ...] = ()
            self.datas: tuple[object, ...] = ()
            self.zipfiles: tuple[object, ...] = ()

    namespace: dict[str, Any] = {
        "SPECPATH": str(spec_path.parent),
        "Analysis": AnalysisStub,
        "PYZ": lambda *_args, **_kwargs: object(),
        "EXE": lambda *_args, **_kwargs: object(),
        "BUNDLE": lambda *_args, **_kwargs: object(),
        "COLLECT": lambda *_args, **_kwargs: object(),
    }
    exec(compile(spec_path.read_bytes(), str(spec_path), "exec"), namespace)
    return tuple(captured["hiddenimports"])


def test_packaging_includes_all_lazily_loaded_site_adapters() -> None:
    hidden_imports = set(_configured_hidden_imports())
    required_modules = {
        "quote_app.sites.jd",
        "quote_app.sites.tmall",
        "quote_app.sites.official",
        "quote_app.sites.official_brands.factory",
        "quote_app.sites.official_brands.base",
        "quote_app.sites.official_brands.models",
        "quote_app.sites.official_brands.xiaomi",
        "quote_app.sites.official_brands.oppo",
        "quote_app.sites.official_brands.vivo",
        "quote_app.sites.official_brands.huawei",
    }

    assert required_modules <= hidden_imports
    for module_name in required_modules:
        assert import_module(module_name).__name__ == module_name

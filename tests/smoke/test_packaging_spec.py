from __future__ import annotations

from pathlib import Path


def test_packaging_includes_all_lazily_loaded_site_adapters() -> None:
    spec_path = Path(__file__).parents[2] / "packaging" / "quotation_app.spec"
    contents = spec_path.read_text(encoding="utf-8")

    for module_name in (
        "quote_app.sites.jd",
        "quote_app.sites.tmall",
        "quote_app.sites.official",
    ):
        assert module_name in contents

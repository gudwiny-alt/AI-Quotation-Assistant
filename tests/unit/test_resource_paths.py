from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_default_quote_template_path_loads_from_a_pyinstaller_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the packaged app finishes web work but cannot publish Excel."""
    from quote_app.services.web_to_excel import default_template_path

    bundled_template = (
        tmp_path / "resources" / "templates" / "quote_template.xlsx"
    )
    bundled_template.parent.mkdir(parents=True)
    bundled_template.write_bytes(b"template")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert default_template_path() == bundled_template

"""Resolve bundled local resources in source and frozen application runs."""

from __future__ import annotations

import sys
from pathlib import Path


def bundled_resource_path(relative_path: str | Path) -> Path:
    """Return one project resource path, including from a PyInstaller bundle."""
    requested = Path(relative_path)
    if requested.is_absolute():
        raise ValueError("relative_path must be relative")
    bundle_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_root, str) and bundle_root:
        root = Path(bundle_root)
    else:
        root = Path(__file__).resolve().parents[2]
    return root / requested

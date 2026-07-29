"""Stable per-user locations for local application state."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "福建移动铺货报价助手"


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    browser_profile: Path
    task_database: Path
    evidence_dir: Path


def build_app_paths(
    system: str | None = None,
    *,
    home: Path | None = None,
) -> AppPaths:
    """Return paths that survive replacing the installed application files."""
    selected_system = system or platform.system()
    selected_home = (home or Path.home()).expanduser()
    if selected_system == "Windows":
        data_dir = selected_home / "AppData" / "Local" / APP_NAME
    elif selected_system == "Darwin":
        data_dir = selected_home / "Library" / "Application Support" / APP_NAME
    else:
        data_dir = selected_home / ".local" / "share" / APP_NAME
    return AppPaths(
        data_dir=data_dir,
        browser_profile=data_dir / "browser-profile",
        task_database=data_dir / "tasks.sqlite3",
        evidence_dir=data_dir / "evidence",
    )

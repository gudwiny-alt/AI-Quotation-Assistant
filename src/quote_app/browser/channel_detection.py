from __future__ import annotations

import os
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BrowserChoice:
    browser_name: str
    executable_path: Path
    searched_paths: tuple[Path, ...]


class BrowserNotFoundError(RuntimeError):
    def __init__(self, message: str, searched_paths: Sequence[Path] = ()) -> None:
        self.searched_paths = tuple(searched_paths)
        suffix = ""
        if self.searched_paths:
            suffix = "；已检查：" + "、".join(str(path) for path in self.searched_paths)
        super().__init__(message + suffix)


def detect_browser_choice(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
    candidates: Mapping[str, Sequence[Path]] | None = None,
) -> BrowserChoice:
    """Return the first installed browser, always preferring Chrome to Edge."""

    current_platform = platform_name or platform.system()
    if current_platform not in {"Darwin", "Windows"}:
        raise BrowserNotFoundError(f"unsupported platform: {current_platform}")

    candidate_map = (
        _default_candidates(
            current_platform,
            environ=os.environ if environ is None else environ,
            home_dir=Path.home() if home_dir is None else home_dir,
        )
        if candidates is None
        else candidates
    )
    browser_order = ("chrome",) if current_platform == "Darwin" else ("chrome", "edge")
    searched: list[Path] = []
    for browser_name in browser_order:
        for candidate in candidate_map.get(browser_name, ()):
            normalized = Path(candidate).expanduser().resolve()
            searched.append(normalized)
            if normalized.is_file():
                return BrowserChoice(
                    browser_name=browser_name,
                    executable_path=normalized,
                    searched_paths=tuple(searched),
                )

    raise BrowserNotFoundError("未找到受支持的本机 Chrome 或 Edge 浏览器", searched)


def _default_candidates(
    platform_name: str,
    *,
    environ: Mapping[str, str],
    home_dir: Path,
) -> dict[str, tuple[Path, ...]]:
    if platform_name == "Darwin":
        return {
            "chrome": (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                home_dir
                / "Applications"
                / "Google Chrome.app"
                / "Contents"
                / "MacOS"
                / "Google Chrome",
            )
        }

    local_app_data = _environment_path(environ, "LOCALAPPDATA")
    program_files = _environment_path(environ, "PROGRAMFILES")
    program_files_x86 = _environment_path(environ, "PROGRAMFILES(X86)")
    return {
        "chrome": _existing_environment_roots(
            (local_app_data, program_files, program_files_x86),
            Path("Google/Chrome/Application/chrome.exe"),
        ),
        "edge": _existing_environment_roots(
            (local_app_data, program_files, program_files_x86),
            Path("Microsoft/Edge/Application/msedge.exe"),
        ),
    }


def _environment_path(environ: Mapping[str, str], name: str) -> Path | None:
    value = environ.get(name)
    if not value:
        return None
    return Path(value)


def _existing_environment_roots(
    roots: Sequence[Path | None],
    suffix: Path,
) -> tuple[Path, ...]:
    return tuple(root / suffix for root in roots if root is not None)

"""Read-only preflight checks for local browser quotation runs."""

from __future__ import annotations

import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quote_app.browser.session import (
    BrowserProfileInUseError,
    ProfileValidationError,
    ensure_browser_profile_available,
)
from quote_app.evidence.macos_native import MacNativeBridge
from quote_app.paths import AppPaths

ScreenPermissionProvider = Callable[[], bool]
AccessibilityPermissionProvider = Callable[[], bool]
ProfileAvailableProvider = Callable[[Path], bool]


@dataclass(frozen=True, slots=True)
class ReadinessItem:
    key: str
    label: str
    ready: bool
    failure_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("readiness item key must be nonblank")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("readiness item label must be nonblank")
        if type(self.ready) is not bool:
            raise ValueError("readiness item ready must be boolean")
        if self.failure_message is not None and (
            not isinstance(self.failure_message, str)
            or not self.failure_message.strip()
        ):
            raise ValueError("readiness item failure message must be nonblank")
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "label", self.label.strip())
        if self.failure_message is not None:
            object.__setattr__(self, "failure_message", self.failure_message.strip())


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    items: tuple[ReadinessItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple | list) or not all(
            isinstance(item, ReadinessItem) for item in self.items
        ):
            raise ValueError("readiness items must be ReadinessItem values")
        object.__setattr__(self, "items", tuple(self.items))

    @property
    def ready(self) -> bool:
        return all(item.ready for item in self.items)

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(
            item.failure_message or item.label
            for item in self.items
            if not item.ready
        )


def check_runtime_readiness(
    app_paths: AppPaths,
    *,
    platform_name: str | None = None,
    screen_permission: ScreenPermissionProvider | None = None,
    accessibility_permission: AccessibilityPermissionProvider | None = None,
    profile_available: ProfileAvailableProvider | None = None,
) -> ReadinessCheck:
    """Check required local capabilities without launching or navigating Chrome."""
    if not isinstance(app_paths, AppPaths):
        raise TypeError("app_paths must be AppPaths")
    selected_platform = platform_name or platform.system()
    if not isinstance(selected_platform, str) or not selected_platform.strip():
        raise ValueError("platform_name must be nonblank")
    profile_check = profile_available or _profile_is_available
    profile_ready = _required_boolean(
        profile_check(app_paths.browser_profile),
        provider_name="profile_available",
    )
    items: list[ReadinessItem] = [
        ReadinessItem(
            "browser_profile",
            "程序专用 Chrome 未被占用",
            profile_ready,
            "程序专用 Chrome 正在运行，请先完全退出 Chrome",
        )
    ]
    if selected_platform == "Darwin":
        screen_check, accessibility_check = _mac_permission_providers(
            screen_permission,
            accessibility_permission,
        )
        items.extend(
            (
                ReadinessItem(
                    "screen_capture",
                    "屏幕与系统音频录制权限",
                    _required_boolean(
                        screen_check(),
                        provider_name="screen_permission",
                    ),
                    "未开启屏幕与系统音频录制权限",
                ),
                ReadinessItem(
                    "accessibility",
                    "辅助功能权限",
                    _required_boolean(
                        accessibility_check(),
                        provider_name="accessibility_permission",
                    ),
                    "未开启辅助功能权限",
                ),
            )
        )
    return ReadinessCheck(tuple(items))


def _mac_permission_providers(
    screen_permission: ScreenPermissionProvider | None,
    accessibility_permission: AccessibilityPermissionProvider | None,
) -> tuple[ScreenPermissionProvider, AccessibilityPermissionProvider]:
    if screen_permission is not None and not callable(screen_permission):
        raise TypeError("screen_permission must be callable")
    if accessibility_permission is not None and not callable(accessibility_permission):
        raise TypeError("accessibility_permission must be callable")
    if screen_permission is not None and accessibility_permission is not None:
        return screen_permission, accessibility_permission
    permissions = MacNativeBridge().permissions()
    return (
        screen_permission or (lambda: permissions.screen_recording),
        accessibility_permission or (lambda: permissions.accessibility),
    )


def _profile_is_available(profile_dir: Path) -> bool:
    try:
        ensure_browser_profile_available(profile_dir)
    except (BrowserProfileInUseError, ProfileValidationError):
        return False
    return True


def _required_boolean(value: object, *, provider_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{provider_name} must return a boolean")
    return value

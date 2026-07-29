#!/usr/bin/env python3
"""Read-only, fail-closed preflight for the macOS evidence runtime.

This script intentionally does not start a browser, request macOS permissions,
open System Settings, inspect windows, or capture screen pixels.
"""

from __future__ import annotations

import importlib.util
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Protocol

from quote_app.evidence.models import (
    MacCapturePolicy,
    accepts_capture_validation,
    validate_mac_capture_policy,
)


class _MacBridge(Protocol):
    def permissions(self) -> object: ...

    def primary_display_bounds(self) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimePreflight:
    screen_recording_permission: bool
    accessibility_permission: bool
    pyobjc_available: bool
    chrome_available: bool
    primary_display_available: bool
    status_code: str
    capture_acceptance_policy: MacCapturePolicy
    permitted_capture_validation_codes: tuple[str, ...]
    manual_visual_review_requirement: str


def collect_preflight(
    *,
    platform_name: str | None = None,
    mac_beta_visual_system_ui_review: bool = False,
) -> RuntimePreflight:
    """Return only capability booleans; every probe failure closes the gate."""

    selected_platform = sys.platform if platform_name is None else platform_name
    policy = _capture_acceptance_policy(
        platform_name=selected_platform,
        mac_beta_visual_system_ui_review=mac_beta_visual_system_ui_review,
    )
    if selected_platform != "darwin":
        return _result(
            screen_recording_permission=False,
            accessibility_permission=False,
            pyobjc_available=False,
            chrome_available=False,
            primary_display_available=False,
            status_code="PREFLIGHT_UNSUPPORTED_PLATFORM",
            policy=policy,
        )

    pyobjc_available = _pyobjc_frameworks_available()
    chrome_available = _chrome_available()
    if not pyobjc_available:
        return _result(
            screen_recording_permission=False,
            accessibility_permission=False,
            pyobjc_available=False,
            chrome_available=chrome_available,
            primary_display_available=False,
            status_code="PREFLIGHT_PYOBJC_UNAVAILABLE",
            policy=policy,
        )

    try:
        bridge = _mac_bridge()
        permissions = bridge.permissions()
        screen_recording = _strict_bool(
            getattr(permissions, "screen_recording", None)
        )
        accessibility = _strict_bool(
            getattr(permissions, "accessibility", None)
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _result(
            screen_recording_permission=False,
            accessibility_permission=False,
            pyobjc_available=True,
            chrome_available=chrome_available,
            primary_display_available=False,
            status_code="PREFLIGHT_NATIVE_RUNTIME_UNAVAILABLE",
            policy=policy,
        )

    try:
        bridge.primary_display_bounds()
        primary_display_available = True
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        primary_display_available = False

    return _result(
        screen_recording_permission=screen_recording,
        accessibility_permission=accessibility,
        pyobjc_available=True,
        chrome_available=chrome_available,
        primary_display_available=primary_display_available,
        status_code=_status_code(
            screen_recording=screen_recording,
            accessibility=accessibility,
            chrome_available=chrome_available,
            primary_display_available=primary_display_available,
        ),
        policy=policy,
    )


def _pyobjc_frameworks_available() -> bool:
    try:
        return all(
            importlib.util.find_spec(module_name) is not None
            for module_name in ("Quartz", "AppKit", "ApplicationServices")
        )
    except (ImportError, AttributeError, ValueError):
        return False


def _chrome_available() -> bool:
    try:
        from quote_app.browser.channel_detection import (
            BrowserNotFoundError,
            detect_browser_choice,
        )

        detect_browser_choice(platform_name="Darwin")
    except (BrowserNotFoundError, OSError, RuntimeError):
        return False
    return True


def _mac_bridge() -> _MacBridge:
    from quote_app.evidence.macos_native import MacNativeBridge

    return MacNativeBridge()


def _status_code(
    *,
    screen_recording: bool,
    accessibility: bool,
    chrome_available: bool,
    primary_display_available: bool,
) -> str:
    if not screen_recording:
        return "PREFLIGHT_SCREEN_RECORDING_DENIED"
    if not accessibility:
        return "PREFLIGHT_ACCESSIBILITY_DENIED"
    if not chrome_available:
        return "PREFLIGHT_CHROME_UNAVAILABLE"
    if not primary_display_available:
        return "PREFLIGHT_PRIMARY_DISPLAY_UNAVAILABLE"
    return "PREFLIGHT_READY"


def _strict_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid permission state")
    return value


def _result(
    *,
    screen_recording_permission: bool,
    accessibility_permission: bool,
    pyobjc_available: bool,
    chrome_available: bool,
    primary_display_available: bool,
    status_code: str,
    policy: MacCapturePolicy,
) -> RuntimePreflight:
    normalized_platform = "Darwin" if sys.platform == "darwin" else sys.platform
    return RuntimePreflight(
        screen_recording_permission=screen_recording_permission,
        accessibility_permission=accessibility_permission,
        pyobjc_available=pyobjc_available,
        chrome_available=chrome_available,
        primary_display_available=primary_display_available,
        status_code=status_code,
        capture_acceptance_policy=policy,
        permitted_capture_validation_codes=tuple(
            validation_code
            for validation_code in (
                "CAPTURE_OK",
                "CAPTURE_OK_MAC_VISUAL_REVIEW",
            )
            if accepts_capture_validation(
                validation_code,
                policy=policy,
                platform_name=normalized_platform,
            )
        ),
        manual_visual_review_requirement=(
            "confirm_chrome_target_page_menu_bar_datetime_and_dock"
            if policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
            else "not_required"
        ),
    )


def _capture_acceptance_policy(
    *,
    platform_name: str,
    mac_beta_visual_system_ui_review: bool,
) -> MacCapturePolicy:
    if not mac_beta_visual_system_ui_review:
        return MacCapturePolicy.STRICT
    if platform_name != "darwin":
        raise ValueError(
            "macOS visual-review beta policy is supported only on Darwin"
        )
    policy = MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
    validate_mac_capture_policy(policy, platform_name="Darwin")
    return policy


def _parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Read-only macOS formal-capture acceptance preflight."
    )
    parser.add_argument(
        "--mac-beta-visual-system-ui-review",
        action="store_true",
        help=(
            "Permit only the macOS manual visual-review beta capture code in "
            "this acceptance preflight record."
        ),
    )
    return parser


def main(
    arguments: list[str] | None = None,
    *,
    platform_name: str | None = None,
) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    options = _parser().parse_args(arguments)
    try:
        result = collect_preflight(
            platform_name=platform_name,
            mac_beta_visual_system_ui_review=(
                options.mac_beta_visual_system_ui_review
            ),
        )
    except ValueError as error:
        print("status_code=PREFLIGHT_MAC_VISUAL_REVIEW_UNSUPPORTED_PLATFORM")
        print(str(error), file=sys.stderr)
        return 2
    for key, value in (
        ("capture_acceptance_policy", result.capture_acceptance_policy.value),
        (
            "permitted_capture_validation_codes",
            ",".join(result.permitted_capture_validation_codes),
        ),
        (
            "manual_visual_review_requirement",
            result.manual_visual_review_requirement,
        ),
        ("screen_recording_permission", result.screen_recording_permission),
        ("accessibility_permission", result.accessibility_permission),
        ("pyobjc_available", result.pyobjc_available),
        ("chrome_available", result.chrome_available),
        ("primary_display_available", result.primary_display_available),
    ):
        rendered = str(value).lower() if type(value) is bool else str(value)
        print(f"{key}={rendered}")
    print(f"status_code={result.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "verify_macos_capture_runtime.py"
)


def test_preflight_script_emits_only_safe_stable_fields() -> None:
    """Catch a preflight regression that prints titles, URLs, or error details."""

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    fields = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line
    )
    assert set(fields) == {
        "status_code",
        "capture_acceptance_policy",
        "permitted_capture_validation_codes",
        "manual_visual_review_requirement",
        "screen_recording_permission",
        "accessibility_permission",
        "pyobjc_available",
        "chrome_available",
        "primary_display_available",
    }
    assert fields["status_code"] in {
        "PREFLIGHT_READY",
        "PREFLIGHT_UNSUPPORTED_PLATFORM",
        "PREFLIGHT_PYOBJC_UNAVAILABLE",
        "PREFLIGHT_NATIVE_RUNTIME_UNAVAILABLE",
        "PREFLIGHT_SCREEN_RECORDING_DENIED",
        "PREFLIGHT_ACCESSIBILITY_DENIED",
        "PREFLIGHT_CHROME_UNAVAILABLE",
        "PREFLIGHT_PRIMARY_DISPLAY_UNAVAILABLE",
    }
    for key in fields.keys() - {"status_code"}:
        if key in {
            "capture_acceptance_policy",
            "permitted_capture_validation_codes",
            "manual_visual_review_requirement",
        }:
            continue
        assert fields[key] in {"true", "false"}
    assert fields["capture_acceptance_policy"] == "strict"
    assert fields["permitted_capture_validation_codes"] == "CAPTURE_OK"
    assert fields["manual_visual_review_requirement"] == "not_required"


def test_preflight_fails_closed_on_a_non_macos_platform() -> None:
    """Catch accidental native probing when this shared script runs on Windows."""

    spec = importlib.util.spec_from_file_location(
        "macos_capture_preflight_test_module", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        result = module.collect_preflight(platform_name="win32")
    finally:
        if previous is None:
            del sys.modules[spec.name]
        else:
            sys.modules[spec.name] = previous

    assert result.status_code == "PREFLIGHT_UNSUPPORTED_PLATFORM"
    assert result.screen_recording_permission is False
    assert result.accessibility_permission is False
    assert result.pyobjc_available is False
    assert result.chrome_available is False
    assert result.primary_display_available is False


def test_visual_system_ui_review_switch_is_rejected_off_macos() -> None:
    """Catch accidental beta-policy activation from a Windows/Linux review run."""

    spec = importlib.util.spec_from_file_location(
        "macos_capture_preflight_visual_review_test_module", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        with pytest.raises(ValueError, match="only on Darwin"):
            module.collect_preflight(
                platform_name="win32",
                mac_beta_visual_system_ui_review=True,
            )
    finally:
        if previous is None:
            del sys.modules[spec.name]
        else:
            sys.modules[spec.name] = previous


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")
def test_macos_visual_review_switch_records_only_the_fixed_review_contract() -> None:
    """Catch a beta record that lacks its distinct code or leaks browser state."""

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--mac-beta-visual-system-ui-review",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    fields = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line
    )
    assert fields["capture_acceptance_policy"] == "mac_visual_review_beta"
    assert fields["permitted_capture_validation_codes"] == (
        "CAPTURE_OK,CAPTURE_OK_MAC_VISUAL_REVIEW"
    )
    assert fields["manual_visual_review_requirement"] == (
        "confirm_chrome_target_page_menu_bar_datetime_and_dock"
    )
    assert "http" not in result.stdout.lower()
    assert "cookie" not in result.stdout.lower()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")
def test_macos_optional_pyobjc_frameworks_have_an_import_smoke() -> None:
    """Catch a broken optional Mac capture installation before live acceptance."""

    assert importlib.util.find_spec("Quartz") is not None
    assert importlib.util.find_spec("AppKit") is not None
    assert importlib.util.find_spec("ApplicationServices") is not None

    from quote_app.evidence.macos_native import MacNativeBridge

    assert MacNativeBridge.__name__ == "MacNativeBridge"

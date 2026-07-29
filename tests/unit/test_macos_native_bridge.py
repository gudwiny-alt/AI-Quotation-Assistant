from __future__ import annotations

import builtins
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import quote_app.evidence.macos_native as macos_native
from quote_app.evidence.geometry import DisplayBounds
from quote_app.evidence.macos_native import (
    MacFrameworks,
    MacNativeBridge,
    MacPermissionState,
    NativeSystemUISample,
    NativeWebAreaSample,
    NativeWindowSample,
)
from quote_app.evidence.models import MacCapturePolicy
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    NonRetryableEvidenceCaptureError,
)


def _assert_code(error: pytest.ExceptionInfo[BaseException], code: str) -> None:
    assert isinstance(error.value, NonRetryableEvidenceCaptureError)
    assert error.value.code == code


@dataclass(frozen=True)
class _AXValue:
    kind: int
    value: object


class _AXElement:
    def __init__(
        self,
        *,
        pid: int,
        window_id: int | None = None,
        attrs: dict[str, object] | None = None,
    ) -> None:
        self.pid = pid
        self.window_id = window_id
        self.attrs = attrs or {}
        self.raise_attrs: set[str] = set()
        self.attribute_statuses: dict[str, int] = {}


class _PyObjCNativeInt(int):
    """Models a PyObjC NSNumber integer bridged from Core Graphics."""


class _RunningApplication:
    def __init__(
        self,
        pid: int,
        bundle_id: str,
        *,
        activate_result: object = True,
    ) -> None:
        self._pid = pid
        self._bundle_id = bundle_id
        self.activate_result = activate_result
        self.activation_options: list[int] = []

    def processIdentifier(self) -> int:
        return self._pid

    def bundleIdentifier(self) -> str:
        return self._bundle_id

    def activateWithOptions_(self, options: int) -> object:
        self.activation_options.append(options)
        return self.activate_result


class _FakeApplicationServices:
    kAXErrorSuccess = 0
    kAXErrorInvalidUIElement = -25202
    kAXErrorCannotComplete = -25204
    kAXErrorAttributeUnsupported = -25205
    kAXErrorAPIDisabled = -25211
    kAXErrorNoValue = -25212
    kAXFocusedWindowAttribute = "AXFocusedWindow"
    kAXWindowsAttribute = "AXWindows"
    kAXRoleAttribute = "AXRole"
    kAXSubroleAttribute = "AXSubrole"
    kAXChildrenAttribute = "AXChildren"
    kAXPositionAttribute = "AXPosition"
    kAXSizeAttribute = "AXSize"
    kAXHiddenAttribute = "AXHidden"
    kAXIdentifierAttribute = "AXIdentifier"
    kAXMenuBarAttribute = "AXMenuBar"
    kAXVisibleChildrenAttribute = "AXVisibleChildren"
    kAXRaiseAction = "AXRaise"
    kAXValueCGPointType = 1
    kAXValueCGSizeType = 2

    def __init__(self) -> None:
        self.trusted: object = True
        self.app_by_pid: dict[int, _AXElement] = {}
        self.private_status: object = 0
        self.private_value: object | None = None
        self.raise_status: object = 0
        self.ax_value_results: dict[int, object] = {}
        self.set_attribute_statuses: dict[str, object] = {}
        self.set_attribute_calls: list[
            tuple[_AXElement, str, _AXValue]
        ] = []

    def AXIsProcessTrusted(self) -> object:
        return self.trusted

    def AXUIElementCreateApplication(self, pid: int) -> _AXElement | None:
        return self.app_by_pid.get(pid)

    def AXUIElementCopyAttributeValue(
        self,
        element: _AXElement,
        attribute: str,
        _out: None,
    ) -> tuple[int, object | None]:
        if attribute in element.raise_attrs:
            raise RuntimeError("injected AX copy failure")
        if attribute in element.attribute_statuses:
            return (element.attribute_statuses[attribute], None)
        if attribute not in element.attrs:
            return (self.kAXErrorAttributeUnsupported, None)
        return (self.kAXErrorSuccess, element.attrs[attribute])

    def AXUIElementGetPid(
        self,
        element: _AXElement,
        _out: None,
    ) -> tuple[int, int]:
        return (self.kAXErrorSuccess, element.pid)

    def AXValueGetType(self, value: _AXValue) -> int:
        return value.kind

    def AXValueGetValue(
        self,
        value: _AXValue,
        expected_kind: int,
        _out: None,
    ) -> object:
        if value.kind != expected_kind:
            raise ValueError("wrong injected AX value kind")
        return self.ax_value_results.get(
            expected_kind,
            (True, value.value),
        )

    def CGPointMake(self, x: float, y: float) -> SimpleNamespace:
        return SimpleNamespace(x=x, y=y)

    def CGSizeMake(self, width: float, height: float) -> SimpleNamespace:
        return SimpleNamespace(width=width, height=height)

    def AXValueCreate(self, kind: int, value: object) -> _AXValue:
        return _AXValue(kind, value)

    def AXUIElementSetAttributeValue(
        self,
        element: _AXElement,
        attribute: str,
        value: _AXValue,
    ) -> object:
        self.set_attribute_calls.append((element, attribute, value))
        status = self.set_attribute_statuses.get(
            attribute,
            self.kAXErrorSuccess,
        )
        if status == self.kAXErrorSuccess:
            element.attrs[attribute] = value
        return status

    def AXUIElementPerformAction(
        self,
        _element: _AXElement,
        _action: str,
    ) -> object:
        return self.raise_status

    def _AXUIElementGetWindow(
        self,
        element: _AXElement,
        _out: None,
    ) -> tuple[object, object]:
        value = (
            self.private_value
            if self.private_value is not None
            else element.window_id
        )
        return (self.private_status, value)


class _FakeQuartz:
    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGWindowListOptionIncludingWindow = 8
    kCGNullWindowID = 0
    kCGWindowOwnerPID = "OwnerPID"
    kCGWindowNumber = "WindowNumber"
    kCGWindowBounds = "Bounds"
    kCGWindowLayer = "Layer"
    kCGWindowIsOnscreen = "OnScreen"

    def __init__(self, windows: list[dict[str, object]]) -> None:
        self.permission: object = True
        self.windows = windows
        self.primary_bounds: object = SimpleNamespace(
            origin=SimpleNamespace(x=0, y=0),
            size=SimpleNamespace(width=1440, height=900),
        )
        self.window_error = False

    def CGPreflightScreenCaptureAccess(self) -> object:
        return self.permission

    def CGWindowListCopyWindowInfo(
        self,
        options: int,
        relative_window: int,
    ) -> list[dict[str, object]]:
        if self.window_error:
            raise RuntimeError("injected CG failure")
        if options == self.kCGWindowListOptionIncludingWindow:
            return [
                window
                for window in self.windows
                if window[self.kCGWindowNumber] == relative_window
            ]
        return list(self.windows)

    def CGMainDisplayID(self) -> int:
        return 1

    def CGDisplayBounds(self, _display_id: int) -> object:
        return self.primary_bounds


class _FakeAppKit:
    NSApplicationActivateIgnoringOtherApps = 2

    def __init__(
        self,
        frontmost: _RunningApplication,
        running: dict[str, list[_RunningApplication]],
    ) -> None:
        self.backing_scale = 1.0
        self.NSScreen = SimpleNamespace(
            mainScreen=lambda: SimpleNamespace(
                backingScaleFactor=lambda: self.backing_scale,
            ),
        )
        workspace = SimpleNamespace(frontmostApplication=lambda: frontmost)
        self.NSWorkspace = SimpleNamespace(
            sharedWorkspace=lambda: workspace,
        )
        all_apps = [frontmost]
        all_apps.extend(
            application
            for applications in running.values()
            for application in applications
        )
        by_pid = {application.processIdentifier(): application for application in all_apps}
        self.NSRunningApplication = SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=(
                lambda bundle_id: list(running.get(bundle_id, []))
            ),
            runningApplicationWithProcessIdentifier_=by_pid.get,
        )


def _bounds(
    x: int,
    y: int,
    width: int,
    height: int,
) -> dict[str, int]:
    return {"X": x, "Y": y, "Width": width, "Height": height}


def _window(
    *,
    pid: object = 42,
    window_id: object = 101,
    bounds: object | None = None,
    layer: object = 0,
    on_screen: object = True,
) -> dict[str, object]:
    return {
        "OwnerPID": pid,
        "WindowNumber": window_id,
        "Bounds": bounds if bounds is not None else _bounds(10, 20, 800, 600),
        "Layer": layer,
        "OnScreen": on_screen,
    }


def _ax_bounds(
    element: _AXElement,
    x: object,
    y: object,
    width: object,
    height: object,
) -> None:
    element.attrs["AXPosition"] = _AXValue(
        1,
        SimpleNamespace(x=x, y=y),
    )
    element.attrs["AXSize"] = _AXValue(
        2,
        SimpleNamespace(width=width, height=height),
    )


@dataclass
class _Runtime:
    bridge: MacNativeBridge
    application_services: _FakeApplicationServices
    quartz: _FakeQuartz
    appkit: _FakeAppKit
    browser_window: _AXElement
    web_area: _AXElement
    menu_bar: _AXElement
    clock: _AXElement
    dock: _AXElement
    control_center_app: _RunningApplication
    dock_app: _RunningApplication


def _runtime() -> _Runtime:
    application_services = _FakeApplicationServices()
    browser_window = _AXElement(pid=42, window_id=101)
    web_area = _AXElement(
        pid=42,
        attrs={
            "AXRole": "AXWebArea",
            "AXHidden": False,
            "AXChildren": [],
        },
    )
    _ax_bounds(web_area, 20, 70, 760, 530)
    browser_window.attrs["AXChildren"] = [web_area]
    browser_app_ax = _AXElement(
        pid=42,
        attrs={
            "AXFocusedWindow": browser_window,
            "AXWindows": [browser_window],
        },
    )

    clock = _AXElement(
        pid=500,
        attrs={
            "AXRole": "AXMenuBarItem",
            "AXIdentifier": "com.apple.controlcenter.clock",
            "AXHidden": False,
            "AXChildren": [],
        },
    )
    _ax_bounds(clock, 1300, 0, 100, 24)
    menu_bar = _AXElement(
        pid=500,
        attrs={
            "AXRole": "AXMenuBar",
            "AXHidden": False,
            "AXChildren": [clock],
        },
    )
    _ax_bounds(menu_bar, 0, 0, 1440, 24)
    control_center_ax = _AXElement(
        pid=500,
        attrs={"AXMenuBar": menu_bar, "AXChildren": [menu_bar]},
    )

    dock = _AXElement(
        pid=600,
        attrs={
            "AXRole": "AXList",
            "AXSubrole": "AXContentList",
            "AXHidden": False,
            "AXChildren": [],
        },
    )
    _ax_bounds(dock, 0, 850, 1440, 50)
    dock_ax = _AXElement(pid=600, attrs={"AXChildren": [dock]})

    application_services.app_by_pid = {
        42: browser_app_ax,
        500: control_center_ax,
        600: dock_ax,
    }
    browser_app = _RunningApplication(42, "com.example.browser")
    control_center_app = _RunningApplication(500, "com.apple.controlcenter")
    dock_app = _RunningApplication(600, "com.apple.dock")
    running = {
        "com.apple.controlcenter": [control_center_app],
        "com.apple.systemuiserver": [],
        "com.apple.dock": [dock_app],
    }
    appkit = _FakeAppKit(browser_app, running)
    quartz = _FakeQuartz([_window()])
    frameworks = MacFrameworks(
        AppKit=appkit,
        ApplicationServices=application_services,
        Quartz=quartz,
    )
    bridge = MacNativeBridge(
        framework_loader=lambda: frameworks,
        platform_provider=lambda: "darwin",
        macos_major_version_provider=lambda: 26,
        sample_id_provider=lambda: "sample-1",
        monotonic_clock=lambda: 123.25,
        sleeper=lambda _seconds: None,
    )
    return _Runtime(
        bridge=bridge,
        application_services=application_services,
        quartz=quartz,
        appkit=appkit,
        browser_window=browser_window,
        web_area=web_area,
        menu_bar=menu_bar,
        clock=clock,
        dock=dock,
        control_center_app=control_center_app,
        dock_app=dock_app,
    )


def _expected_identity() -> BrowserWindowIdentity:
    return BrowserWindowIdentity("macos", 42, "101")


def test_module_import_does_not_import_pyobjc_frameworks() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ | {"PYTHONPATH": str(source_root)}
    program = """
import builtins

blocked = ("Quartz", "AppKit", "ApplicationServices", "objc")
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.startswith(blocked):
        raise ImportError(f"eager native import: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import quote_app.evidence.macos_native
"""

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=source_root.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("platform_name", "major_version"),
    [("win32", 26), ("linux", 26), ("darwin", 25), ("darwin", 27)],
)
def test_platform_and_version_guard_fail_before_framework_loading(
    platform_name: str,
    major_version: int,
) -> None:
    loads: list[str] = []
    bridge = MacNativeBridge(
        framework_loader=lambda: loads.append("loaded"),
        platform_provider=lambda: platform_name,
        macos_major_version_provider=lambda: major_version,
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        bridge.permissions()

    _assert_code(captured, "CAPTURE_ENVIRONMENT")
    assert loads == []


@pytest.mark.parametrize(
    ("screen_recording", "accessibility"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_permissions_return_only_exact_preflight_state(
    screen_recording: bool,
    accessibility: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.quartz.permission = screen_recording
    runtime.application_services.trusted = accessibility
    opened: list[object] = []
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    result = runtime.bridge.permissions()

    assert result == MacPermissionState(screen_recording, accessibility)
    assert opened == []


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_permission_state_rejects_non_boolean_values(value: object) -> None:
    with pytest.raises(ValueError, match="permission flags must be booleans"):
        MacPermissionState(value, True)  # type: ignore[arg-type]


@pytest.mark.parametrize("provider", ["screen", "accessibility"])
def test_permission_provider_exceptions_are_stable_environment_errors(
    provider: str,
) -> None:
    runtime = _runtime()
    if provider == "screen":
        runtime.quartz.CGPreflightScreenCaptureAccess = (  # type: ignore[method-assign]
            lambda: (_ for _ in ()).throw(RuntimeError("private path"))
        )
    else:
        runtime.application_services.AXIsProcessTrusted = (  # type: ignore[method-assign]
            lambda: (_ for _ in ()).throw(RuntimeError("private path"))
        )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.permissions()

    _assert_code(captured, "CAPTURE_ENVIRONMENT")
    assert "private path" not in str(captured.value)


def test_windows_convert_only_current_cg_window_metadata() -> None:
    runtime = _runtime()

    result = runtime.bridge.windows()

    assert result == (
        NativeWindowSample(
            process_id=42,
            window_id=101,
            bounds_px=DisplayBounds(10, 20, 800, 600),
            layer=0,
            on_screen=True,
            minimized=False,
            sample_id="sample-1",
            sampled_at_monotonic=123.25,
        ),
    )
    assert not hasattr(result[0], "title")
    assert not hasattr(result[0], "owner_name")


def test_window_bounds_layout_convergence_reads_only_bound_identity() -> None:
    runtime = _runtime()
    runtime.quartz.windows = [
        _window(pid=99, window_id=202),
        _window(bounds=_bounds(0, 24, 1440, 826)),
    ]

    result = runtime.bridge.window_bounds(_expected_identity())

    assert result == NativeWindowSample(
        process_id=42,
        window_id=101,
        bounds_px=DisplayBounds(0, 24, 1440, 826),
        layer=0,
        on_screen=True,
        minimized=False,
        sample_id="sample-1",
        sampled_at_monotonic=123.25,
    )


@pytest.mark.parametrize(
    "windows",
    [
        [_window(pid=99)],
        [_window(window_id=202)],
        [_window(on_screen=False)],
        [_window(bounds=_bounds(0, 24, 0, 826))],
    ],
)
def test_window_bounds_layout_convergence_rejects_invalid_bound_sample(
    windows: list[dict[str, object]],
) -> None:
    runtime = _runtime()
    runtime.quartz.windows = windows

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.window_bounds(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_window_bounds_layout_convergence_maps_api_failure_to_system_ui() -> None:
    runtime = _runtime()
    runtime.quartz.window_error = True

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.window_bounds(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_cg_windows_accept_pyobjc_native_integer_subclasses() -> None:
    runtime = _runtime()
    runtime.quartz.windows = [
        _window(
            pid=_PyObjCNativeInt(42),
            window_id=_PyObjCNativeInt(101),
            layer=_PyObjCNativeInt(0),
        )
    ]

    result = runtime.bridge.windows()

    assert result[0].process_id == 42
    assert type(result[0].process_id) is int
    assert result[0].window_id == 101
    assert type(result[0].window_id) is int
    assert result[0].layer == 0
    assert type(result[0].layer) is int


def test_positive_integer_guard_normalizes_pyobjc_native_integer() -> None:
    value = _PyObjCNativeInt(42)

    result = macos_native._positive_int(value)

    assert result == 42
    assert type(result) is int


def test_strict_integer_guard_normalizes_pyobjc_native_integer() -> None:
    value = _PyObjCNativeInt(0)

    result = macos_native._strict_int(value, minimum=0)

    assert result == 0
    assert type(result) is int


@pytest.mark.parametrize("value", [True, 0, -1, 1.0, "1", None])
def test_positive_integer_guard_rejects_nonpositive_or_noninteger_values(
    value: object,
) -> None:
    with pytest.raises(ValueError):
        macos_native._positive_int(value)


@pytest.mark.parametrize(
    ("value", "minimum"),
    [
        (True, 0),
        (-1, 0),
        (0, 1),
        (1.0, 0),
        ("1", 0),
        (None, 0),
    ],
)
def test_strict_integer_guard_rejects_out_of_range_or_noninteger_values(
    value: object,
    minimum: int,
) -> None:
    with pytest.raises(ValueError):
        macos_native._strict_int(value, minimum=minimum)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OwnerPID", None),
        ("OwnerPID", True),
        ("OwnerPID", 0),
        ("OwnerPID", 42.0),
        ("WindowNumber", None),
        ("WindowNumber", True),
        ("WindowNumber", -1),
        ("WindowNumber", 101.0),
        ("Bounds", None),
        ("Bounds", _bounds(0, 0, 0, 100)),
        ("Bounds", {"X": 0, "Y": 0, "Width": 100}),
        ("Layer", None),
        ("Layer", True),
        ("Layer", -1),
        ("Layer", 0.0),
        ("OnScreen", 1),
        ("OnScreen", False),
    ],
)
def test_windows_reject_invalid_or_nonvisible_metadata(
    key: str,
    value: object,
) -> None:
    runtime = _runtime()
    runtime.quartz.windows = [_window() | {key: value}]

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.windows()

    _assert_code(captured, "CAPTURE_WINDOW_IDENTITY")


def test_core_graphics_window_exception_is_stable_environment_error() -> None:
    runtime = _runtime()
    runtime.quartz.window_error = True

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.windows()

    _assert_code(captured, "CAPTURE_ENVIRONMENT")


def test_focused_window_binds_frontmost_ax_private_id_and_unique_cg_window() -> None:
    runtime = _runtime()

    result = runtime.bridge.focused_window()

    assert result.process_id == 42
    assert result.window_id == 101
    assert result.sample_id == "sample-1"


@pytest.mark.parametrize(
    ("status", "window_id"),
    [(1, 101), (0, True), (0, 0), (0, -1), (0, "101")],
)
def test_private_window_id_status_and_value_are_strict(
    status: object,
    window_id: object,
) -> None:
    runtime = _runtime()
    runtime.application_services.private_status = status
    runtime.application_services.private_value = window_id

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.focused_window()

    _assert_code(captured, "CAPTURE_WINDOW_IDENTITY")


def test_missing_private_window_symbol_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    monkeypatch.delattr(
        _FakeApplicationServices,
        "_AXUIElementGetWindow",
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.focused_window()

    _assert_code(captured, "CAPTURE_WINDOW_IDENTITY")


@pytest.mark.parametrize("mismatch", ["frontmost_pid", "ax_pid", "cg_pid", "duplicate"])
def test_focused_window_rejects_every_pid_id_or_uniqueness_mismatch(
    mismatch: str,
) -> None:
    runtime = _runtime()
    if mismatch == "frontmost_pid":
        frontmost = (
            runtime.appkit.NSRunningApplication
            .runningApplicationWithProcessIdentifier_(42)
        )
        frontmost._pid = 99
    elif mismatch == "ax_pid":
        runtime.browser_window.pid = 99
    elif mismatch == "cg_pid":
        runtime.quartz.windows = [_window(pid=99)]
    else:
        runtime.quartz.windows.append(_window())

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.focused_window()

    _assert_code(captured, "CAPTURE_WINDOW_IDENTITY")


def test_activate_window_raises_exact_ax_window_and_rechecks_focus() -> None:
    runtime = _runtime()

    result = runtime.bridge.activate_window(_expected_identity())

    assert result is None
    frontmost = runtime.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(42)
    assert frontmost.activation_options == [2]


def test_activate_window_waits_for_delayed_frontmost_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    frameworks = runtime.bridge._frameworks()
    expected_sample, expected_element = runtime.bridge._focused_window(
        frameworks
    )
    calls = 0
    sleeps: list[float] = []

    def delayed_focused_window(_frameworks: MacFrameworks) -> tuple[NativeWindowSample, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return replace(expected_sample, process_id=99), expected_element
        return expected_sample, expected_element

    monkeypatch.setattr(
        runtime.bridge,
        "_focused_window",
        delayed_focused_window,
    )
    runtime.bridge._sleeper = sleeps.append

    runtime.bridge.activate_window(_expected_identity())

    assert sleeps == [0.1]


def test_activate_window_rejects_stale_identity_before_activation() -> None:
    runtime = _runtime()

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.activate_window(
            BrowserWindowIdentity("macos", 42, "999")
        )

    _assert_code(captured, "CAPTURE_WINDOW_IDENTITY")


def test_available_window_frame_accepts_inset_top_menu_and_bottom_dock() -> None:
    assert macos_native._available_window_frame(
        menu_bar=DisplayBounds(0, 3, 1920, 24),
        dock=DisplayBounds(38, 1002, 1844, 68),
        display=DisplayBounds(0, 0, 1920, 1080),
    ) == DisplayBounds(0, 27, 1920, 975)


@pytest.mark.parametrize(
    ("dock", "expected"),
    [
        (
            DisplayBounds(10, 100, 50, 700),
            DisplayBounds(60, 27, 1380, 873),
        ),
        (
            DisplayBounds(1380, 100, 50, 700),
            DisplayBounds(0, 27, 1380, 873),
        ),
    ],
)
def test_available_window_frame_accepts_inset_side_dock(
    dock: DisplayBounds,
    expected: DisplayBounds,
) -> None:
    assert macos_native._available_window_frame(
        menu_bar=DisplayBounds(0, 3, 1440, 24),
        dock=dock,
        display=DisplayBounds(0, 0, 1440, 900),
    ) == expected


@pytest.mark.parametrize(
    ("menu_bar", "dock"),
    [
        (
            DisplayBounds(0, 3, 1440, 24),
            DisplayBounds(10, 20, 50, 700),
        ),
        (
            DisplayBounds(0, 0, 50, 900),
            DisplayBounds(50, 0, 1390, 900),
        ),
        (
            DisplayBounds(0, -1, 1440, 24),
            DisplayBounds(38, 822, 1364, 68),
        ),
        (
            DisplayBounds(0, 3, 1440, 24),
            DisplayBounds(38, 822, 1403, 68),
        ),
        (
            DisplayBounds(0, 3, 1440, 24),
            DisplayBounds(100, 100, 200, 50),
        ),
        (
            DisplayBounds(0, 3, 1440, 24),
            DisplayBounds(0, 800, 100, 100),
        ),
    ],
)
def test_available_window_frame_rejects_inconsistent_system_ui(
    menu_bar: DisplayBounds,
    dock: DisplayBounds,
) -> None:
    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        macos_native._available_window_frame(
            menu_bar=menu_bar,
            dock=dock,
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_prepare_safe_capture_window_sets_only_bound_focused_window_frame() -> None:
    runtime = _runtime()

    runtime.bridge.prepare_safe_capture_window(
        _expected_identity(),
        menu_bar=DisplayBounds(0, 0, 1440, 24),
        dock=DisplayBounds(200, 850, 1040, 50),
        display=DisplayBounds(0, 0, 1440, 900),
    )

    position = runtime.browser_window.attrs["AXPosition"]
    size = runtime.browser_window.attrs["AXSize"]
    assert position == _AXValue(1, SimpleNamespace(x=0.0, y=24.0))
    assert size == _AXValue(2, SimpleNamespace(width=1440.0, height=826.0))
    assert {
        id(element)
        for element, _attribute, _value
        in runtime.application_services.set_attribute_calls
    } == {id(runtime.browser_window)}


def test_prepare_safe_capture_window_beta_sets_only_bound_focused_window_to_48px_inset_frame() -> None:
    runtime = _runtime()

    runtime.bridge.prepare_safe_capture_window(
        _expected_identity(),
        menu_bar=DisplayBounds(0, 0, 1440, 24),
        dock=DisplayBounds(200, 850, 1040, 50),
        display=DisplayBounds(0, 0, 1440, 900),
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert runtime.browser_window.attrs["AXPosition"] == _AXValue(
        1,
        SimpleNamespace(x=48.0, y=72.0),
    )
    assert runtime.browser_window.attrs["AXSize"] == _AXValue(
        2,
        SimpleNamespace(width=1344.0, height=730.0),
    )
    assert {
        id(element)
        for element, _attribute, _value
        in runtime.application_services.set_attribute_calls
    } == {id(runtime.browser_window)}


def test_prepare_safe_capture_window_beta_rejects_nonpositive_fixed_frame_before_ax_writes() -> None:
    runtime = _runtime()

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=DisplayBounds(0, 0, 96, 24),
            dock=DisplayBounds(0, 168, 96, 32),
            display=DisplayBounds(0, 0, 96, 200),
            policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.application_services.set_attribute_calls == []


def test_prepare_safe_capture_window_rejects_wrong_focused_window() -> None:
    runtime = _runtime()
    other_window = _AXElement(pid=42, window_id=202)
    browser_app = runtime.application_services.app_by_pid[42]
    browser_app.attrs["AXFocusedWindow"] = other_window
    browser_app.attrs["AXWindows"] = [runtime.browser_window, other_window]
    runtime.quartz.windows.append(_window(window_id=202))

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=DisplayBounds(0, 0, 1440, 24),
            dock=DisplayBounds(200, 850, 1040, 50),
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.application_services.set_attribute_calls == []


@pytest.mark.parametrize(
    ("menu_bar", "dock"),
    [
        (
            DisplayBounds(0, -1, 1440, 24),
            DisplayBounds(200, 850, 1040, 50),
        ),
        (
            DisplayBounds(0, 0, 1440, 24),
            DisplayBounds(100, 100, 200, 50),
        ),
        (
            DisplayBounds(0, 0, 1440, 24),
            DisplayBounds(0, 20, 1440, 50),
        ),
    ],
)
def test_prepare_safe_capture_window_rejects_invalid_system_ui_bounds(
    menu_bar: DisplayBounds,
    dock: DisplayBounds,
) -> None:
    runtime = _runtime()

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=menu_bar,
            dock=dock,
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.application_services.set_attribute_calls == []


def test_prepare_safe_capture_window_rejects_frame_setting_failure() -> None:
    runtime = _runtime()
    runtime.application_services.set_attribute_statuses["AXPosition"] = (
        runtime.application_services.kAXErrorCannotComplete
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=DisplayBounds(0, 0, 1440, 24),
            dock=DisplayBounds(200, 850, 1040, 50),
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert "AXPosition" not in runtime.browser_window.attrs


def test_prepare_safe_capture_window_rejects_nonpositive_available_frame() -> None:
    runtime = _runtime()

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=DisplayBounds(0, 0, 100, 50),
            dock=DisplayBounds(25, 50, 50, 50),
            display=DisplayBounds(0, 0, 100, 100),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.application_services.set_attribute_calls == []


@pytest.mark.parametrize(
    ("dock", "position", "size"),
    [
        (
            DisplayBounds(0, 200, 100, 100),
            SimpleNamespace(x=100.0, y=24.0),
            SimpleNamespace(width=1340.0, height=876.0),
        ),
        (
            DisplayBounds(1340, 200, 100, 100),
            SimpleNamespace(x=0.0, y=24.0),
            SimpleNamespace(width=1340.0, height=876.0),
        ),
    ],
)
def test_prepare_safe_capture_window_accepts_square_side_dock(
    dock: DisplayBounds,
    position: SimpleNamespace,
    size: SimpleNamespace,
) -> None:
    runtime = _runtime()

    runtime.bridge.prepare_safe_capture_window(
        _expected_identity(),
        menu_bar=DisplayBounds(0, 0, 1440, 24),
        dock=dock,
        display=DisplayBounds(0, 0, 1440, 900),
    )

    assert runtime.browser_window.attrs["AXPosition"] == _AXValue(1, position)
    assert runtime.browser_window.attrs["AXSize"] == _AXValue(2, size)


@pytest.mark.parametrize(
    "dock",
    [
        DisplayBounds(100, 100, 100, 100),
        DisplayBounds(0, 800, 100, 100),
    ],
)
def test_prepare_safe_capture_window_rejects_ambiguous_dock_edge(
    dock: DisplayBounds,
) -> None:
    runtime = _runtime()

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=DisplayBounds(0, 0, 1440, 24),
            dock=dock,
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.application_services.set_attribute_calls == []


def test_prepare_safe_capture_window_rejects_partial_frame_setting() -> None:
    runtime = _runtime()
    runtime.application_services.set_attribute_statuses["AXSize"] = (
        runtime.application_services.kAXErrorCannotComplete
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            _expected_identity(),
            menu_bar=DisplayBounds(0, 0, 1440, 24),
            dock=DisplayBounds(200, 850, 1040, 50),
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.browser_window.attrs["AXPosition"] == _AXValue(
        1,
        SimpleNamespace(x=0.0, y=24.0),
    )
    assert "AXSize" not in runtime.browser_window.attrs


def test_prepare_safe_capture_window_maps_invalid_identity_to_system_ui() -> None:
    runtime = _runtime()

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.prepare_safe_capture_window(
            BrowserWindowIdentity("win32", 42, "101"),
            menu_bar=DisplayBounds(0, 0, 1440, 24),
            dock=DisplayBounds(200, 850, 1040, 50),
            display=DisplayBounds(0, 0, 1440, 900),
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")
    assert runtime.application_services.set_attribute_calls == []


def test_web_area_returns_one_visible_area_inside_exact_bound_window() -> None:
    runtime = _runtime()

    result = runtime.bridge.web_area(_expected_identity())

    assert result == NativeWebAreaSample(
        window_id=101,
        bounds_dip=DisplayBounds(20, 70, 760, 530),
        sample_id="sample-1",
        sampled_at_monotonic=123.25,
    )


def test_web_area_ignores_more_than_the_ax_limit_of_web_content_children() -> None:
    runtime = _runtime()
    runtime.web_area.attrs["AXChildren"] = [
        _AXElement(pid=42, attrs={"AXChildren": []})
        for _ in range(513)
    ]

    assert runtime.bridge.web_area(_expected_identity()).window_id == 101


@pytest.mark.parametrize("case", ["missing", "zero", "multiple", "outside", "hidden"])
def test_web_area_fails_closed_when_not_unique_visible_and_contained(
    case: str,
) -> None:
    runtime = _runtime()
    if case == "missing":
        runtime.browser_window.attrs["AXChildren"] = []
    elif case == "zero":
        _ax_bounds(runtime.web_area, 20, 70, 0, 530)
    elif case == "multiple":
        duplicate = _AXElement(
            pid=42,
            attrs={
                "AXRole": "AXWebArea",
                "AXHidden": False,
                "AXChildren": [],
            },
        )
        _ax_bounds(duplicate, 30, 80, 700, 500)
        runtime.browser_window.attrs["AXChildren"] = [
            runtime.web_area,
            duplicate,
        ]
    elif case == "outside":
        _ax_bounds(runtime.web_area, 20, 70, 900, 530)
    else:
        runtime.web_area.attrs["AXHidden"] = True

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")


def test_ax_tree_exception_is_stable_accessibility_error() -> None:
    runtime = _runtime()
    runtime.browser_window.raise_attrs.add("AXChildren")

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")


@pytest.mark.parametrize(
    "result",
    [
        (
            False,
            SimpleNamespace(x=20, y=70),
        ),
        (True,),
        (
            1,
            SimpleNamespace(x=20, y=70),
        ),
        (
            True,
            SimpleNamespace(left=20, top=70),
        ),
    ],
)
def test_ax_value_result_requires_exact_success_tuple_and_struct(
    result: object,
) -> None:
    runtime = _runtime()
    runtime.application_services.ax_value_results[1] = result

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")


def test_ax_value_type_must_match_the_requested_geometry_type() -> None:
    runtime = _runtime()
    runtime.web_area.attrs["AXPosition"] = _AXValue(
        2,
        SimpleNamespace(x=20, y=70),
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="real AXValue contract is Darwin-only",
)
def test_real_pyobjc_ax_value_contract_decodes_without_ax_permission() -> None:
    application_services = pytest.importorskip("ApplicationServices")
    from quote_app.evidence import macos_native

    raw_value = application_services.AXValueCreate(
        application_services.kAXValueCGPointType,
        application_services.CGPointMake(12.0, 34.0),
    )

    decoded = macos_native._decode_ax_value(
        application_services,
        raw_value,
        application_services.kAXValueCGPointType,
        code="CAPTURE_ACCESSIBILITY",
    )

    assert decoded.x == 12.0
    assert decoded.y == 34.0


@pytest.mark.parametrize(
    "status_name",
    ["kAXErrorAttributeUnsupported", "kAXErrorNoValue"],
)
def test_optional_ax_attribute_allows_only_documented_absence_statuses(
    status_name: str,
) -> None:
    runtime = _runtime()
    unrelated = _AXElement(pid=42, attrs={"AXChildren": []})
    unrelated.attribute_statuses["AXRole"] = getattr(
        runtime.application_services,
        status_name,
    )
    runtime.browser_window.attrs["AXChildren"] = [
        unrelated,
        runtime.web_area,
    ]

    assert runtime.bridge.web_area(_expected_identity()).window_id == 101


@pytest.mark.parametrize(
    "status_name",
    [
        "kAXErrorCannotComplete",
        "kAXErrorAPIDisabled",
        "kAXErrorInvalidUIElement",
    ],
)
def test_optional_ax_attribute_rejects_operational_ax_errors(
    status_name: str,
) -> None:
    runtime = _runtime()
    unrelated = _AXElement(pid=42, attrs={"AXChildren": []})
    unrelated.attribute_statuses["AXRole"] = getattr(
        runtime.application_services,
        status_name,
    )
    runtime.browser_window.attrs["AXChildren"] = [
        unrelated,
        runtime.web_area,
    ]

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")


def test_web_area_records_timeout_after_readiness_window() -> None:
    runtime = _runtime()
    runtime.browser_window.attrs["AXChildren"] = []

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_TIMEOUT"


def test_web_area_returns_when_candidate_becomes_ready_on_fifteenth_sample() -> None:
    runtime = _runtime()
    runtime.browser_window.attrs["AXChildren"] = []
    sleeps: list[float] = []

    def make_candidate_ready_after_fourteen_waits(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 14:
            runtime.browser_window.attrs["AXChildren"] = [runtime.web_area]

    runtime.bridge._sleeper = make_candidate_ready_after_fourteen_waits  # type: ignore[method-assign]

    assert runtime.bridge.web_area(_expected_identity()).window_id == 101
    assert sleeps == [0.1] * 14


def test_web_area_rejects_candidate_when_sampling_crosses_deadline() -> None:
    runtime = _runtime()
    clock_values = iter((0.0, 0.0, 0.0, 0.1, 2.1, 2.1))
    runtime.bridge._monotonic_clock = lambda: next(clock_values)  # type: ignore[method-assign]

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_TIMEOUT"


def test_web_area_checks_deadline_before_reading_next_candidate() -> None:
    runtime = _runtime()
    runtime.browser_window.attrs["AXChildren"] = []
    clock_values = iter((0.0, 0.0, 0.0, 2.1))
    children_reads = 0
    original_copy = runtime.application_services.AXUIElementCopyAttributeValue

    def counted_copy(
        element: _AXElement,
        attribute: str,
        output: None,
    ) -> tuple[int, object | None]:
        nonlocal children_reads
        if (
            element is runtime.browser_window
            and attribute == "AXChildren"
        ):
            children_reads += 1
        return original_copy(element, attribute, output)

    runtime.bridge._monotonic_clock = lambda: next(clock_values)  # type: ignore[method-assign]
    runtime.application_services.AXUIElementCopyAttributeValue = counted_copy

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_TIMEOUT"
    assert children_reads == 0


def test_web_area_samples_twenty_times_with_nineteen_bounded_waits() -> None:
    runtime = _runtime()
    runtime.browser_window.attrs["AXChildren"] = []
    sleeps: list[float] = []
    children_reads = 0
    original_copy = runtime.application_services.AXUIElementCopyAttributeValue

    def counted_copy(
        element: _AXElement,
        attribute: str,
        output: None,
    ) -> tuple[int, object | None]:
        nonlocal children_reads
        if (
            element is runtime.browser_window
            and attribute == "AXChildren"
        ):
            children_reads += 1
        return original_copy(element, attribute, output)

    runtime.application_services.AXUIElementCopyAttributeValue = counted_copy
    runtime.bridge._sleeper = sleeps.append  # type: ignore[method-assign]

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_TIMEOUT"
    assert children_reads == 20
    assert sleeps == [0.1] * 19


def test_web_area_records_not_unique_diagnostic_after_bounded_wait() -> None:
    runtime = _runtime()
    duplicate = _AXElement(
        pid=42,
        attrs={
            "AXRole": "AXWebArea",
            "AXHidden": False,
            "AXChildren": [],
        },
    )
    _ax_bounds(duplicate, 30, 80, 700, 500)
    runtime.browser_window.attrs["AXChildren"] = [
        runtime.web_area,
        duplicate,
    ]

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_NOT_UNIQUE"


def test_web_area_maps_bound_identity_failure_to_safe_diagnostic() -> None:
    runtime = _runtime()
    runtime.browser_window.window_id = 102

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_IDENTITY_MISMATCH"


def test_web_area_rejects_unique_cross_pid_candidate() -> None:
    runtime = _runtime()
    runtime.web_area.pid = 700

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_IDENTITY_MISMATCH"


def test_web_area_maps_ax_operation_failure_to_safe_diagnostic() -> None:
    runtime = _runtime()
    runtime.browser_window.raise_attrs.add("AXChildren")

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_NATIVE_ERROR"


def test_web_area_records_timeout_for_noncredible_unique_candidate() -> None:
    runtime = _runtime()
    runtime.web_area.attrs["AXHidden"] = True

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.web_area(_expected_identity())

    _assert_code(captured, "CAPTURE_ACCESSIBILITY")
    assert captured.value.message == "WEB_AREA_TIMEOUT"


def test_primary_display_bounds_are_strictly_validated() -> None:
    runtime = _runtime()

    assert runtime.bridge.primary_display_bounds() == DisplayBounds(
        0,
        0,
        1440,
        900,
    )

    runtime.quartz.primary_bounds = SimpleNamespace(
        origin=SimpleNamespace(x=0, y=0),
        size=SimpleNamespace(width=0, height=900),
    )
    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.primary_display_bounds()
    _assert_code(captured, "CAPTURE_ENVIRONMENT")


def test_primary_display_bounds_use_main_screen_backing_scale() -> None:
    runtime = _runtime()
    runtime.appkit.backing_scale = 2.0

    assert runtime.bridge.primary_display_bounds() == DisplayBounds(
        0,
        0,
        2880,
        1800,
    )


def test_system_ui_returns_only_validated_current_bounds() -> None:
    runtime = _runtime()

    result = runtime.bridge.system_ui(_expected_identity())

    assert result == NativeSystemUISample(
        menu_bar_bounds_px=DisplayBounds(0, 0, 1440, 24),
        date_time_bounds_px=DisplayBounds(1300, 0, 100, 24),
        dock_bounds_px=DisplayBounds(0, 850, 1440, 50),
        sample_id="sample-1",
        sampled_at_monotonic=123.25,
    )
    assert not hasattr(result, "raw_element")
    assert not hasattr(result, "title")


def test_system_ui_conservatively_expands_fractional_ax_bounds() -> None:
    runtime = _runtime()
    _ax_bounds(runtime.menu_bar, 0.25, 0.5, 1439.5, 23.25)
    _ax_bounds(runtime.dock, 0.5, 849.25, 1439.25, 50.5)

    result = runtime.bridge.system_ui(_expected_identity())

    assert result.menu_bar_bounds_px == DisplayBounds(0, 0, 1440, 24)
    assert result.dock_bounds_px == DisplayBounds(0, 849, 1440, 51)


@pytest.mark.parametrize(
    ("x", "y", "width", "height"),
    [
        (float("nan"), 0, 1440, 24),
        (0, float("inf"), 1440, 24),
        (0, 0, 0, 24),
        (0, 0, -1, 24),
        (0, 0, 1440, -1),
    ],
)
def test_system_ui_rejects_invalid_fractional_ax_bounds(
    x: object,
    y: object,
    width: object,
    height: object,
) -> None:
    runtime = _runtime()
    _ax_bounds(runtime.menu_bar, x, y, width, height)

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_system_ui_strict_rejects_unavailable_clock_identity() -> None:
    runtime = _runtime()
    runtime.menu_bar.attrs["AXChildren"] = []

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_system_ui_beta_marks_only_unavailable_clock_identity_for_review() -> None:
    runtime = _runtime()
    runtime.menu_bar.attrs["AXChildren"] = []

    result = runtime.bridge.system_ui(
        _expected_identity(),
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert result == NativeSystemUISample(
        menu_bar_bounds_px=DisplayBounds(0, 0, 1440, 24),
        date_time_bounds_px=None,
        dock_bounds_px=DisplayBounds(0, 850, 1440, 50),
        sample_id="sample-1",
        sampled_at_monotonic=123.25,
        visual_review_required=True,
    )


def test_system_ui_beta_accepts_macos_26_dock_list_without_subrole_when_visible() -> None:
    runtime = _runtime()
    runtime.menu_bar.attrs["AXChildren"] = []
    runtime.dock.attrs.pop("AXSubrole")
    runtime.dock.attrs.pop("AXHidden")
    dock_item = _AXElement(
        pid=600,
        attrs={
            "AXRole": "AXDockItem",
            "AXSubrole": "AXApplicationDockItem",
            "AXChildren": [],
        },
    )
    runtime.dock.attrs["AXChildren"] = [dock_item]
    runtime.dock.attrs["AXVisibleChildren"] = [dock_item]

    result = runtime.bridge.system_ui(
        _expected_identity(),
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert result == NativeSystemUISample(
        menu_bar_bounds_px=DisplayBounds(0, 0, 1440, 24),
        date_time_bounds_px=None,
        dock_bounds_px=DisplayBounds(0, 850, 1440, 50),
        sample_id="sample-1",
        sampled_at_monotonic=123.25,
        visual_review_required=True,
    )


def test_system_ui_rejects_nested_dock_list_without_subrole() -> None:
    runtime = _runtime()
    runtime.dock.attrs.pop("AXSubrole")
    runtime.dock.attrs.pop("AXHidden")
    dock_item = _AXElement(
        pid=600,
        attrs={
            "AXRole": "AXDockItem",
            "AXSubrole": "AXApplicationDockItem",
            "AXChildren": [],
        },
    )
    runtime.dock.attrs["AXChildren"] = [dock_item]
    runtime.dock.attrs["AXVisibleChildren"] = [dock_item]
    nested_group = _AXElement(
        pid=600,
        attrs={
            "AXRole": "AXGroup",
            "AXChildren": [runtime.dock],
        },
    )
    runtime.application_services.app_by_pid[600].attrs["AXChildren"] = [
        nested_group
    ]

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


@pytest.mark.parametrize("failed_component", ["menu", "dock", "foreground"])
def test_system_ui_beta_keeps_non_clock_identity_failures_closed(
    failed_component: str,
) -> None:
    runtime = _runtime()
    runtime.menu_bar.attrs["AXChildren"] = []
    if failed_component == "menu":
        runtime.menu_bar.attrs["AXHidden"] = True
    elif failed_component == "dock":
        runtime.dock.attrs["AXHidden"] = True
    else:
        runtime.browser_window.window_id = 999

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(
            _expected_identity(),
            policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_system_ui_beta_does_not_downgrade_other_system_ui_errors() -> None:
    runtime = _runtime()
    runtime.menu_bar.attrs["AXChildren"] = []
    runtime.menu_bar.raise_attrs.add("AXChildren")

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(
            _expected_identity(),
            policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        )

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_native_producers_use_injected_monotonic_clock() -> None:
    runtime = _runtime()

    assert runtime.bridge.windows()[0].sampled_at_monotonic == 123.25
    assert runtime.bridge.focused_window().sampled_at_monotonic == 123.25
    assert (
        runtime.bridge.web_area(_expected_identity()).sampled_at_monotonic
        == 123.25
    )
    assert (
        runtime.bridge.system_ui(_expected_identity()).sampled_at_monotonic
        == 123.25
    )


@pytest.mark.parametrize(
    "case",
    [
        "menu_missing",
        "menu_hidden",
        "menu_zero",
        "menu_offscreen",
        "clock_missing",
        "clock_hidden",
        "clock_zero",
        "clock_offscreen",
        "clock_owner_mismatch",
        "clock_identifier_mismatch",
        "clock_role_mismatch",
        "clock_duplicate",
        "dock_missing",
        "dock_hidden",
        "dock_zero",
        "dock_offscreen",
        "dock_owner_mismatch",
        "dock_role_mismatch",
    ],
)
def test_system_ui_requires_exact_visible_unique_current_ax_proof(
    case: str,
) -> None:
    runtime = _runtime()
    system_ax = runtime.application_services.app_by_pid[500]
    dock_ax = runtime.application_services.app_by_pid[600]
    if case == "menu_missing":
        system_ax.attrs.pop("AXMenuBar")
    elif case == "menu_hidden":
        runtime.menu_bar.attrs["AXHidden"] = True
    elif case == "menu_zero":
        _ax_bounds(runtime.menu_bar, 0, 0, 0, 24)
    elif case == "menu_offscreen":
        _ax_bounds(runtime.menu_bar, 0, -30, 1440, 24)
    elif case == "clock_missing":
        runtime.menu_bar.attrs["AXChildren"] = []
    elif case == "clock_hidden":
        runtime.clock.attrs["AXHidden"] = True
    elif case == "clock_zero":
        _ax_bounds(runtime.clock, 1300, 0, 0, 24)
    elif case == "clock_offscreen":
        _ax_bounds(runtime.clock, 1500, 0, 100, 24)
    elif case == "clock_owner_mismatch":
        runtime.control_center_app._bundle_id = "com.example.fake-clock"
    elif case == "clock_identifier_mismatch":
        runtime.clock.attrs["AXIdentifier"] = "com.example.clock"
    elif case == "clock_role_mismatch":
        runtime.clock.attrs["AXRole"] = "AXButton"
    elif case == "clock_duplicate":
        duplicate = _AXElement(
            pid=500,
            attrs=dict(runtime.clock.attrs),
        )
        runtime.menu_bar.attrs["AXChildren"] = [runtime.clock, duplicate]
    elif case == "dock_missing":
        dock_ax.attrs["AXChildren"] = []
    elif case == "dock_hidden":
        runtime.dock.attrs["AXHidden"] = True
    elif case == "dock_zero":
        _ax_bounds(runtime.dock, 0, 850, 1440, 0)
    elif case == "dock_offscreen":
        _ax_bounds(runtime.dock, 0, 950, 1440, 50)
    elif case == "dock_owner_mismatch":
        runtime.dock_app._bundle_id = "com.example.fake-dock"
    else:
        runtime.dock.attrs["AXSubrole"] = "AXUnknown"

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_system_ui_rejects_duplicate_current_menu_bars() -> None:
    runtime = _runtime()
    second_menu = _AXElement(
        pid=501,
        attrs=dict(runtime.menu_bar.attrs),
    )
    second_owner_ax = _AXElement(
        pid=501,
        attrs={"AXMenuBar": second_menu, "AXChildren": [second_menu]},
    )
    runtime.application_services.app_by_pid[501] = second_owner_ax
    second_owner = _RunningApplication(501, "com.apple.systemuiserver")
    runtime.appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_ = (
        lambda bundle_id: (
            [runtime.control_center_app]
            if bundle_id == "com.apple.controlcenter"
            else [second_owner]
            if bundle_id == "com.apple.systemuiserver"
            else [runtime.dock_app]
            if bundle_id == "com.apple.dock"
            else []
        )
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


def test_system_ui_ax_exception_is_stable_and_does_not_fall_back() -> None:
    runtime = _runtime()
    runtime.menu_bar.raise_attrs.add("AXChildren")

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")


@pytest.mark.parametrize("element_name", ["menu_bar", "clock", "dock"])
def test_system_ui_rejects_element_pid_mismatch(
    element_name: str,
) -> None:
    runtime = _runtime()
    getattr(runtime, element_name).pid = 999

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.bridge.system_ui(_expected_identity())

    _assert_code(captured, "CAPTURE_SYSTEM_UI")

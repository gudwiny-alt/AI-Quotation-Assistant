from __future__ import annotations

import ctypes
import math
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from quote_app.evidence.geometry import DisplayBounds, GeometryError
from quote_app.evidence.models import EvidenceRecord
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureEnvironmentUnavailable,
    CaptureGeometrySnapshot,
    CaptureRequest,
    EvidenceCapturePipeline,
    SystemUIProof,
    capture_display_with_mss,
)

GeometrySnapshotProvider = Callable[
    [BrowserWindowIdentity],
    CaptureGeometrySnapshot,
]

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_SW_MAXIMIZE = 3
_ABM_GETSTATE = 4
_ABS_AUTOHIDE = 1


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _AppBarData(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uCallbackMessage", ctypes.c_uint),
        ("uEdge", ctypes.c_uint),
        ("rc", _Rect),
        ("lParam", ctypes.c_ssize_t),
    ]


class WindowsCaptureEnvironment:
    """Windows adapter that refuses physical metrics without verified DPI mode."""

    def __init__(
        self,
        *,
        geometry_snapshot_provider: GeometrySnapshotProvider | None = None,
        prepare_callback: Callable[[BrowserWindowIdentity], None] | None = None,
        dpi_awareness_verifier: Callable[[], bool] | None = None,
    ) -> None:
        self._geometry_snapshot_provider = geometry_snapshot_provider
        self._prepare_callback = prepare_callback
        self._dpi_awareness_verifier = (
            dpi_awareness_verifier or is_per_monitor_dpi_aware
        )
        self._expected: BrowserWindowIdentity | None = None

    def screen_capture_permission(self) -> bool:
        return True

    def prepare_browser(self, expected: BrowserWindowIdentity) -> None:
        self._require_dpi_awareness()
        self._expected = expected
        if self._prepare_callback is not None:
            try:
                result = self._prepare_callback(expected)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise RuntimeError(
                    "browser preparation callback failed"
                ) from None
            if result is not None:
                raise CaptureEnvironmentUnavailable(
                    "browser preparation callback returned invalid data"
                )
            return
        handle = _parse_handle(expected.window_handle)
        user32 = _configured_user32()
        if not bool(user32.IsWindow(handle)):
            raise RuntimeError("managed browser window no longer exists")
        user32.ShowWindow(handle, _SW_MAXIMIZE)
        if not bool(user32.SetForegroundWindow(handle)):
            raise RuntimeError("cannot foreground managed browser window")

    def geometry_snapshot(
        self,
        expected: BrowserWindowIdentity,
    ) -> CaptureGeometrySnapshot:
        self._require_dpi_awareness()
        if self._geometry_snapshot_provider is None:
            raise CaptureEnvironmentUnavailable(
                "authoritative capture geometry snapshot is required"
            )
        try:
            snapshot = self._geometry_snapshot_provider(expected)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CaptureEnvironmentUnavailable(
                "capture geometry snapshot provider failed"
            ) from None
        if not isinstance(snapshot, CaptureGeometrySnapshot):
            raise CaptureEnvironmentUnavailable(
                "capture geometry snapshot provider returned invalid data"
            )
        handle = _parse_handle(expected.window_handle)
        try:
            user32 = _configured_user32()
            maximized = bool(user32.IsZoomed(handle))
            native_dpi = int(user32.GetDpiForWindow(handle))
        except (AttributeError, OSError) as error:
            raise CaptureEnvironmentUnavailable(
                "cannot prove browser maximized state or native DPI"
            ) from error
        if not maximized or not snapshot.maximized:
            raise CaptureEnvironmentUnavailable(
                "browser is not authoritatively maximized"
            )
        _validate_windows_snapshot_dpi(snapshot, native_dpi=native_dpi)
        return snapshot

    def system_ui_proof(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof:
        self._require_dpi_awareness()
        return _windows_system_ui_proof(snapshot)

    def foreground_window(self) -> BrowserWindowIdentity:
        self._require_dpi_awareness()
        user32 = _configured_user32()
        handle = int(user32.GetForegroundWindow())
        if not handle:
            raise RuntimeError("no Windows foreground window")
        process_id = ctypes.c_ulong()
        if not user32.GetWindowThreadProcessId(
            handle,
            ctypes.byref(process_id),
        ):
            raise RuntimeError("cannot read foreground window process")
        return BrowserWindowIdentity(
            platform="windows",
            process_id=int(process_id.value),
            window_handle=hex(handle),
        )

    def capture_primary_display(
        self,
        destination: Path,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        self._require_dpi_awareness()
        capture_display_with_mss(
            destination,
            snapshot.display_physical_bounds,
        )

    def _require_dpi_awareness(self) -> None:
        try:
            verified = self._dpi_awareness_verifier()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CaptureEnvironmentUnavailable(
                "Windows DPI awareness verification failed"
            ) from None
        if type(verified) is not bool or not verified:
            raise CaptureEnvironmentUnavailable(
                "per-monitor DPI awareness v2 is not verified"
            )


class WindowsEvidenceCapture:
    def __init__(
        self,
        environment: WindowsCaptureEnvironment | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if environment is None:
            startup_dpi_verified = ensure_per_monitor_dpi_awareness()
            environment = WindowsCaptureEnvironment(
                dpi_awareness_verifier=lambda: startup_dpi_verified
            )
        self._pipeline = EvidenceCapturePipeline(
            environment,
            sleeper=sleeper or time.sleep,
            now=now,
        )

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        return self._pipeline.capture(request)


def ensure_per_monitor_dpi_awareness(user32: Any | None = None) -> bool:
    """Set PMv2 at process startup and verify the effective thread context."""
    api = _configured_user32(user32)
    try:
        api.SetProcessDpiAwarenessContext(
            _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except (AttributeError, OSError):
        return False
    return is_per_monitor_dpi_aware(api)


def is_per_monitor_dpi_aware(user32: Any | None = None) -> bool:
    """Return True only when the current thread context is exactly PMv2."""
    try:
        api = _configured_user32(user32)
        context = api.GetThreadDpiAwarenessContext()
        if not context:
            return False
        return bool(
            api.AreDpiAwarenessContextsEqual(
                context,
                _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
            )
        )
    except (AttributeError, OSError, TypeError):
        return False


def _windows_system_ui_proof(
    snapshot: CaptureGeometrySnapshot,
    *,
    user32: Any | None = None,
    shell32: Any | None = None,
) -> SystemUIProof:
    api = _configured_user32(user32)
    shell = _configured_shell32(shell32)
    taskbar = api.FindWindowW("Shell_TrayWnd", None)
    if not taskbar or not bool(api.IsWindowVisible(taskbar)):
        raise CaptureEnvironmentUnavailable(
            "Windows taskbar is not visibly available"
        )

    appbar_data = _AppBarData()
    appbar_data.cbSize = ctypes.sizeof(_AppBarData)
    appbar_data.hWnd = taskbar
    state = int(shell.SHAppBarMessage(_ABM_GETSTATE, ctypes.byref(appbar_data)))
    if state & _ABS_AUTOHIDE:
        raise CaptureEnvironmentUnavailable("Windows taskbar auto-hide is on")

    rectangle = _Rect()
    if not bool(api.GetWindowRect(taskbar, ctypes.byref(rectangle))):
        raise CaptureEnvironmentUnavailable("cannot read Windows taskbar bounds")
    taskbar_bounds = DisplayBounds(
        int(rectangle.left),
        int(rectangle.top),
        int(rectangle.right - rectangle.left),
        int(rectangle.bottom - rectangle.top),
    )
    intersects = _bounds_intersect(
        taskbar_bounds,
        snapshot.display_physical_bounds,
    )
    if not intersects:
        raise CaptureEnvironmentUnavailable(
            "Windows taskbar does not intersect the primary display"
        )
    if _bounds_intersect(
        taskbar_bounds,
        snapshot.browser_physical_bounds,
    ):
        raise CaptureEnvironmentUnavailable(
            "browser overlaps the visible Windows taskbar"
        )

    tray = api.FindWindowExW(taskbar, 0, "TrayNotifyWnd", None)
    clock = api.FindWindowExW(tray, 0, "TrayClockWClass", None) if tray else 0
    clock_visible = bool(clock and api.IsWindowVisible(clock))
    if not clock_visible:
        raise CaptureEnvironmentUnavailable(
            "Windows taskbar clock control is not visibly available"
        )
    return SystemUIProof(
        expected_window=snapshot.expected_window,
        system_bar_visible=True,
        date_time_visible=True,
        intersects_primary_display=True,
        authoritative=True,
        source="windows-shell32-appbar-and-clock",
    )


def _bounds_intersect(first: DisplayBounds, second: DisplayBounds) -> bool:
    return (
        first.x < second.x + second.width
        and first.x + first.width > second.x
        and first.y < second.y + second.height
        and first.y + first.height > second.y
    )


def _parse_handle(value: str) -> int:
    try:
        handle = int(value, 0)
    except ValueError as error:
        raise RuntimeError("invalid native browser window handle") from error
    if handle <= 0:
        raise RuntimeError("invalid native browser window handle")
    return handle


def _configured_user32(user32: Any | None = None) -> Any:
    api = user32 or _user32()
    signatures: tuple[tuple[str, list[Any], Any], ...] = (
        ("IsWindow", [ctypes.c_void_p], ctypes.c_bool),
        ("ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_bool),
        (
            "SetForegroundWindow",
            [ctypes.c_void_p],
            ctypes.c_bool,
        ),
        ("IsZoomed", [ctypes.c_void_p], ctypes.c_bool),
        ("GetDpiForWindow", [ctypes.c_void_p], ctypes.c_uint),
        ("FindWindowW", [ctypes.c_wchar_p, ctypes.c_wchar_p], ctypes.c_void_p),
        (
            "FindWindowExW",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
            ],
            ctypes.c_void_p,
        ),
        ("IsWindowVisible", [ctypes.c_void_p], ctypes.c_bool),
        ("GetWindowRect", [ctypes.c_void_p, ctypes.POINTER(_Rect)], ctypes.c_bool),
        ("GetForegroundWindow", [], ctypes.c_void_p),
        (
            "GetWindowThreadProcessId",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)],
            ctypes.c_ulong,
        ),
        (
            "SetProcessDpiAwarenessContext",
            [ctypes.c_void_p],
            ctypes.c_bool,
        ),
        ("GetThreadDpiAwarenessContext", [], ctypes.c_void_p),
        (
            "GetAwarenessFromDpiAwarenessContext",
            [ctypes.c_void_p],
            ctypes.c_int,
        ),
        (
            "AreDpiAwarenessContextsEqual",
            [ctypes.c_void_p, ctypes.c_void_p],
            ctypes.c_bool,
        ),
    )
    for name, argtypes, restype in signatures:
        function = getattr(api, name, None)
        if function is not None:
            function.argtypes = argtypes
            function.restype = restype
    return api


def _configured_shell32(shell32: Any | None = None) -> Any:
    api = shell32 or _shell32()
    function = getattr(api, "SHAppBarMessage")
    function.argtypes = [ctypes.c_uint, ctypes.POINTER(_AppBarData)]
    function.restype = ctypes.c_size_t
    return api


def _user32() -> Any:
    loader = getattr(ctypes, "windll", None)
    if loader is None:
        raise CaptureEnvironmentUnavailable("Windows user32 APIs unavailable")
    return loader.user32


def _shell32() -> Any:
    loader = getattr(ctypes, "windll", None)
    if loader is None:
        raise CaptureEnvironmentUnavailable("Windows shell32 APIs unavailable")
    return loader.shell32


def _validate_windows_snapshot_dpi(
    snapshot: CaptureGeometrySnapshot,
    *,
    native_dpi: int,
) -> None:
    if (
        snapshot.device_pixel_ratio is None
        or snapshot.dpi_x is None
        or snapshot.dpi_y is None
    ):
        raise CaptureEnvironmentUnavailable(
            "Windows geometry snapshot must include DPR and x/y DPI"
        )
    if type(native_dpi) is not int or native_dpi <= 0:
        raise CaptureEnvironmentUnavailable(
            "GetDpiForWindow did not return a valid native DPI"
        )
    expected_scale = native_dpi / 96.0
    values = (
        snapshot.dpi_x / 96.0,
        snapshot.dpi_y / 96.0,
        snapshot.viewport_geometry.scale_x,
        snapshot.viewport_geometry.scale_y,
        snapshot.device_pixel_ratio,
    )
    if any(
        not math.isclose(
            value,
            expected_scale,
            rel_tol=0.03,
            abs_tol=0.03,
        )
        for value in values
    ):
        raise GeometryError(
            "native DPI, snapshot DPI, viewport scale, and JavaScript DPR disagree"
        )

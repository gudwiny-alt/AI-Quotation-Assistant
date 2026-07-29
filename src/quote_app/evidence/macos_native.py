from __future__ import annotations

import math
import platform
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from quote_app.evidence.geometry import DisplayBounds
from quote_app.evidence.models import MacCapturePolicy
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    EvidenceCaptureError,
    make_capture_error,
)

_SUPPORTED_MACOS_MAJOR = 26
_SYSTEM_UI_BUNDLE_IDS = (
    "com.apple.controlcenter",
    "com.apple.systemuiserver",
)
_CLOCK_IDENTIFIERS = frozenset(
    {
        "com.apple.controlcenter.clock",
        "com.apple.menuextra.clock",
    }
)
_CLOCK_ROLES = frozenset({"AXMenuBarItem", "AXMenuExtra"})
_DOCK_BUNDLE_ID = "com.apple.dock"
_DOCK_CONTAINER_ROLES = frozenset(
    {
        ("AXList", "AXContentList"),
        ("AXList", None),
    }
)
_MAX_AX_TREE_ELEMENTS = 512
_WEB_AREA_MAX_ATTEMPTS = 20
_WEB_AREA_POLL_SECONDS = 0.1
_WEB_AREA_TIMEOUT_SECONDS = 2.0
_WEB_AREA_CLOCK_EPSILON = 1e-9
_FOREGROUND_FOCUS_MAX_ATTEMPTS = 20
_FOREGROUND_FOCUS_POLL_SECONDS = 0.1
_FIXED_CAPTURE_FRAME_INSET_PX = 48

_WEB_AREA_NOT_FOUND = "WEB_AREA_NOT_FOUND"
_WEB_AREA_NOT_UNIQUE = "WEB_AREA_NOT_UNIQUE"
_WEB_AREA_IDENTITY_MISMATCH = "WEB_AREA_IDENTITY_MISMATCH"
_WEB_AREA_TIMEOUT = "WEB_AREA_TIMEOUT"
_WEB_AREA_NATIVE_ERROR = "WEB_AREA_NATIVE_ERROR"


class _ClockIdentityUnavailable(Exception):
    """The verified menu bar has no uniquely bound current clock element."""

    def __init__(self, menu_bar_bounds_px: DisplayBounds) -> None:
        self.menu_bar_bounds_px = menu_bar_bounds_px


@dataclass(frozen=True, slots=True)
class MacFrameworks:
    AppKit: Any
    ApplicationServices: Any
    Quartz: Any


@dataclass(frozen=True, slots=True)
class MacPermissionState:
    screen_recording: bool
    accessibility: bool

    def __post_init__(self) -> None:
        if (
            type(self.screen_recording) is not bool
            or type(self.accessibility) is not bool
        ):
            raise ValueError("permission flags must be booleans")


@dataclass(frozen=True, slots=True)
class NativeWindowSample:
    process_id: int
    window_id: int
    bounds_px: DisplayBounds
    layer: int
    on_screen: bool
    minimized: bool
    sample_id: str
    sampled_at_monotonic: float = 0.0

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 0:
            raise ValueError("process_id must be a positive integer")
        if type(self.window_id) is not int or self.window_id <= 0:
            raise ValueError("window_id must be a positive integer")
        if not isinstance(self.bounds_px, DisplayBounds):
            raise ValueError("bounds_px must be DisplayBounds")
        if type(self.layer) is not int or self.layer < 0:
            raise ValueError("layer must be a non-negative integer")
        if type(self.on_screen) is not bool or type(self.minimized) is not bool:
            raise ValueError("window visibility flags must be booleans")
        _validate_sample_id(self.sample_id)
        _validate_sampled_at_monotonic(self.sampled_at_monotonic)


@dataclass(frozen=True, slots=True)
class NativeWebAreaSample:
    window_id: int
    bounds_dip: DisplayBounds
    sample_id: str
    sampled_at_monotonic: float = 0.0

    def __post_init__(self) -> None:
        if type(self.window_id) is not int or self.window_id <= 0:
            raise ValueError("window_id must be a positive integer")
        if not isinstance(self.bounds_dip, DisplayBounds):
            raise ValueError("bounds_dip must be DisplayBounds")
        _validate_sample_id(self.sample_id)
        _validate_sampled_at_monotonic(self.sampled_at_monotonic)


@dataclass(frozen=True, slots=True)
class NativeSystemUISample:
    menu_bar_bounds_px: DisplayBounds
    date_time_bounds_px: DisplayBounds | None
    dock_bounds_px: DisplayBounds
    sample_id: str
    sampled_at_monotonic: float = 0.0
    visual_review_required: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("menu_bar_bounds_px", self.menu_bar_bounds_px),
            ("dock_bounds_px", self.dock_bounds_px),
        ):
            if not isinstance(value, DisplayBounds):
                raise ValueError(f"{name} must be DisplayBounds")
        if self.date_time_bounds_px is not None and not isinstance(
            self.date_time_bounds_px,
            DisplayBounds,
        ):
            raise ValueError("date_time_bounds_px must be DisplayBounds or None")
        if type(self.visual_review_required) is not bool:
            raise ValueError("visual_review_required must be boolean")
        if (self.date_time_bounds_px is None) != self.visual_review_required:
            raise ValueError(
                "visual review is required exactly when date-time bounds are unavailable"
            )
        _validate_sample_id(self.sample_id)
        _validate_sampled_at_monotonic(self.sampled_at_monotonic)


FrameworkLoader = Callable[[], MacFrameworks]
PlatformProvider = Callable[[], str]
VersionProvider = Callable[[], int]
SampleIDProvider = Callable[[], str]
MonotonicClock = Callable[[], float]
PrivateWindowIDProvider = Callable[[Any, Any], object]


class MacNativeBridge:
    """Strict macOS 26 PyObjC boundary with no eager native imports."""

    def __init__(
        self,
        *,
        framework_loader: FrameworkLoader | None = None,
        platform_provider: PlatformProvider | None = None,
        macos_major_version_provider: VersionProvider | None = None,
        sample_id_provider: SampleIDProvider | None = None,
        private_window_id_provider: PrivateWindowIDProvider | None = None,
        monotonic_clock: MonotonicClock | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._framework_loader = framework_loader or _load_pyobjc_frameworks
        self._platform_provider = platform_provider or (lambda: sys.platform)
        self._version_provider = (
            macos_major_version_provider or _macos_major_version
        )
        self._sample_id_provider = (
            sample_id_provider or (lambda: uuid.uuid4().hex)
        )
        self._private_window_id_provider = private_window_id_provider
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._sleeper = sleeper or time.sleep

    def permissions(self) -> MacPermissionState:
        frameworks = self._frameworks()
        try:
            screen_recording = (
                frameworks.Quartz.CGPreflightScreenCaptureAccess()
            )
            accessibility = (
                frameworks.ApplicationServices.AXIsProcessTrusted()
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_ENVIRONMENT",
                "macOS permission preflight failed",
            )
        if (
            type(screen_recording) is not bool
            or type(accessibility) is not bool
        ):
            _fail(
                "CAPTURE_ENVIRONMENT",
                "macOS permission preflight returned invalid data",
            )
        return MacPermissionState(screen_recording, accessibility)

    def windows(self) -> tuple[NativeWindowSample, ...]:
        frameworks = self._frameworks()
        try:
            quartz = frameworks.Quartz
            options = _strict_int(
                quartz.kCGWindowListOptionOnScreenOnly,
                minimum=0,
            ) | _strict_int(
                quartz.kCGWindowListExcludeDesktopElements,
                minimum=0,
            )
            raw_windows = quartz.CGWindowListCopyWindowInfo(
                options,
                _strict_int(quartz.kCGNullWindowID, minimum=0),
            )
        except EvidenceCaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_ENVIRONMENT",
                "Core Graphics window query failed",
            )
        sequence = _strict_sequence(
            raw_windows,
            "CAPTURE_WINDOW_IDENTITY",
            "Core Graphics returned invalid window metadata",
        )
        sample_id = self._sample_id("CAPTURE_WINDOW_IDENTITY")
        sampled_at_monotonic = self._sample_time(
            "CAPTURE_WINDOW_IDENTITY"
        )
        return tuple(
            _parse_cg_window(
                frameworks.Quartz,
                raw,
                sample_id=sample_id,
                sampled_at_monotonic=sampled_at_monotonic,
                require_on_screen=True,
            )
            for raw in sequence
        )

    def window_bounds(
        self,
        identity: BrowserWindowIdentity,
    ) -> NativeWindowSample:
        """Read current CGWindow bounds for exactly one bound identity."""
        try:
            frameworks = self._frameworks()
            window_id = _identity_window_id(identity)
            sample = self._lookup_cg_window(frameworks, window_id)
            if (
                sample.process_id != identity.process_id
                or sample.window_id != window_id
                or not sample.on_screen
                or sample.minimized
                or sample.layer != 0
            ):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "window layout convergence identity is invalid",
                )
            return sample
        except EvidenceCaptureError as error:
            if error.code == "CAPTURE_SYSTEM_UI":
                raise
            _fail(
                "CAPTURE_SYSTEM_UI",
                "window layout convergence identity cannot be proven",
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "window layout convergence query failed",
            )

    def focused_window(self) -> NativeWindowSample:
        frameworks = self._frameworks()
        try:
            sample, _element = self._focused_window(frameworks)
            return sample
        except EvidenceCaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "focused macOS window identity cannot be proven",
            )

    def activate_window(self, identity: BrowserWindowIdentity) -> None:
        frameworks = self._frameworks()
        try:
            expected_window_id = _identity_window_id(identity)
            window_element = self._bound_ax_window(frameworks, identity)
            running_application = (
                frameworks.AppKit.NSRunningApplication
                .runningApplicationWithProcessIdentifier_(identity.process_id)
            )
            if running_application is None:
                _fail(
                    "CAPTURE_FOREGROUND",
                    "browser process is not running",
                )
            activation_options = _strict_int(
                frameworks.AppKit.NSApplicationActivateIgnoringOtherApps,
                minimum=0,
            )
            activated = running_application.activateWithOptions_(
                activation_options
            )
            if type(activated) is not bool or not activated:
                _fail(
                    "CAPTURE_FOREGROUND",
                    "browser activation was not confirmed",
                )
            action_status = (
                frameworks.ApplicationServices.AXUIElementPerformAction(
                    window_element,
                    frameworks.ApplicationServices.kAXRaiseAction,
                )
            )
            if (
                type(action_status) is not int
                or action_status
                != _ax_success(frameworks.ApplicationServices)
            ):
                _fail(
                    "CAPTURE_FOREGROUND",
                    "browser window raise was not confirmed",
                )
            self._wait_for_window_focus(
                frameworks,
                process_id=identity.process_id,
                window_id=expected_window_id,
            )
        except EvidenceCaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_FOREGROUND",
                "browser activation failed",
            )

    def _wait_for_window_focus(
        self,
        frameworks: MacFrameworks,
        *,
        process_id: int,
        window_id: int,
    ) -> None:
        for attempt in range(_FOREGROUND_FOCUS_MAX_ATTEMPTS):
            try:
                focused, _raw = self._focused_window(frameworks)
            except EvidenceCaptureError:
                focused = None
            if (
                focused is not None
                and focused.process_id == process_id
                and focused.window_id == window_id
            ):
                return
            if attempt + 1 < _FOREGROUND_FOCUS_MAX_ATTEMPTS:
                self._sleeper(_FOREGROUND_FOCUS_POLL_SECONDS)
        _fail(
            "CAPTURE_FOREGROUND",
            "browser window did not become focused within the bounded wait",
        )

    def prepare_safe_capture_window(
        self,
        identity: BrowserWindowIdentity,
        *,
        menu_bar: DisplayBounds,
        dock: DisplayBounds,
        display: DisplayBounds,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> None:
        try:
            if not isinstance(policy, MacCapturePolicy):
                raise ValueError("policy must be MacCapturePolicy")
            frameworks = self._frameworks()
            expected_window_id = _identity_window_id(identity)
            window_element = self._bound_ax_window(frameworks, identity)
            focused, _focused_element = self._focused_window(frameworks)
            if (
                focused.process_id != identity.process_id
                or focused.window_id != expected_window_id
            ):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "safe capture preparation is not bound to the focused window",
                )
            if policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA:
                frame = fixed_capture_window_frame(
                    menu_bar=menu_bar,
                    dock=dock,
                    display=display,
                )
            else:
                frame = _available_window_frame(
                    menu_bar=menu_bar,
                    dock=dock,
                    display=display,
                )
            _set_ax_window_frame(
                frameworks.ApplicationServices,
                window_element,
                frame,
            )
        except EvidenceCaptureError as error:
            if error.code == "CAPTURE_SYSTEM_UI":
                raise
            _fail(
                "CAPTURE_SYSTEM_UI",
                "safe capture window identity cannot be proven",
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "macOS safe capture window preparation failed",
            )

    def web_area(
        self,
        identity: BrowserWindowIdentity,
    ) -> NativeWebAreaSample:
        frameworks = self._frameworks()
        try:
            try:
                window_id = _identity_window_id(identity)
                window = self._bound_ax_window(frameworks, identity)
                window_sample = self._lookup_cg_window(
                    frameworks,
                    window_id,
                )
            except EvidenceCaptureError:
                _fail("CAPTURE_ACCESSIBILITY", _WEB_AREA_IDENTITY_MISMATCH)
            application_services = frameworks.ApplicationServices
            deadline = (
                self._web_area_time()
                + _WEB_AREA_TIMEOUT_SECONDS
            )
            diagnostic = _WEB_AREA_TIMEOUT
            for attempt in range(_WEB_AREA_MAX_ATTEMPTS):
                self._require_web_area_deadline(deadline)
                sample, diagnostic = self._sample_web_area(
                    application_services,
                    window,
                    window_sample,
                    window_id,
                    identity.process_id,
                )
                if sample is not None:
                    self._require_web_area_deadline(deadline)
                    return sample
                if attempt == _WEB_AREA_MAX_ATTEMPTS - 1:
                    final_diagnostic = (
                        _WEB_AREA_TIMEOUT
                        if diagnostic == _WEB_AREA_NOT_FOUND
                        else diagnostic
                    )
                    _fail("CAPTURE_ACCESSIBILITY", final_diagnostic)
                self._wait_for_web_area_retry(deadline)
            _fail("CAPTURE_ACCESSIBILITY", _WEB_AREA_TIMEOUT)
        except EvidenceCaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_ACCESSIBILITY",
                _WEB_AREA_NATIVE_ERROR,
            )

    def _sample_web_area(
        self,
        application_services: Any,
        window: Any,
        window_sample: NativeWindowSample,
        window_id: int,
        expected_process_id: int,
    ) -> tuple[NativeWebAreaSample | None, str]:
        try:
            candidates: list[Any] = []
            roles_by_identity: dict[int, object | None] = {}

            def should_descend(element: Any) -> bool:
                role = _ax_optional_attribute(
                    application_services,
                    element,
                    application_services.kAXRoleAttribute,
                    code="CAPTURE_ACCESSIBILITY",
                )
                roles_by_identity[id(element)] = role
                return role != "AXWebArea"

            for element in _ax_descendants(
                application_services,
                window,
                code="CAPTURE_ACCESSIBILITY",
                should_descend=should_descend,
            ):
                if roles_by_identity[id(element)] == "AXWebArea":
                    candidates.append(element)
        except EvidenceCaptureError:
            return None, _WEB_AREA_NATIVE_ERROR

        if not candidates:
            return None, _WEB_AREA_NOT_FOUND
        if len(candidates) != 1:
            return None, _WEB_AREA_NOT_UNIQUE

        candidate = candidates[0]
        try:
            candidate_process_id = _ax_element_pid(
                application_services,
                candidate,
                code="CAPTURE_ACCESSIBILITY",
            )
        except EvidenceCaptureError:
            return None, _WEB_AREA_NATIVE_ERROR
        if candidate_process_id != expected_process_id:
            return None, _WEB_AREA_IDENTITY_MISMATCH
        try:
            _require_ax_visible(
                application_services,
                candidate,
                code="CAPTURE_ACCESSIBILITY",
            )
            bounds = _ax_element_bounds(
                application_services,
                candidate,
                code="CAPTURE_ACCESSIBILITY",
            )
        except EvidenceCaptureError:
            return None, _WEB_AREA_TIMEOUT
        if not _contains(window_sample.bounds_px, bounds):
            return None, _WEB_AREA_TIMEOUT
        try:
            return (
                NativeWebAreaSample(
                    window_id=window_id,
                    bounds_dip=bounds,
                    sample_id=self._sample_id("CAPTURE_ACCESSIBILITY"),
                    sampled_at_monotonic=self._sample_time(
                        "CAPTURE_ACCESSIBILITY"
                    ),
                ),
                _WEB_AREA_TIMEOUT,
            )
        except EvidenceCaptureError:
            return None, _WEB_AREA_NATIVE_ERROR

    def _wait_for_web_area_retry(self, deadline: float) -> None:
        now = self._web_area_time()
        if (
            now + _WEB_AREA_POLL_SECONDS
            > deadline + _WEB_AREA_CLOCK_EPSILON
        ):
            _fail("CAPTURE_ACCESSIBILITY", _WEB_AREA_TIMEOUT)
        self._sleeper(_WEB_AREA_POLL_SECONDS)
        if self._web_area_time() > deadline + _WEB_AREA_CLOCK_EPSILON:
            _fail("CAPTURE_ACCESSIBILITY", _WEB_AREA_TIMEOUT)

    def _require_web_area_deadline(self, deadline: float) -> None:
        if self._web_area_time() > deadline + _WEB_AREA_CLOCK_EPSILON:
            _fail("CAPTURE_ACCESSIBILITY", _WEB_AREA_TIMEOUT)

    def _web_area_time(self) -> float:
        value = self._monotonic_clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            _fail("CAPTURE_ACCESSIBILITY", _WEB_AREA_NATIVE_ERROR)
        return float(value)

    def system_ui(
        self,
        identity: BrowserWindowIdentity,
        *,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> NativeSystemUISample:
        frameworks = self._frameworks()
        try:
            if not isinstance(policy, MacCapturePolicy):
                _fail("CAPTURE_SYSTEM_UI", "macOS system UI policy is invalid")
            expected_window_id = _identity_window_id(identity)
            focused, _element = self._focused_window(frameworks)
            if (
                focused.process_id != identity.process_id
                or focused.window_id != expected_window_id
            ):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "system UI sample is not bound to the expected window",
                )
            display = self._primary_display_bounds(frameworks)
            try:
                menu_bar, clock = self._menu_bar_and_clock(
                    frameworks,
                    display,
                )
            except _ClockIdentityUnavailable as unavailable:
                if policy is not MacCapturePolicy.MAC_VISUAL_REVIEW_BETA:
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "exactly one current date-time menu item is required",
                    )
                dock = self._dock(frameworks, display)
                return NativeSystemUISample(
                    menu_bar_bounds_px=unavailable.menu_bar_bounds_px,
                    date_time_bounds_px=None,
                    dock_bounds_px=dock,
                    sample_id=self._sample_id("CAPTURE_SYSTEM_UI"),
                    sampled_at_monotonic=self._sample_time(
                        "CAPTURE_SYSTEM_UI"
                    ),
                    visual_review_required=True,
                )
            dock = self._dock(frameworks, display)
            return NativeSystemUISample(
                menu_bar_bounds_px=menu_bar,
                date_time_bounds_px=clock,
                dock_bounds_px=dock,
                sample_id=self._sample_id("CAPTURE_SYSTEM_UI"),
                sampled_at_monotonic=self._sample_time(
                    "CAPTURE_SYSTEM_UI"
                ),
            )
        except EvidenceCaptureError as error:
            if error.code == "CAPTURE_SYSTEM_UI":
                raise
            _fail(
                "CAPTURE_SYSTEM_UI",
                "system UI proof cannot be bound to the expected window",
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "macOS system UI proof failed",
            )

    def primary_display_bounds(self) -> DisplayBounds:
        frameworks = self._frameworks()
        try:
            return self._primary_display_bounds(frameworks)
        except EvidenceCaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_ENVIRONMENT",
                "primary display bounds query failed",
            )

    def _frameworks(self) -> MacFrameworks:
        try:
            platform_name = self._platform_provider()
            major_version = self._version_provider()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_ENVIRONMENT",
                "macOS platform version cannot be determined",
            )
        if platform_name != "darwin":
            _fail(
                "CAPTURE_ENVIRONMENT",
                "macOS native bridge is unavailable on this platform",
            )
        if (
            type(major_version) is not int
            or major_version != _SUPPORTED_MACOS_MAJOR
        ):
            _fail(
                "CAPTURE_ENVIRONMENT",
                "macOS native bridge requires macOS 26",
            )
        try:
            frameworks = self._framework_loader()
        except EvidenceCaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_ENVIRONMENT",
                "PyObjC frameworks are unavailable",
            )
        if not isinstance(frameworks, MacFrameworks):
            _fail(
                "CAPTURE_ENVIRONMENT",
                "PyObjC framework loader returned invalid data",
            )
        return frameworks

    def _sample_id(self, code: str) -> str:
        try:
            sample_id = self._sample_id_provider()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(code, "native sample identifier generation failed")
        if not isinstance(sample_id, str) or not sample_id.strip():
            _fail(code, "native sample identifier is invalid")
        return sample_id.strip()

    def _sample_time(self, code: str) -> float:
        try:
            value = self._monotonic_clock()
            _validate_sampled_at_monotonic(value)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(code, "native monotonic sample time is invalid")
        return float(value)

    def _focused_window(
        self,
        frameworks: MacFrameworks,
    ) -> tuple[NativeWindowSample, Any]:
        appkit = frameworks.AppKit
        application_services = frameworks.ApplicationServices
        workspace = appkit.NSWorkspace.sharedWorkspace()
        frontmost = workspace.frontmostApplication()
        if frontmost is None:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "frontmost macOS application is unavailable",
            )
        frontmost_pid = _positive_int(frontmost.processIdentifier())
        application = application_services.AXUIElementCreateApplication(
            frontmost_pid
        )
        if application is None:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "frontmost AX application is unavailable",
            )
        focused = _ax_required_attribute(
            application_services,
            application,
            application_services.kAXFocusedWindowAttribute,
            code="CAPTURE_WINDOW_IDENTITY",
        )
        focused_pid = _ax_element_pid(
            application_services,
            focused,
            code="CAPTURE_WINDOW_IDENTITY",
        )
        if focused_pid != frontmost_pid:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "frontmost and focused AX process IDs differ",
            )
        window_id = self._private_window_id(
            application_services,
            focused,
        )
        sample = self._lookup_cg_window(frameworks, window_id)
        if sample.process_id != frontmost_pid:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "AX and Core Graphics process IDs differ",
            )
        return sample, focused

    def _private_window_id(
        self,
        application_services: Any,
        element: Any,
    ) -> int:
        provider = (
            self._private_window_id_provider
            or _private_ax_window_id_result
        )
        try:
            result = provider(application_services, element)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "private AX window identity API is unavailable",
            )
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or type(result[0]) is not int
            or result[0] != _ax_success(application_services)
        ):
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "private AX window identity query failed",
            )
        return _positive_int_or_fail(
            result[1],
            code="CAPTURE_WINDOW_IDENTITY",
            message="private AX window ID is invalid",
        )

    def _lookup_cg_window(
        self,
        frameworks: MacFrameworks,
        window_id: int,
    ) -> NativeWindowSample:
        quartz = frameworks.Quartz
        try:
            raw_windows = quartz.CGWindowListCopyWindowInfo(
                _strict_int(
                    quartz.kCGWindowListOptionIncludingWindow,
                    minimum=0,
                ),
                window_id,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "Core Graphics window identity query failed",
            )
        windows = _strict_sequence(
            raw_windows,
            "CAPTURE_WINDOW_IDENTITY",
            "Core Graphics returned invalid window identity metadata",
        )
        if len(windows) != 1:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "Core Graphics window identity is not unique",
            )
        sample = _parse_cg_window(
            quartz,
            windows[0],
            sample_id=self._sample_id("CAPTURE_WINDOW_IDENTITY"),
            sampled_at_monotonic=self._sample_time(
                "CAPTURE_WINDOW_IDENTITY"
            ),
            require_on_screen=True,
        )
        if sample.window_id != window_id:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "Core Graphics returned a different window ID",
            )
        return sample

    def _bound_ax_window(
        self,
        frameworks: MacFrameworks,
        identity: BrowserWindowIdentity,
    ) -> Any:
        window_id = _identity_window_id(identity)
        cg_window = self._lookup_cg_window(frameworks, window_id)
        if cg_window.process_id != identity.process_id:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "browser identity process ID does not own the window",
            )
        application_services = frameworks.ApplicationServices
        application = application_services.AXUIElementCreateApplication(
            identity.process_id
        )
        if application is None:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "browser AX application is unavailable",
            )
        raw_windows = _ax_required_attribute(
            application_services,
            application,
            application_services.kAXWindowsAttribute,
            code="CAPTURE_WINDOW_IDENTITY",
        )
        ax_windows = _strict_sequence(
            raw_windows,
            "CAPTURE_WINDOW_IDENTITY",
            "browser AX windows metadata is invalid",
        )
        matches: list[Any] = []
        for candidate in ax_windows:
            candidate_id = self._private_window_id(
                application_services,
                candidate,
            )
            if candidate_id == window_id:
                matches.append(candidate)
        if len(matches) != 1:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "browser AX window identity is not unique",
            )
        candidate_pid = _ax_element_pid(
            application_services,
            matches[0],
            code="CAPTURE_WINDOW_IDENTITY",
        )
        if candidate_pid != identity.process_id:
            _fail(
                "CAPTURE_WINDOW_IDENTITY",
                "browser AX window process ID differs",
            )
        return matches[0]

    def _primary_display_bounds(
        self,
        frameworks: MacFrameworks,
    ) -> DisplayBounds:
        quartz = frameworks.Quartz
        display_id = _positive_int_or_fail(
            quartz.CGMainDisplayID(),
            code="CAPTURE_ENVIRONMENT",
            message="primary display ID is invalid",
        )
        raw_bounds = quartz.CGDisplayBounds(display_id)
        logical_bounds = _display_bounds_from_cg_rect(
            raw_bounds,
            code="CAPTURE_ENVIRONMENT",
            message="primary display bounds are invalid",
        )
        screen = frameworks.AppKit.NSScreen.mainScreen()
        if screen is None:
            _fail(
                "CAPTURE_ENVIRONMENT",
                "primary display backing scale is unavailable",
            )
        scale = screen.backingScaleFactor()
        if (
            isinstance(scale, bool)
            or not isinstance(scale, int | float)
            or not math.isfinite(scale)
            or scale <= 0
        ):
            _fail(
                "CAPTURE_ENVIRONMENT",
                "primary display backing scale is invalid",
            )
        return DisplayBounds(
            round(logical_bounds.x * scale),
            round(logical_bounds.y * scale),
            round(logical_bounds.width * scale),
            round(logical_bounds.height * scale),
        )

    def _menu_bar_and_clock(
        self,
        frameworks: MacFrameworks,
        display: DisplayBounds,
    ) -> tuple[DisplayBounds, DisplayBounds]:
        appkit = frameworks.AppKit
        application_services = frameworks.ApplicationServices
        menu_candidates: list[tuple[str, int, Any, DisplayBounds]] = []
        for bundle_id in _SYSTEM_UI_BUNDLE_IDS:
            for running_application in _running_applications(
                appkit,
                bundle_id,
                code="CAPTURE_SYSTEM_UI",
            ):
                owner_bundle = running_application.bundleIdentifier()
                if owner_bundle != bundle_id:
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "system UI owner bundle identifier differs",
                    )
                pid = _positive_int_or_fail(
                    running_application.processIdentifier(),
                    code="CAPTURE_SYSTEM_UI",
                    message="system UI owner process ID is invalid",
                )
                application = (
                    application_services.AXUIElementCreateApplication(pid)
                )
                if application is None:
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "system UI AX application is unavailable",
                    )
                menu_bar = _ax_optional_attribute(
                    application_services,
                    application,
                    application_services.kAXMenuBarAttribute,
                    code="CAPTURE_SYSTEM_UI",
                )
                if menu_bar is None:
                    continue
                if (
                    _ax_element_pid(
                        application_services,
                        menu_bar,
                        code="CAPTURE_SYSTEM_UI",
                    )
                    != pid
                ):
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "system UI menu bar process ID differs",
                    )
                role = _ax_required_attribute(
                    application_services,
                    menu_bar,
                    application_services.kAXRoleAttribute,
                    code="CAPTURE_SYSTEM_UI",
                )
                if role != "AXMenuBar":
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "system UI menu bar role is invalid",
                    )
                _require_ax_visible(
                    application_services,
                    menu_bar,
                    code="CAPTURE_SYSTEM_UI",
                )
                bounds = _ax_element_bounds(
                    application_services,
                    menu_bar,
                    code="CAPTURE_SYSTEM_UI",
                )
                if not _contains(display, bounds):
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "system UI menu bar is outside the primary display",
                    )
                menu_candidates.append((bundle_id, pid, menu_bar, bounds))
        if len(menu_candidates) != 1:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "exactly one current macOS menu bar is required",
            )
        owner_bundle, owner_pid, menu_bar, menu_bounds = menu_candidates[0]
        if owner_bundle not in _SYSTEM_UI_BUNDLE_IDS:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "system UI menu bar owner is invalid",
            )
        clock_candidates: list[DisplayBounds] = []
        for element in _ax_descendants(
            application_services,
            menu_bar,
            code="CAPTURE_SYSTEM_UI",
        ):
            identifier = _ax_optional_attribute(
                application_services,
                element,
                application_services.kAXIdentifierAttribute,
                code="CAPTURE_SYSTEM_UI",
            )
            if identifier not in _CLOCK_IDENTIFIERS:
                continue
            role = _ax_required_attribute(
                application_services,
                element,
                application_services.kAXRoleAttribute,
                code="CAPTURE_SYSTEM_UI",
            )
            if role not in _CLOCK_ROLES:
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "date-time menu item role is invalid",
                )
            if (
                _ax_element_pid(
                    application_services,
                    element,
                    code="CAPTURE_SYSTEM_UI",
                )
                != owner_pid
            ):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "date-time menu item process ID differs",
                )
            _require_ax_visible(
                application_services,
                element,
                code="CAPTURE_SYSTEM_UI",
            )
            bounds = _ax_element_bounds(
                application_services,
                element,
                code="CAPTURE_SYSTEM_UI",
            )
            if not _contains(display, bounds) or not _contains(
                menu_bounds,
                bounds,
            ):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "date-time menu item is outside the current menu bar",
                )
            clock_candidates.append(bounds)
        if len(clock_candidates) != 1:
            raise _ClockIdentityUnavailable(menu_bounds)
        return menu_bounds, clock_candidates[0]

    def _dock(
        self,
        frameworks: MacFrameworks,
        display: DisplayBounds,
    ) -> DisplayBounds:
        appkit = frameworks.AppKit
        application_services = frameworks.ApplicationServices
        running = _running_applications(
            appkit,
            _DOCK_BUNDLE_ID,
            code="CAPTURE_SYSTEM_UI",
        )
        if len(running) != 1:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "exactly one current Dock owner is required",
            )
        dock_owner = running[0]
        if dock_owner.bundleIdentifier() != _DOCK_BUNDLE_ID:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "Dock owner bundle identifier differs",
            )
        pid = _positive_int_or_fail(
            dock_owner.processIdentifier(),
            code="CAPTURE_SYSTEM_UI",
            message="Dock owner process ID is invalid",
        )
        application = application_services.AXUIElementCreateApplication(pid)
        if application is None:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "Dock AX application is unavailable",
            )
        candidates: list[DisplayBounds] = []
        dock_children = _ax_optional_attribute(
            application_services,
            application,
            application_services.kAXChildrenAttribute,
            code="CAPTURE_SYSTEM_UI",
        )
        for element in (
            ()
            if dock_children is None
            else _strict_sequence(
                dock_children,
                "CAPTURE_SYSTEM_UI",
                "Dock children metadata is invalid",
            )
        ):
            role = _ax_optional_attribute(
                application_services,
                element,
                application_services.kAXRoleAttribute,
                code="CAPTURE_SYSTEM_UI",
            )
            subrole = _ax_optional_attribute(
                application_services,
                element,
                application_services.kAXSubroleAttribute,
                code="CAPTURE_SYSTEM_UI",
            )
            if (role, subrole) not in _DOCK_CONTAINER_ROLES:
                continue
            if (
                _ax_element_pid(
                    application_services,
                    element,
                    code="CAPTURE_SYSTEM_UI",
                )
                != pid
            ):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "Dock container process ID differs",
                )
            if subrole is None:
                hidden = _ax_optional_attribute(
                    application_services,
                    element,
                    application_services.kAXHiddenAttribute,
                    code="CAPTURE_SYSTEM_UI",
                )
                if hidden is not None and (
                    type(hidden) is not bool or hidden
                ):
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "AX element is not proven currently visible",
                    )
                visible_children = _ax_required_attribute(
                    application_services,
                    element,
                    application_services.kAXVisibleChildrenAttribute,
                    code="CAPTURE_SYSTEM_UI",
                )
                if not _strict_sequence(
                    visible_children,
                    "CAPTURE_SYSTEM_UI",
                    "Dock visible children metadata is invalid",
                ):
                    _fail(
                        "CAPTURE_SYSTEM_UI",
                        "AX element is not proven currently visible",
                    )
            else:
                _require_ax_visible(
                    application_services,
                    element,
                    code="CAPTURE_SYSTEM_UI",
                )
            bounds = _ax_element_bounds(
                application_services,
                element,
                code="CAPTURE_SYSTEM_UI",
            )
            if not _contains(display, bounds):
                _fail(
                    "CAPTURE_SYSTEM_UI",
                    "Dock is outside the primary display",
                )
            candidates.append(bounds)
        if len(candidates) != 1:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "exactly one current visible Dock container is required",
            )
        return candidates[0]


def _load_pyobjc_frameworks() -> MacFrameworks:
    import AppKit  # type: ignore[import-untyped]
    import ApplicationServices  # type: ignore[import-untyped]
    import Quartz  # type: ignore[import-untyped]

    return MacFrameworks(AppKit, ApplicationServices, Quartz)


def _macos_major_version() -> int:
    version = platform.mac_ver()[0]
    major, _separator, _remainder = version.partition(".")
    return int(major)


def _private_ax_window_id_result(
    application_services: Any,
    element: Any,
) -> object:
    function = getattr(
        application_services,
        "_AXUIElementGetWindow",
        None,
    )
    if not callable(function):
        import objc  # type: ignore[import-untyped]

        bundle = getattr(application_services, "__bundle__")
        functions: dict[str, Any] = {}
        objc.loadBundleFunctions(
            bundle,
            functions,
            [
                (
                    "_AXUIElementGetWindow",
                    b"i^{__AXUIElement=}o^I",
                )
            ],
        )
        function = functions.get("_AXUIElementGetWindow")
    if not callable(function):
        raise AttributeError("_AXUIElementGetWindow")
    return function(element, None)


def _parse_cg_window(
    quartz: Any,
    raw: object,
    *,
    sample_id: str,
    sampled_at_monotonic: float,
    require_on_screen: bool,
) -> NativeWindowSample:
    if not isinstance(raw, Mapping):
        _fail(
            "CAPTURE_WINDOW_IDENTITY",
            "Core Graphics window entry is invalid",
        )
    try:
        process_id = _positive_int(raw[quartz.kCGWindowOwnerPID])
        window_id = _positive_int(raw[quartz.kCGWindowNumber])
        layer = _strict_int(raw[quartz.kCGWindowLayer], minimum=0)
        on_screen = raw[quartz.kCGWindowIsOnscreen]
        raw_bounds = raw[quartz.kCGWindowBounds]
    except EvidenceCaptureError:
        raise
    except Exception:
        _fail(
            "CAPTURE_WINDOW_IDENTITY",
            "Core Graphics window entry is incomplete",
        )
    if type(on_screen) is not bool:
        _fail(
            "CAPTURE_WINDOW_IDENTITY",
            "Core Graphics window visibility is invalid",
        )
    if require_on_screen and not on_screen:
        _fail(
            "CAPTURE_WINDOW_IDENTITY",
            "Core Graphics window is not currently on screen",
        )
    bounds = _display_bounds_from_mapping(
        raw_bounds,
        code="CAPTURE_WINDOW_IDENTITY",
        message="Core Graphics window bounds are invalid",
    )
    return NativeWindowSample(
        process_id=process_id,
        window_id=window_id,
        bounds_px=bounds,
        layer=layer,
        on_screen=on_screen,
        minimized=False,
        sample_id=sample_id,
        sampled_at_monotonic=sampled_at_monotonic,
    )


def _ax_required_attribute(
    application_services: Any,
    element: Any,
    attribute: Any,
    *,
    code: str,
) -> Any:
    result = application_services.AXUIElementCopyAttributeValue(
        element,
        attribute,
        None,
    )
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or type(result[0]) is not int
        or result[0] != _ax_success(application_services)
        or result[1] is None
    ):
        _fail(code, "required AX attribute is unavailable")
    return result[1]


def _ax_optional_attribute(
    application_services: Any,
    element: Any,
    attribute: Any,
    *,
    code: str,
) -> Any | None:
    result = application_services.AXUIElementCopyAttributeValue(
        element,
        attribute,
        None,
    )
    if not isinstance(result, tuple) or len(result) != 2:
        _fail(code, "AX attribute result is invalid")
    status, value = result
    if type(status) is not int:
        _fail(code, "AX attribute status is invalid")
    if status != _ax_success(application_services):
        absence_statuses = {
            _strict_int(
                application_services.kAXErrorAttributeUnsupported,
                minimum=-sys.maxsize - 1,
            ),
            _strict_int(
                application_services.kAXErrorNoValue,
                minimum=-sys.maxsize - 1,
            ),
        }
        if status in absence_statuses:
            return None
        _fail(code, "optional AX attribute query failed")
    if value is None:
        _fail(code, "AX attribute value is invalid")
    return value


def _ax_element_pid(
    application_services: Any,
    element: Any,
    *,
    code: str,
) -> int:
    result = application_services.AXUIElementGetPid(element, None)
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or type(result[0]) is not int
        or result[0] != _ax_success(application_services)
    ):
        _fail(code, "AX element process ID query failed")
    return _positive_int_or_fail(
        result[1],
        code=code,
        message="AX element process ID is invalid",
    )


def _ax_descendants(
    application_services: Any,
    root: Any,
    *,
    code: str,
    should_descend: Callable[[Any], bool] | None = None,
) -> tuple[Any, ...]:
    children = _ax_optional_attribute(
        application_services,
        root,
        application_services.kAXChildrenAttribute,
        code=code,
    )
    if children is None:
        return ()
    queue = list(
        _strict_sequence(
            children,
            code,
            "AX children metadata is invalid",
        )
    )
    descendants: list[Any] = []
    seen: set[int] = set()
    while queue:
        element = queue.pop(0)
        identity = id(element)
        if identity in seen:
            continue
        seen.add(identity)
        descendants.append(element)
        if len(descendants) > _MAX_AX_TREE_ELEMENTS:
            _fail(code, "AX tree exceeds the bounded element limit")
        if should_descend is not None and not should_descend(element):
            continue
        nested = _ax_optional_attribute(
            application_services,
            element,
            application_services.kAXChildrenAttribute,
            code=code,
        )
        if nested is not None:
            queue.extend(
                _strict_sequence(
                    nested,
                    code,
                    "AX children metadata is invalid",
                )
            )
    return tuple(descendants)


def _require_ax_visible(
    application_services: Any,
    element: Any,
    *,
    code: str,
) -> None:
    hidden = _ax_required_attribute(
        application_services,
        element,
        application_services.kAXHiddenAttribute,
        code=code,
    )
    if type(hidden) is not bool or hidden:
        _fail(code, "AX element is not proven currently visible")


def _ax_element_bounds(
    application_services: Any,
    element: Any,
    *,
    code: str,
) -> DisplayBounds:
    position_value = _ax_required_attribute(
        application_services,
        element,
        application_services.kAXPositionAttribute,
        code=code,
    )
    size_value = _ax_required_attribute(
        application_services,
        element,
        application_services.kAXSizeAttribute,
        code=code,
    )
    position = _decode_ax_value(
        application_services,
        position_value,
        application_services.kAXValueCGPointType,
        code=code,
    )
    size = _decode_ax_value(
        application_services,
        size_value,
        application_services.kAXValueCGSizeType,
        code=code,
    )
    return _outward_display_bounds_from_values(
        _member(position, "x"),
        _member(position, "y"),
        _member(size, "width"),
        _member(size, "height"),
        code=code,
        message="AX element bounds are invalid",
    )


def _outward_display_bounds_from_values(
    x: object,
    y: object,
    width: object,
    height: object,
    *,
    code: str,
    message: str,
) -> DisplayBounds:
    """Conservatively enclose finite AX floating-point element bounds."""
    try:
        left = _finite_real_number(x)
        top = _finite_real_number(y)
        raw_width = _finite_real_number(width)
        raw_height = _finite_real_number(height)
        if raw_width <= 0 or raw_height <= 0:
            raise ValueError("AX bounds dimensions must be positive")
        right = left + raw_width
        bottom = top + raw_height
        if not math.isfinite(right) or not math.isfinite(bottom):
            raise ValueError("AX bounds extent must be finite")
        rounded_left = math.floor(left)
        rounded_top = math.floor(top)
        rounded_right = math.ceil(right)
        rounded_bottom = math.ceil(bottom)
        return DisplayBounds(
            rounded_left,
            rounded_top,
            rounded_right - rounded_left,
            rounded_bottom - rounded_top,
        )
    except (OverflowError, TypeError, ValueError):
        _fail(code, message)


def _decode_ax_value(
    application_services: Any,
    value: Any,
    expected_type: Any,
    *,
    code: str,
) -> Any:
    if (
        type(expected_type) is not int
        or application_services.AXValueGetType(value) != expected_type
    ):
        _fail(code, "AX element bounds types are invalid")
    result = application_services.AXValueGetValue(
        value,
        expected_type,
        None,
    )
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or type(result[0]) is not bool
        or not result[0]
        or result[1] is None
    ):
        _fail(code, "AX element bounds value is invalid")
    return result[1]


def _running_applications(
    appkit: Any,
    bundle_id: str,
    *,
    code: str,
) -> tuple[Any, ...]:
    raw = (
        appkit.NSRunningApplication
        .runningApplicationsWithBundleIdentifier_(bundle_id)
    )
    return _strict_sequence(
        raw,
        code,
        "running application metadata is invalid",
    )


def _display_bounds_from_mapping(
    raw: object,
    *,
    code: str,
    message: str,
) -> DisplayBounds:
    if not isinstance(raw, Mapping):
        _fail(code, message)
    try:
        return _display_bounds_from_values(
            raw["X"],
            raw["Y"],
            raw["Width"],
            raw["Height"],
            code=code,
            message=message,
        )
    except EvidenceCaptureError:
        raise
    except Exception:
        _fail(code, message)


def _display_bounds_from_cg_rect(
    raw: object,
    *,
    code: str,
    message: str,
) -> DisplayBounds:
    try:
        origin = _member(raw, "origin")
        size = _member(raw, "size")
        return _display_bounds_from_values(
            _member(origin, "x"),
            _member(origin, "y"),
            _member(size, "width"),
            _member(size, "height"),
            code=code,
            message=message,
        )
    except EvidenceCaptureError:
        raise
    except Exception:
        _fail(code, message)


def _display_bounds_from_values(
    x: object,
    y: object,
    width: object,
    height: object,
    *,
    code: str,
    message: str,
) -> DisplayBounds:
    try:
        return DisplayBounds(
            _integral_number(x),
            _integral_number(y),
            _integral_number(width),
            _integral_number(height),
        )
    except (TypeError, ValueError):
        _fail(code, message)


def _member(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _strict_sequence(
    value: object,
    code: str,
    message: str,
) -> tuple[Any, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes | bytearray)
    ):
        _fail(code, message)
    return tuple(value)


def _identity_window_id(identity: BrowserWindowIdentity) -> int:
    if not isinstance(identity, BrowserWindowIdentity):
        raise ValueError("identity must be BrowserWindowIdentity")
    if identity.platform not in {"darwin", "macos"}:
        _fail(
            "CAPTURE_WINDOW_IDENTITY",
            "browser identity is not a macOS window",
        )
    handle = identity.window_handle
    try:
        window_id = (
            int(handle, 16)
            if handle.lower().startswith("0x")
            else int(handle, 10)
        )
    except ValueError:
        _fail(
            "CAPTURE_WINDOW_IDENTITY",
            "browser window handle is not a CGWindowID",
        )
    return _positive_int_or_fail(
        window_id,
        code="CAPTURE_WINDOW_IDENTITY",
        message="browser window handle is not a positive CGWindowID",
    )


def _contains(outer: DisplayBounds, inner: DisplayBounds) -> bool:
    return (
        inner.x >= outer.x
        and inner.y >= outer.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def _available_window_frame(
    *,
    menu_bar: object,
    dock: object,
    display: object,
) -> DisplayBounds:
    if (
        not isinstance(menu_bar, DisplayBounds)
        or not isinstance(dock, DisplayBounds)
        or not isinstance(display, DisplayBounds)
    ):
        _fail("CAPTURE_SYSTEM_UI", "window layout bounds are invalid")
    if not _contains(display, menu_bar) or not _contains(display, dock):
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout system UI is outside the display",
        )
    if _intersects(menu_bar, dock):
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout system UI bounds overlap",
        )
    display_right = display.x + display.width
    display_bottom = display.y + display.height
    top = menu_bar.y + menu_bar.height
    if top >= display_bottom:
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout available frame is not positive",
        )

    left = display.x
    right = display_right
    bottom = display_bottom
    bottom_gap = display_bottom - (dock.y + dock.height)
    top_gap = dock.y - top
    left_gap = dock.x - display.x
    right_gap = display_right - (dock.x + dock.width)
    if dock.width > dock.height:
        dock_edge = "bottom"
        if bottom_gap > min(top_gap, left_gap, right_gap):
            _fail(
                "CAPTURE_SYSTEM_UI",
                "Dock does not define a supported horizontal safe boundary",
            )
    elif dock.height > dock.width:
        side_gaps = (("left", left_gap), ("right", right_gap))
        minimum_side_gap = min(gap for _edge, gap in side_gaps)
        closest_sides = tuple(
            edge for edge, gap in side_gaps if gap == minimum_side_gap
        )
        if (
            len(closest_sides) != 1
            or minimum_side_gap > min(top_gap, bottom_gap)
        ):
            _fail(
                "CAPTURE_SYSTEM_UI",
                "Dock does not define one supported side boundary",
            )
        dock_edge = closest_sides[0]
    else:
        supported_gaps = (
            ("bottom", bottom_gap),
            ("left", left_gap),
            ("right", right_gap),
        )
        minimum_gap = min(gap for _edge, gap in supported_gaps)
        closest_edges = tuple(
            edge for edge, gap in supported_gaps if gap == minimum_gap
        )
        if len(closest_edges) != 1 or minimum_gap != 0:
            _fail(
                "CAPTURE_SYSTEM_UI",
                "Dock must block exactly one supported available-frame direction",
            )
        dock_edge = closest_edges[0]
    if dock_edge == "bottom":
        bottom = dock.y
    elif dock_edge == "left":
        left = dock.x + dock.width
    elif dock_edge == "right":
        right = dock.x
    else:
        _fail(
            "CAPTURE_SYSTEM_UI",
            "Dock must block exactly one supported available-frame direction",
        )

    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout available frame is not positive",
        )
    frame = DisplayBounds(left, top, width, height)
    if (
        not _contains(display, frame)
        or _intersects(frame, menu_bar)
        or _intersects(frame, dock)
    ):
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout available frame is invalid",
        )
    return frame


def fixed_capture_window_frame(
    *,
    menu_bar: DisplayBounds,
    dock: DisplayBounds,
    display: DisplayBounds,
) -> DisplayBounds:
    """Return the beta target in physical pixels, inset from the safe frame."""
    available = _available_window_frame(
        menu_bar=menu_bar,
        dock=dock,
        display=display,
    )
    width = available.width - 2 * _FIXED_CAPTURE_FRAME_INSET_PX
    height = available.height - 2 * _FIXED_CAPTURE_FRAME_INSET_PX
    if width <= 0 or height <= 0:
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout fixed capture frame is not positive",
        )
    return DisplayBounds(
        available.x + _FIXED_CAPTURE_FRAME_INSET_PX,
        available.y + _FIXED_CAPTURE_FRAME_INSET_PX,
        width,
        height,
    )


def safe_capture_window_has_required_coverage(
    browser: object,
    *,
    menu_bar: object,
    dock: object,
    display: object,
) -> bool:
    if not isinstance(browser, DisplayBounds):
        _fail("CAPTURE_SYSTEM_UI", "browser window bounds are invalid")
    if not isinstance(menu_bar, DisplayBounds):
        _fail("CAPTURE_SYSTEM_UI", "menu bar bounds are invalid")
    if not isinstance(dock, DisplayBounds):
        _fail("CAPTURE_SYSTEM_UI", "Dock bounds are invalid")
    if not isinstance(display, DisplayBounds):
        _fail("CAPTURE_SYSTEM_UI", "display bounds are invalid")
    frame = _available_window_frame(
        menu_bar=menu_bar,
        dock=dock,
        display=display,
    )
    return (
        _contains(frame, browser)
        and not _intersects(browser, menu_bar)
        and not _intersects(browser, dock)
        and browser.width >= frame.width * 0.9
        and browser.height >= frame.height * 0.9
    )


def _set_ax_window_frame(
    application_services: Any,
    element: Any,
    frame: DisplayBounds,
) -> None:
    point_type = _strict_int(
        application_services.kAXValueCGPointType,
        minimum=0,
    )
    size_type = _strict_int(
        application_services.kAXValueCGSizeType,
        minimum=0,
    )
    position = application_services.AXValueCreate(
        point_type,
        application_services.CGPointMake(float(frame.x), float(frame.y)),
    )
    size = application_services.AXValueCreate(
        size_type,
        application_services.CGSizeMake(
            float(frame.width),
            float(frame.height),
        ),
    )
    if position is None or size is None:
        _fail(
            "CAPTURE_SYSTEM_UI",
            "window layout AX frame values are unavailable",
        )
    for attribute, value in (
        (application_services.kAXPositionAttribute, position),
        (application_services.kAXSizeAttribute, size),
    ):
        status = application_services.AXUIElementSetAttributeValue(
            element,
            attribute,
            value,
        )
        if (
            type(status) is not int
            or status != _ax_success(application_services)
        ):
            _fail(
                "CAPTURE_SYSTEM_UI",
                "window layout AX frame setting failed",
            )


def _intersects(first: DisplayBounds, second: DisplayBounds) -> bool:
    return (
        first.x < second.x + second.width
        and first.x + first.width > second.x
        and first.y < second.y + second.height
        and first.y + first.height > second.y
    )


def _ax_success(application_services: Any) -> int:
    return _strict_int(application_services.kAXErrorSuccess, minimum=0)


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("value must be a positive integer")
    return int(value)


def _positive_int_or_fail(
    value: object,
    *,
    code: str,
    message: str,
) -> int:
    try:
        return _positive_int(value)
    except ValueError:
        _fail(code, message)


def _strict_int(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("value must be an integer in range")
    return int(value)


def _integral_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("value must be numeric")
    if not math.isfinite(value) or not float(value).is_integer():
        raise ValueError("value must be finite and integral")
    return int(value)


def _finite_real_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("value must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError("value must be finite")
    return normalized


def _validate_sample_id(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sample_id must be a nonblank string")


def _validate_sampled_at_monotonic(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(
            "sampled_at_monotonic must be exact, finite, and non-negative"
        )


def _fail(code: str, message: str) -> NoReturn:
    raise make_capture_error(code, message)

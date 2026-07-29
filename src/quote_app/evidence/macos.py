from __future__ import annotations

import threading
import time
import math
import platform as host_platform
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from quote_app.evidence.geometry import GeometryError
from quote_app.evidence.models import (
    EvidenceRecord,
    MacCapturePolicy,
    validate_mac_capture_policy,
)
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureEnvironmentUnavailable,
    CaptureGeometrySnapshot,
    CaptureRequest,
    EvidenceCaptureError,
    EvidenceCapturePipeline,
    SystemUIProof,
    revalidate_capture_error,
)

GeometrySnapshotProvider = Callable[
    [BrowserWindowIdentity],
    CaptureGeometrySnapshot,
]
ForegroundProvider = Callable[[], BrowserWindowIdentity]
PrepareCallback = Callable[[BrowserWindowIdentity], None]
SystemUIProvider = Callable[[CaptureGeometrySnapshot], SystemUIProof]
PostCaptureValidator = Callable[[CaptureGeometrySnapshot], None]

_MSS_DARWIN_OPTIONS_LOCK = threading.Lock()


class MacOSCaptureEnvironment:
    """Fail-closed macOS adapter driven by one authoritative CDP/native sample."""

    def __init__(
        self,
        *,
        geometry_snapshot_provider: GeometrySnapshotProvider | None = None,
        foreground_provider: ForegroundProvider | None = None,
        prepare_callback: PrepareCallback | None = None,
        permission_provider: Callable[[], bool] | None = None,
        system_ui_proof_provider: SystemUIProvider | None = None,
        post_capture_validator: PostCaptureValidator | None = None,
        validate_capture_scale_dpr: bool = True,
    ) -> None:
        self._geometry_snapshot_provider = geometry_snapshot_provider
        self._foreground_provider = foreground_provider
        self._prepare_callback = prepare_callback
        self._permission_provider = permission_provider
        self._system_ui_proof_provider = system_ui_proof_provider
        self._post_capture_validator = post_capture_validator
        self._validate_capture_scale_dpr = validate_capture_scale_dpr

    def screen_capture_permission(self) -> bool:
        if self._permission_provider is not None:
            try:
                result = self._permission_provider()
            except EvidenceCaptureError as error:
                raise revalidate_capture_error(
                    error,
                    fallback_code="CAPTURE_ENVIRONMENT",
                    safe_message="macOS screen permission provider failed",
                ) from None
            except Exception:
                raise CaptureEnvironmentUnavailable(
                    "macOS screen permission provider failed"
                ) from None
            if type(result) is not bool:
                raise CaptureEnvironmentUnavailable(
                    "macOS screen permission provider returned invalid data"
                )
            return result
        try:
            import ctypes

            core_graphics = ctypes.CDLL(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
            )
            probe = core_graphics.CGPreflightScreenCaptureAccess
            probe.argtypes = []
            probe.restype = ctypes.c_bool
            return bool(probe())
        except (AttributeError, OSError):
            return False

    def prepare_browser(self, expected: BrowserWindowIdentity) -> None:
        if self._prepare_callback is None:
            raise CaptureEnvironmentUnavailable(
                "authoritative browser preparation callback is required"
            )
        try:
            result = self._prepare_callback(expected)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="browser preparation callback failed",
            ) from None
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

    def geometry_snapshot(
        self,
        expected: BrowserWindowIdentity,
    ) -> CaptureGeometrySnapshot:
        if self._geometry_snapshot_provider is None:
            raise CaptureEnvironmentUnavailable(
                "authoritative capture geometry snapshot is required"
            )
        try:
            snapshot = self._geometry_snapshot_provider(expected)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="capture geometry snapshot provider failed",
            ) from None
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
        return snapshot

    def system_ui_proof(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof:
        # macOS has no safe default here: Dock/menu auto-hide defaults are not
        # proof that both bars and the date/time were visible in this sample.
        if self._system_ui_proof_provider is None:
            raise CaptureEnvironmentUnavailable(
                "authoritative macOS system UI proof is required"
            )
        try:
            proof = self._system_ui_proof_provider(snapshot)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="macOS system UI proof provider failed",
            ) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CaptureEnvironmentUnavailable(
                "macOS system UI proof provider failed"
            ) from None
        if not isinstance(proof, SystemUIProof):
            raise CaptureEnvironmentUnavailable(
                "macOS system UI proof provider returned invalid data"
            )
        return proof

    def foreground_window(self) -> BrowserWindowIdentity:
        if self._foreground_provider is None:
            raise CaptureEnvironmentUnavailable(
                "authoritative macOS foreground-window provider is required"
            )
        try:
            foreground = self._foreground_provider()
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="macOS foreground provider failed",
            ) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CaptureEnvironmentUnavailable(
                "macOS foreground provider failed"
            ) from None
        if not isinstance(foreground, BrowserWindowIdentity):
            raise CaptureEnvironmentUnavailable(
                "macOS foreground provider returned invalid data"
            )
        return foreground

    def capture_primary_display(
        self,
        destination: Path,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        physical_size = _macos_main_display_physical_size()
        expected_size = (
            snapshot.display_physical_bounds.width,
            snapshot.display_physical_bounds.height,
        )
        if expected_size != physical_size:
            raise GeometryError(
                "geometry snapshot physical display size is stale"
            )
        if snapshot.device_pixel_ratio is None:
            raise GeometryError(
                "macOS geometry snapshot must include JavaScript DPR"
            )
        capture_macos_primary_display(
            destination,
            expected_physical_size=physical_size,
            expected_scale=(
                snapshot.viewport_geometry.scale_x,
                snapshot.viewport_geometry.scale_y,
            ),
            expected_device_pixel_ratio=snapshot.device_pixel_ratio,
            validate_scale_dpr=self._validate_capture_scale_dpr,
        )
        self._validate_post_capture(snapshot)

    def _validate_post_capture(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        validator = self._post_capture_validator
        if validator is None:
            return
        try:
            result = validator(snapshot)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="macOS post-capture validation failed",
            ) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RuntimeError("macOS post-capture validation failed") from None
        if result is not None:
            raise CaptureEnvironmentUnavailable(
                "macOS post-capture validator returned invalid data"
            )


class MacOSEvidenceCapture:
    def __init__(
        self,
        environment: MacOSCaptureEnvironment | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> None:
        validate_mac_capture_policy(
            policy,
            platform_name=host_platform.system(),
        )
        self._pipeline = EvidenceCapturePipeline(
            environment or MacOSCaptureEnvironment(),
            sleeper=sleeper or time.sleep,
            now=now,
            policy=policy,
        )

    @property
    def policy(self) -> MacCapturePolicy:
        return self._pipeline.policy

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        return self._pipeline.capture(request)


def select_mss_primary_logical_monitor(
    monitors: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Select the real main monitor, never the aggregate virtual monitor."""
    if not isinstance(monitors, Sequence) or len(monitors) < 2:
        raise GeometryError("mss did not expose a physical monitor")
    candidates: list[Mapping[str, Any]] = []
    for monitor in monitors[1:]:
        if not isinstance(monitor, Mapping):
            raise GeometryError("mss monitor metadata is invalid")
        values = tuple(
            monitor.get(name) for name in ("left", "top", "width", "height")
        )
        if any(type(value) is not int for value in values):
            raise GeometryError("mss monitor metadata is invalid")
        left, top, width, height = tuple(
            cast(int, value) for value in values
        )
        if width <= 0 or height <= 0:
            raise GeometryError("mss monitor dimensions are invalid")
        if left == 0 and top == 0:
            candidates.append(monitor)
    if len(candidates) != 1:
        raise GeometryError("mss main logical monitor cannot be proven")
    return candidates[0]


def capture_macos_primary_display(
    destination: Path,
    *,
    expected_physical_size: tuple[int, int],
    expected_scale: tuple[float, float],
    expected_device_pixel_ratio: float,
    validate_scale_dpr: bool = True,
    mss_factory: Callable[[], Any] | None = None,
    png_writer: Callable[..., Any] | None = None,
    darwin_module: Any | None = None,
) -> None:
    """Capture the logical Quartz monitor and require physical Retina pixels."""
    if (
        not isinstance(expected_physical_size, tuple)
        or len(expected_physical_size) != 2
        or any(
            type(value) is not int or value <= 0
            for value in expected_physical_size
        )
    ):
        raise ValueError("expected_physical_size must be positive integers")
    if (
        not isinstance(expected_scale, tuple)
        or len(expected_scale) != 2
        or any(
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in expected_scale
        )
        or not isinstance(expected_device_pixel_ratio, int | float)
        or isinstance(expected_device_pixel_ratio, bool)
        or not math.isfinite(expected_device_pixel_ratio)
        or expected_device_pixel_ratio <= 0
    ):
        raise ValueError("expected scale and DPR must be finite and positive")
    if mss_factory is None or png_writer is None or darwin_module is None:
        try:
            import mss
            import mss.darwin as native_darwin
            from mss import tools as mss_tools
        except ImportError:
            raise CaptureEnvironmentUnavailable(
                "mss macOS capture dependency is unavailable"
            ) from None

        mss_factory = mss.mss
        png_writer = mss_tools.to_png
        darwin_module = native_darwin

    with _MSS_DARWIN_OPTIONS_LOCK:
        if not hasattr(darwin_module, "IMAGE_OPTIONS"):
            raise CaptureEnvironmentUnavailable(
                "mss darwin image options are unavailable"
            )
        previous_options = getattr(darwin_module, "IMAGE_OPTIONS")
        setattr(darwin_module, "IMAGE_OPTIONS", 0)
        try:
            with mss_factory() as screenshotter:
                monitor = select_mss_primary_logical_monitor(
                    screenshotter.monitors
                )
                screenshot = screenshotter.grab(dict(monitor))
        finally:
            setattr(darwin_module, "IMAGE_OPTIONS", previous_options)

    try:
        actual_size = tuple(screenshot.size)
    except (AttributeError, TypeError) as error:
        raise GeometryError("mss screenshot size is unavailable") from error
    if actual_size != expected_physical_size:
        raise GeometryError(
            "mss screenshot is not the main display physical pixel size"
        )
    logical_width = int(monitor["width"])
    logical_height = int(monitor["height"])
    observed_scale = (
        expected_physical_size[0] / logical_width,
        expected_physical_size[1] / logical_height,
    )
    if validate_scale_dpr:
        for observed, expected in zip(
            observed_scale,
            expected_scale,
            strict=True,
        ):
            if not math.isclose(
                observed,
                expected,
                rel_tol=0.03,
                abs_tol=0.03,
            ):
                raise GeometryError(
                    "mss logical-to-physical scale disagrees with geometry snapshot"
                )
        if any(
            not math.isclose(
                observed,
                expected_device_pixel_ratio,
                rel_tol=0.03,
                abs_tol=0.03,
            )
            for observed in observed_scale
        ):
            raise GeometryError(
                "mss logical-to-physical scale disagrees with JavaScript DPR"
            )
    png_writer(
        screenshot.rgb,
        screenshot.size,
        output=str(destination),
    )


def _macos_main_display_physical_size() -> tuple[int, int]:
    try:
        import ctypes
        import AppKit  # type: ignore[import-untyped]

        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        core_graphics.CGMainDisplayID.argtypes = []
        core_graphics.CGMainDisplayID.restype = ctypes.c_uint32
        display_id = core_graphics.CGMainDisplayID()
        core_graphics.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        core_graphics.CGDisplayPixelsWide.restype = ctypes.c_size_t
        core_graphics.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        core_graphics.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        size = (
            int(core_graphics.CGDisplayPixelsWide(display_id)),
            int(core_graphics.CGDisplayPixelsHigh(display_id)),
        )
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            raise AttributeError("macOS main screen is unavailable")
        scale = float(screen.backingScaleFactor())
        if not math.isfinite(scale) or scale <= 0:
            raise AttributeError("macOS main screen backing scale is invalid")
        size = (round(size[0] * scale), round(size[1] * scale))
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise CaptureEnvironmentUnavailable(
            "cannot prove macOS main display physical size"
        ) from error
    if size[0] <= 0 or size[1] <= 0:
        raise CaptureEnvironmentUnavailable(
            "macOS main display physical size is invalid"
        )
    return size

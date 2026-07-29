from __future__ import annotations

import hashlib
import math
import os
import platform as host_platform
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PIL import Image

from quote_app.evidence.annotations import (
    AnnotationError,
    draw_red_annotations,
    validate_annotation_roles,
)
from quote_app.evidence.chromium import (
    CdpWebAreaSample,
    ChromiumWindowSample,
)
from quote_app.evidence.geometry import (
    CssRect,
    DisplayBounds,
    GeometryError,
    ScreenRect,
    ViewportGeometry,
    css_to_capture_rect,
    validate_screen_rect,
)
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceState,
    MacCapturePolicy,
    validate_mac_capture_policy,
)
from quote_app.evidence.quality import (
    CaptureQualityError,
    SemanticHashProbe,
    assess_capture_quality,
    wait_for_stable_hash,
)
from quote_app.tasks.retry import (
    NonRetryableTechnicalError,
    RetryableTechnicalError,
    TechnicalError,
    credential_free_error_message,
)

if TYPE_CHECKING:
    from quote_app.evidence.macos_binding import BoundMacWindow

_RETRYABLE_CAPTURE_CODES = frozenset(
    {
        "CAPTURE_BLANK",
        "CAPTURE_UNREADABLE",
        "CAPTURE_OBSCURED",
        "CAPTURE_UNSTABLE",
        "CAPTURE_GEOMETRY",
        "CAPTURE_FAILED",
    }
)
_NON_RETRYABLE_CAPTURE_CODES = frozenset(
    {
        "CAPTURE_PERMISSION",
        "CAPTURE_ENVIRONMENT",
        "CAPTURE_ACCESSIBILITY",
        "CAPTURE_WINDOW_IDENTITY",
        "CAPTURE_FOREGROUND",
        "CAPTURE_SYSTEM_UI",
    }
)
_CAPTURE_CODES = _RETRYABLE_CAPTURE_CODES | _NON_RETRYABLE_CAPTURE_CODES
_SAFE_ACCESSIBILITY_DIAGNOSTICS = frozenset(
    {
        "WEB_AREA_NOT_FOUND",
        "WEB_AREA_NOT_UNIQUE",
        "WEB_AREA_IDENTITY_MISMATCH",
        "WEB_AREA_TIMEOUT",
        "WEB_AREA_NATIVE_ERROR",
    }
)
_GEOMETRY_TOLERANCE_PX = 2.0
_SCALE_TOLERANCE = 0.03
_CDP_PROOF_MAX_AGE_SECONDS = 1.0


class EvidenceCaptureError(TechnicalError):
    """Common typed base for stable evidence-capture failures."""


class RetryableEvidenceCaptureError(
    RetryableTechnicalError,
    EvidenceCaptureError,
):
    """A transient capture failure eligible for bounded retry."""


class NonRetryableEvidenceCaptureError(
    NonRetryableTechnicalError,
    EvidenceCaptureError,
):
    """A fixed environment failure requiring intervention."""


class CaptureEnvironmentUnavailable(RuntimeError):
    """A required native or authoritative injected capability is unavailable."""


class NativeScreenPermissionDenied(PermissionError):
    """The native screen API explicitly denied access to screen pixels."""


def make_capture_error(code: str, message: str) -> EvidenceCaptureError:
    if code not in _CAPTURE_CODES:
        raise ValueError(f"unsupported capture error code: {code}")
    safe_message = credential_free_error_message(code, message)
    error_type: type[EvidenceCaptureError]
    if code in _RETRYABLE_CAPTURE_CODES:
        error_type = RetryableEvidenceCaptureError
    else:
        error_type = NonRetryableEvidenceCaptureError
    return error_type(code, safe_message)


def revalidate_capture_error(
    error: EvidenceCaptureError,
    *,
    fallback_code: str,
    safe_message: str,
) -> EvidenceCaptureError:
    """Rebuild an injected capture error through the trusted allowlist."""
    try:
        message = safe_message
        if (
            error.code == "CAPTURE_ACCESSIBILITY"
            and error.message in _SAFE_ACCESSIBILITY_DIAGNOSTICS
        ):
            message = error.message
        return make_capture_error(error.code, message)
    except (AttributeError, TypeError, ValueError):
        return make_capture_error(fallback_code, safe_message)


@dataclass(frozen=True, slots=True)
class BrowserWindowIdentity:
    platform: str
    process_id: int
    window_handle: str

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str) or not self.platform.strip():
            raise ValueError("platform must not be blank")
        if type(self.process_id) is not int or self.process_id <= 0:
            raise ValueError("process_id must be a positive integer")
        if (
            not isinstance(self.window_handle, str)
            or not self.window_handle.strip()
        ):
            raise ValueError("window_handle must not be blank")
        object.__setattr__(self, "platform", self.platform.strip().lower())
        object.__setattr__(self, "window_handle", self.window_handle.strip())


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """Runner-supplied platform capabilities required for formal capture."""

    expected_window: BrowserWindowIdentity
    stability_probe: SemanticHashProbe

    def __post_init__(self) -> None:
        if not isinstance(self.expected_window, BrowserWindowIdentity):
            raise ValueError(
                "expected_window must be a BrowserWindowIdentity"
            )
        if not callable(getattr(self.stability_probe, "semantic_hash", None)):
            raise ValueError("stability_probe must provide semantic_hash")


@dataclass(frozen=True, slots=True)
class CaptureGeometrySnapshot:
    """One immutable, unit-labelled sample used for an entire capture."""

    expected_window: BrowserWindowIdentity
    display_physical_bounds: DisplayBounds
    browser_physical_bounds: DisplayBounds
    viewport_geometry: ViewportGeometry
    viewport_width_css: float
    viewport_height_css: float
    maximized: bool
    fullscreen: bool
    device_pixel_ratio: float | None = None
    dpi_x: float | None = None
    dpi_y: float | None = None
    sample_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expected_window, BrowserWindowIdentity):
            raise ValueError(
                "expected_window must be a BrowserWindowIdentity"
            )
        if not isinstance(self.display_physical_bounds, DisplayBounds):
            raise ValueError("display_physical_bounds must be DisplayBounds")
        if not isinstance(self.browser_physical_bounds, DisplayBounds):
            raise ValueError("browser_physical_bounds must be DisplayBounds")
        if not isinstance(self.viewport_geometry, ViewportGeometry):
            raise ValueError("viewport_geometry must be ViewportGeometry")
        _validate_positive_finite(
            self.viewport_width_css,
            "viewport_width_css",
        )
        _validate_positive_finite(
            self.viewport_height_css,
            "viewport_height_css",
        )
        if type(self.maximized) is not bool or type(self.fullscreen) is not bool:
            raise ValueError("maximized and fullscreen must be booleans")
        for name, value in (
            ("device_pixel_ratio", self.device_pixel_ratio),
            ("dpi_x", self.dpi_x),
            ("dpi_y", self.dpi_y),
        ):
            if value is not None:
                _validate_positive_finite(value, name)
        if self.sample_id is not None and (
            not isinstance(self.sample_id, str) or not self.sample_id.strip()
        ):
            raise ValueError("sample_id must be a nonblank string when set")


def make_cdp_web_area_geometry_snapshot(
    *,
    expected_window: BrowserWindowIdentity,
    chromium: ChromiumWindowSample,
    browser_physical_bounds: DisplayBounds,
    display_physical_bounds: DisplayBounds,
    sample: CdpWebAreaSample,
    now_monotonic: float,
) -> CaptureGeometrySnapshot:
    """Cross-check a geometry-only CDP sample against the bound Mac window."""
    if (
        not isinstance(expected_window, BrowserWindowIdentity)
        or expected_window.platform not in {"darwin", "macos"}
    ):
        raise GeometryError("CDP web-area proof requires a macOS window")
    if not isinstance(chromium, ChromiumWindowSample):
        raise GeometryError("invalid bound Chromium sample")
    if not isinstance(sample, CdpWebAreaSample):
        raise GeometryError("invalid CDP web-area sample")
    if (
        sample.browser_pid != expected_window.process_id
        or chromium.browser_pid != expected_window.process_id
    ):
        raise GeometryError("CDP web-area PID does not match bound window")
    if not isinstance(browser_physical_bounds, DisplayBounds) or not isinstance(
        display_physical_bounds,
        DisplayBounds,
    ):
        raise GeometryError("CDP web-area bounds are invalid")
    if not math.isclose(
        sample.device_pixel_ratio,
        chromium.device_pixel_ratio,
        abs_tol=1e-9,
        rel_tol=1e-9,
    ):
        raise GeometryError("CDP web-area DPR does not match Chromium")
    if (
        sample.layout_page_x_css != chromium.layout_page_x_css
        or sample.layout_page_y_css != chromium.layout_page_y_css
    ):
        raise GeometryError(
            "CDP web-area layout offsets do not match Chromium"
        )
    for actual, expected, name in (
        (
            sample.viewport_width_css,
            chromium.viewport_width_css,
            "viewport width",
        ),
        (
            sample.viewport_height_css,
            chromium.viewport_height_css,
            "viewport height",
        ),
        (
            sample.layout_viewport_width_css,
            chromium.viewport_width_css,
            "layout viewport width",
        ),
        (
            sample.layout_viewport_height_css,
            chromium.viewport_height_css,
            "layout viewport height",
        ),
    ):
        if not math.isclose(actual, expected, abs_tol=1e-9, rel_tol=1e-9):
            raise GeometryError(
                f"CDP web-area {name} does not match Chromium"
            )
    horizontal_frame = sample.outer_width_css - sample.viewport_width_css
    vertical_frame = sample.outer_height_css - sample.viewport_height_css
    if not math.isclose(horizontal_frame % 2.0, 0.0, abs_tol=1e-9):
        raise GeometryError("CDP web-area horizontal frame is not even")
    if vertical_frame <= 0.0:
        raise GeometryError("CDP web-area vertical frame lacks browser chrome")
    content_origin_x = sample.client_origin_x_dip + horizontal_frame / 2.0
    content_origin_y = sample.client_origin_y_dip + vertical_frame
    if content_origin_y <= chromium.bounds_dip.y:
        raise GeometryError(
            "CDP web-area content origin is not below browser chrome"
        )
    for physical, dip, name in (
        (browser_physical_bounds.x, chromium.bounds_dip.x, "window X"),
        (browser_physical_bounds.y, chromium.bounds_dip.y, "window Y"),
        (
            browser_physical_bounds.width,
            chromium.bounds_dip.width,
            "window width",
        ),
        (
            browser_physical_bounds.height,
            chromium.bounds_dip.height,
            "window height",
        ),
    ):
        if (
            abs(physical - dip * chromium.device_pixel_ratio)
            > _GEOMETRY_TOLERANCE_PX
        ):
            raise GeometryError(
                f"CDP web-area {name} does not match native window"
            )
    _validate_extent_inside(
        content_origin_x * sample.device_pixel_ratio,
        content_origin_y * sample.device_pixel_ratio,
        (content_origin_x + sample.viewport_width_css)
        * sample.device_pixel_ratio,
        (content_origin_y + sample.viewport_height_css)
        * sample.device_pixel_ratio,
        browser_physical_bounds,
        "browser",
    )
    if not (
        math.isclose(
            sample.client_origin_x_dip,
            chromium.bounds_dip.x,
            abs_tol=1e-9,
            rel_tol=1e-9,
        )
        and math.isclose(
            sample.client_origin_y_dip,
            chromium.bounds_dip.y,
            abs_tol=1e-9,
            rel_tol=1e-9,
        )
    ):
        raise GeometryError(
            "CDP web-area outer origin does not match Chromium window"
        )
    for actual, expected, name in (
        (sample.outer_width_css, chromium.bounds_dip.width, "outer width"),
        (sample.outer_height_css, chromium.bounds_dip.height, "outer height"),
    ):
        if not math.isclose(actual, expected, abs_tol=1e-9, rel_tol=1e-9):
            raise GeometryError(
                f"CDP web-area {name} does not match Chromium window"
            )

    now = _nonnegative_finite(now_monotonic, "now_monotonic")
    timestamps = (
        _nonnegative_finite(
            chromium.sampled_at_monotonic,
            "Chromium sampled_at_monotonic",
        ),
        _nonnegative_finite(
            sample.sampled_at_monotonic,
            "CDP sampled_at_monotonic",
        ),
    )
    if any(
        timestamp > now or now - timestamp > _CDP_PROOF_MAX_AGE_SECONDS
        for timestamp in timestamps
    ) or max(timestamps) - min(timestamps) > _CDP_PROOF_MAX_AGE_SECONDS:
        raise GeometryError("CDP web-area samples are stale or incoherent")

    _validate_extent_inside(
        float(browser_physical_bounds.x),
        float(browser_physical_bounds.y),
        float(browser_physical_bounds.x + browser_physical_bounds.width),
        float(browser_physical_bounds.y + browser_physical_bounds.height),
        display_physical_bounds,
        "display",
    )
    digest = hashlib.sha256()
    for value in (
        chromium.sample_id,
        sample.sample_id,
        expected_window.window_handle,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    snapshot = CaptureGeometrySnapshot(
        expected_window=expected_window,
        display_physical_bounds=display_physical_bounds,
        browser_physical_bounds=browser_physical_bounds,
        viewport_geometry=ViewportGeometry(
            client_origin_x_dip=content_origin_x,
            client_origin_y_dip=content_origin_y,
            scale_x=sample.device_pixel_ratio,
            scale_y=sample.device_pixel_ratio,
            capture_origin_x_px=display_physical_bounds.x,
            capture_origin_y_px=display_physical_bounds.y,
        ),
        viewport_width_css=sample.viewport_width_css,
        viewport_height_css=sample.viewport_height_css,
        maximized=chromium.maximized,
        fullscreen=chromium.fullscreen,
        device_pixel_ratio=sample.device_pixel_ratio,
        sample_id=digest.hexdigest(),
    )
    _validate_geometry_snapshot(snapshot, expected_window)
    return snapshot


def make_macos_full_display_geometry_snapshot(
    bound: BoundMacWindow,
    display_physical_bounds: DisplayBounds,
    *,
    now_monotonic: float,
) -> CaptureGeometrySnapshot:
    """Prove a bound Mac window supports a full-display capture only.

    The geometry fields below describe the native browser frame and capture
    scale.  They deliberately do not identify a web-content origin, so beta
    callers must not use this snapshot for CSS-to-screen coordinate mapping.
    """
    from quote_app.evidence.macos_binding import BoundMacWindow as RuntimeBoundMacWindow

    if not isinstance(bound, RuntimeBoundMacWindow):
        raise GeometryError("full-display proof requires a bound macOS window")
    if not isinstance(display_physical_bounds, DisplayBounds):
        raise GeometryError("full-display proof requires display bounds")

    identity = bound.identity
    chromium = bound.chromium
    native = bound.native
    if (
        identity.platform not in {"darwin", "macos"}
        or identity.process_id != chromium.browser_pid
        or identity.process_id != native.process_id
        or identity.window_handle != str(native.window_id)
    ):
        raise GeometryError("bound macOS window identity is inconsistent")
    if (
        not native.on_screen
        or native.minimized
        or native.layer != 0
        or chromium.maximized
        or chromium.fullscreen
    ):
        raise GeometryError("macOS browser must be a normal visible window")

    now = _nonnegative_finite(now_monotonic, "now_monotonic")
    timestamps = (
        _nonnegative_finite(
            chromium.sampled_at_monotonic,
            "Chromium sampled_at_monotonic",
        ),
        _nonnegative_finite(
            native.sampled_at_monotonic,
            "native sampled_at_monotonic",
        ),
    )
    if any(timestamp > now or now - timestamp > _CDP_PROOF_MAX_AGE_SECONDS for timestamp in timestamps) or (
        max(timestamps) - min(timestamps) > _CDP_PROOF_MAX_AGE_SECONDS
    ):
        raise GeometryError("full-display samples are stale or incoherent")

    for physical, dip, name in (
        (native.bounds_px.x, chromium.bounds_dip.x, "window X"),
        (native.bounds_px.y, chromium.bounds_dip.y, "window Y"),
        (native.bounds_px.width, chromium.bounds_dip.width, "window width"),
        (native.bounds_px.height, chromium.bounds_dip.height, "window height"),
    ):
        if abs(physical - dip * chromium.device_pixel_ratio) > _GEOMETRY_TOLERANCE_PX:
            raise GeometryError(
                f"full-display {name} does not match native window"
            )
    _validate_extent_inside(
        float(native.bounds_px.x),
        float(native.bounds_px.y),
        float(native.bounds_px.x + native.bounds_px.width),
        float(native.bounds_px.y + native.bounds_px.height),
        display_physical_bounds,
        "display",
    )

    digest = hashlib.sha256()
    for value in (chromium.sample_id, native.sample_id, identity.window_handle):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    snapshot = CaptureGeometrySnapshot(
        expected_window=identity,
        display_physical_bounds=display_physical_bounds,
        browser_physical_bounds=native.bounds_px,
        viewport_geometry=ViewportGeometry(
            client_origin_x_dip=chromium.bounds_dip.x,
            client_origin_y_dip=chromium.bounds_dip.y,
            scale_x=chromium.device_pixel_ratio,
            scale_y=chromium.device_pixel_ratio,
            capture_origin_x_px=display_physical_bounds.x,
            capture_origin_y_px=display_physical_bounds.y,
        ),
        viewport_width_css=chromium.bounds_dip.width,
        viewport_height_css=chromium.bounds_dip.height,
        maximized=False,
        fullscreen=False,
        device_pixel_ratio=chromium.device_pixel_ratio,
        sample_id=digest.hexdigest(),
    )
    _validate_geometry_snapshot(snapshot, identity)
    return snapshot


@dataclass(frozen=True, slots=True)
class SystemUIProof:
    """Authoritative platform proof that required system chrome is visible."""

    expected_window: BrowserWindowIdentity
    system_bar_visible: bool
    date_time_visible: bool
    intersects_primary_display: bool
    authoritative: bool
    source: str
    visual_review_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.expected_window, BrowserWindowIdentity):
            raise ValueError(
                "expected_window must be a BrowserWindowIdentity"
            )
        if any(
            type(value) is not bool
            for value in (
                self.system_bar_visible,
                self.date_time_visible,
                self.intersects_primary_display,
                self.authoritative,
                self.visual_review_required,
            )
        ):
            raise ValueError("system UI proof flags must be booleans")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("system UI proof source must not be blank")


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    destination: Path
    state: EvidenceState
    css_rectangles: tuple[CssRect, ...]
    expected_roles: tuple[str, ...]
    expected_window: BrowserWindowIdentity
    stability_probe: SemanticHashProbe
    minimum_stability_interval_seconds: float = 0.15

    def __post_init__(self) -> None:
        if not isinstance(self.destination, Path):
            raise ValueError("destination must be a Path")
        if not isinstance(self.state, EvidenceState):
            raise ValueError("state must be an EvidenceState")
        if not isinstance(self.css_rectangles, tuple | list) or not all(
            isinstance(rectangle, CssRect)
            for rectangle in self.css_rectangles
        ):
            raise ValueError("css_rectangles must contain CssRect values")
        if not isinstance(self.expected_roles, tuple | list) or not all(
            isinstance(role, str) and role.strip()
            for role in self.expected_roles
        ):
            raise ValueError("expected_roles must contain nonblank strings")
        if not isinstance(self.expected_window, BrowserWindowIdentity):
            raise ValueError(
                "expected_window must be a BrowserWindowIdentity"
            )
        if not callable(getattr(self.stability_probe, "semantic_hash", None)):
            raise ValueError("stability_probe must provide semantic_hash")
        _validate_positive_finite(
            self.minimum_stability_interval_seconds,
            "minimum_stability_interval_seconds",
        )
        object.__setattr__(
            self,
            "destination",
            Path(os.path.abspath(os.path.normpath(self.destination))),
        )
        object.__setattr__(self, "css_rectangles", tuple(self.css_rectangles))
        object.__setattr__(
            self,
            "expected_roles",
            tuple(role.strip() for role in self.expected_roles),
        )


class CaptureEnvironment(Protocol):
    def screen_capture_permission(self) -> bool: ...

    def prepare_browser(
        self,
        expected: BrowserWindowIdentity,
    ) -> object | None: ...

    def geometry_snapshot(
        self,
        expected: BrowserWindowIdentity,
    ) -> CaptureGeometrySnapshot: ...

    def system_ui_proof(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof: ...

    def foreground_window(self) -> BrowserWindowIdentity: ...

    def capture_primary_display(
        self,
        destination: Path,
        snapshot: CaptureGeometrySnapshot,
    ) -> object | None: ...


class PlatformEvidenceCapture(Protocol):
    def capture(self, request: CaptureRequest) -> EvidenceRecord: ...


class EvidenceCapturePipeline:
    """Fail-closed, atomic, full-display evidence capture pipeline."""

    def __init__(
        self,
        environment: CaptureEnvironment,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> None:
        validate_mac_capture_policy(
            policy,
            platform_name=host_platform.system(),
        )
        self.environment = environment
        self.sleeper = sleeper
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._policy = policy

    @property
    def policy(self) -> MacCapturePolicy:
        """The construction-selected capture policy (read-only)."""
        return self._policy

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        if not isinstance(request, CaptureRequest):
            raise ValueError("request must be a CaptureRequest")
        policy = self._capture_policy()
        destination = request.destination
        temporary_path: Path | None = None
        try:
            self._require_capture_permission()
            self._prepare_browser(request.expected_window)
            snapshot = self._geometry_snapshot(request.expected_window)
            _validate_geometry_snapshot(snapshot, request.expected_window)
            proof = self._system_ui_proof(snapshot)
            _validate_system_ui_proof(
                proof,
                snapshot.expected_window,
                policy=policy,
            )

            if not _same_window(
                _foreground_window(self.environment),
                request.expected_window,
            ):
                raise make_capture_error(
                    "CAPTURE_OBSCURED",
                    "截图前浏览器不是预期前台窗口",
                )
            wait_for_stable_hash(
                request.stability_probe,
                minimum_interval_seconds=(
                    request.minimum_stability_interval_seconds
                ),
                max_checks=3,
                sleeper=self.sleeper,
            )

            display = snapshot.display_physical_bounds
            screen_rectangles: tuple[ScreenRect, ...] = ()
            if policy is MacCapturePolicy.STRICT:
                screen_rectangles = tuple(
                    css_to_capture_rect(
                        rectangle,
                        snapshot.viewport_geometry,
                    )
                    for rectangle in request.css_rectangles
                )
                screen_rectangles = tuple(
                    validate_screen_rect(
                        rectangle,
                        image_width=display.width,
                        image_height=display.height,
                    )
                    for rectangle in screen_rectangles
                )
                validate_annotation_roles(
                    request.state,
                    screen_rectangles,
                    expected_roles=request.expected_roles,
                )

            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, raw_path = tempfile.mkstemp(
                    prefix=f".{destination.stem}.",
                    suffix=".png",
                    dir=destination.parent,
                )
                os.close(descriptor)
            except OSError as error:
                raise make_capture_error(
                    "CAPTURE_ENVIRONMENT",
                    "证据输出目录不可用",
                ) from error
            temporary_path = Path(raw_path)
            self._capture_display(temporary_path, snapshot)

            if not _same_window(
                _foreground_window(self.environment),
                request.expected_window,
            ):
                raise make_capture_error(
                    "CAPTURE_OBSCURED",
                    "截图后浏览器不再是预期前台窗口",
                )
            image = _load_capture(temporary_path)
            assess_capture_quality(
                image,
                expected_size=(display.width, display.height),
            )
            annotations = (
                draw_red_annotations(
                    image,
                    request.state,
                    screen_rectangles,
                )
                if policy is MacCapturePolicy.STRICT
                else ()
            )
            try:
                image.save(
                    temporary_path,
                    format="PNG",
                    optimize=False,
                    compress_level=1,
                )
                _fsync_file(temporary_path)
                sha256 = _sha256(temporary_path)
            except OSError as error:
                raise make_capture_error(
                    "CAPTURE_FAILED",
                    "截图文件处理暂时失败",
                ) from error
            record = EvidenceRecord(
                state=request.state,
                path=destination,
                sha256=sha256,
                pixel_width=image.width,
                pixel_height=image.height,
                captured_at=self.now(),
                validation_code=_capture_validation_code(policy),
                annotations=annotations,
            )
            try:
                os.replace(temporary_path, destination)
            except OSError as error:
                raise make_capture_error(
                    "CAPTURE_ENVIRONMENT",
                    "证据文件无法原子发布",
                ) from error
            temporary_path = None
            return record
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="正式截图流程失败",
            ) from None
        except CaptureQualityError as error:
            raise make_capture_error(error.code, error.message) from None
        except (GeometryError, AnnotationError) as error:
            raise make_capture_error(
                "CAPTURE_GEOMETRY",
                "截图坐标或业务标注超出有效范围",
            ) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "操作系统整屏截图环境不可用",
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _capture_policy(self) -> MacCapturePolicy:
        policy = self._policy
        try:
            validate_mac_capture_policy(
                policy,
                platform_name=host_platform.system(),
            )
        except ValueError as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "截图策略无效或不适用于当前操作系统",
            ) from error
        return policy

    def _require_capture_permission(self) -> None:
        try:
            allowed = self.environment.screen_capture_permission()
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="屏幕录制权限检查失败",
            ) from None
        except CaptureEnvironmentUnavailable as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "屏幕录制权限检查能力不可用",
            ) from error
        except PermissionError as error:
            raise make_capture_error(
                "CAPTURE_PERMISSION",
                "操作系统拒绝检查屏幕录制权限",
            ) from error
        except Exception:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "屏幕录制权限检查失败",
            ) from None
        if type(allowed) is not bool:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "屏幕录制权限检查返回值无效",
            )
        if not allowed:
            raise make_capture_error(
                "CAPTURE_PERMISSION",
                "操作系统未授予屏幕录制权限",
            )

    def _prepare_browser(self, expected: BrowserWindowIdentity) -> None:
        try:
            result = self.environment.prepare_browser(expected)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="浏览器前台控制失败",
            ) from None
        except CaptureEnvironmentUnavailable as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "浏览器前台控制能力在当前环境不可用",
            ) from error
        except PermissionError as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "浏览器前台控制被操作系统拒绝",
            ) from error
        except (OSError, RuntimeError) as error:
            raise make_capture_error(
                "CAPTURE_OBSCURED",
                "浏览器暂时无法切换到预期前台窗口",
            ) from error
        except Exception:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "浏览器前台控制返回异常",
            ) from None
        if result is not None:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "浏览器前台控制返回值无效",
            )

    def _geometry_snapshot(
        self,
        expected: BrowserWindowIdentity,
    ) -> CaptureGeometrySnapshot:
        try:
            snapshot = self.environment.geometry_snapshot(expected)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="截图几何快照提供器失败",
            ) from None
        except CaptureEnvironmentUnavailable as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "权威截图几何能力在当前环境不可用",
            ) from error
        except GeometryError as error:
            raise make_capture_error(
                "CAPTURE_GEOMETRY",
                "截图几何快照校验失败",
            ) from error
        except Exception:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "截图几何快照提供器异常",
            ) from None
        if not isinstance(snapshot, CaptureGeometrySnapshot):
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "截图几何快照返回值无效",
            )
        return snapshot

    def _system_ui_proof(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof:
        try:
            proof = self.environment.system_ui_proof(snapshot)
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="系统栏可见性证明提供器失败",
            ) from None
        except CaptureEnvironmentUnavailable as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "系统栏可见性证明能力不可用",
            ) from error
        except Exception:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "系统栏可见性证明提供器异常",
            ) from None
        if not isinstance(proof, SystemUIProof):
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "系统栏可见性证明返回值无效",
            )
        return proof

    def _capture_display(
        self,
        temporary_path: Path,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        try:
            result = self.environment.capture_primary_display(
                temporary_path,
                snapshot,
            )
        except EvidenceCaptureError as error:
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="操作系统整屏截图提供器失败",
            ) from None
        except NativeScreenPermissionDenied as error:
            # At this boundary PermissionError means the OS capture API denied
            # screen pixels. File PermissionError remains CAPTURE_FAILED.
            raise make_capture_error(
                "CAPTURE_PERMISSION",
                "操作系统拒绝屏幕录制",
            ) from error
        except GeometryError as error:
            raise make_capture_error(
                "CAPTURE_GEOMETRY",
                "整屏截图像素尺寸与主屏幕不一致",
            ) from error
        except CaptureEnvironmentUnavailable as error:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "整屏截图能力不可用",
            ) from error
        except (OSError, RuntimeError) as error:
            raise make_capture_error(
                "CAPTURE_FAILED",
                "操作系统整屏截图暂时失败",
            ) from error
        except Exception:
            raise make_capture_error(
                "CAPTURE_FAILED",
                "操作系统整屏截图返回异常",
            ) from None
        if result is not None:
            raise make_capture_error(
                "CAPTURE_FAILED",
                "操作系统整屏截图返回值无效",
            )


def capture_display_with_mss(
    destination: Path,
    bounds: DisplayBounds,
) -> None:
    """Capture a physical-coordinate display (Windows after DPI verification)."""
    try:
        import mss
        from mss import tools as mss_tools
    except ImportError:
        raise CaptureEnvironmentUnavailable(
            "mss screen capture dependency is unavailable"
        ) from None

    monitor = {
        "left": bounds.x,
        "top": bounds.y,
        "width": bounds.width,
        "height": bounds.height,
    }
    with mss.mss() as screenshotter:
        screenshot = screenshotter.grab(monitor)
        if tuple(screenshot.size) != (bounds.width, bounds.height):
            raise GeometryError("captured pixels do not match display bounds")
        mss_tools.to_png(
            screenshot.rgb,
            screenshot.size,
            output=str(destination),
        )


def _foreground_window(
    environment: CaptureEnvironment,
) -> BrowserWindowIdentity:
    try:
        actual = environment.foreground_window()
    except EvidenceCaptureError as error:
        raise revalidate_capture_error(
            error,
            fallback_code="CAPTURE_ENVIRONMENT",
            safe_message="前台窗口识别提供器失败",
        ) from None
    except CaptureEnvironmentUnavailable as error:
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "前台窗口识别能力在当前环境不可用",
        ) from error
    except (OSError, RuntimeError) as error:
        raise make_capture_error(
            "CAPTURE_OBSCURED",
            "暂时无法确认浏览器前台窗口",
        ) from error
    except Exception:
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "前台窗口识别提供器异常",
        ) from None
    if not isinstance(actual, BrowserWindowIdentity):
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "前台窗口识别返回值无效",
        )
    return actual


def _validate_geometry_snapshot(
    snapshot: CaptureGeometrySnapshot,
    expected: BrowserWindowIdentity,
) -> None:
    if not _same_window(snapshot.expected_window, expected):
        raise GeometryError("geometry snapshot belongs to a stale browser window")
    display = snapshot.display_physical_bounds
    browser = snapshot.browser_physical_bounds
    geometry = snapshot.viewport_geometry
    if snapshot.expected_window.platform == "macos":
        if snapshot.maximized or snapshot.fullscreen:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "macOS 浏览器必须为普通窗口且不能处于全屏模式",
            )
    else:
        if not snapshot.maximized or snapshot.fullscreen:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "浏览器必须最大化且不能处于全屏模式",
            )
        _validate_browser_coverage(display, browser)
    if (
        not math.isclose(
            geometry.capture_origin_x_px,
            display.x,
            abs_tol=_GEOMETRY_TOLERANCE_PX,
        )
        or not math.isclose(
            geometry.capture_origin_y_px,
            display.y,
            abs_tol=_GEOMETRY_TOLERANCE_PX,
        )
    ):
        raise GeometryError(
            "viewport capture origin does not match primary display"
        )
    if snapshot.device_pixel_ratio is not None and (
        not math.isclose(
            geometry.scale_x,
            snapshot.device_pixel_ratio,
            abs_tol=_SCALE_TOLERANCE,
            rel_tol=_SCALE_TOLERANCE,
        )
        or not math.isclose(
            geometry.scale_y,
            snapshot.device_pixel_ratio,
            abs_tol=_SCALE_TOLERANCE,
            rel_tol=_SCALE_TOLERANCE,
        )
    ):
        raise GeometryError("viewport scale and device pixel ratio disagree")
    if snapshot.dpi_x is not None and not math.isclose(
        geometry.scale_x,
        snapshot.dpi_x / 96.0,
        abs_tol=_SCALE_TOLERANCE,
        rel_tol=_SCALE_TOLERANCE,
    ):
        raise GeometryError("viewport x scale and DPI disagree")
    if snapshot.dpi_y is not None and not math.isclose(
        geometry.scale_y,
        snapshot.dpi_y / 96.0,
        abs_tol=_SCALE_TOLERANCE,
        rel_tol=_SCALE_TOLERANCE,
    ):
        raise GeometryError("viewport y scale and DPI disagree")

    viewport_left = geometry.client_origin_x_dip * geometry.scale_x
    viewport_top = geometry.client_origin_y_dip * geometry.scale_y
    viewport_right = (
        geometry.client_origin_x_dip + snapshot.viewport_width_css
    ) * geometry.scale_x
    viewport_bottom = (
        geometry.client_origin_y_dip + snapshot.viewport_height_css
    ) * geometry.scale_y
    _validate_extent_inside(
        viewport_left,
        viewport_top,
        viewport_right,
        viewport_bottom,
        browser,
        "browser",
    )
    _validate_extent_inside(
        viewport_left,
        viewport_top,
        viewport_right,
        viewport_bottom,
        display,
        "display",
    )


def _validate_system_ui_proof(
    proof: SystemUIProof,
    expected: BrowserWindowIdentity,
    *,
    policy: MacCapturePolicy,
) -> None:
    required_proof: tuple[bool, ...] = (
        proof.authoritative,
        proof.system_bar_visible,
        proof.intersects_primary_display,
    )
    if policy is MacCapturePolicy.STRICT:
        required_proof += (proof.date_time_visible,)
    elif policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA:
        required_proof += (proof.visual_review_required,)
    else:
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "截图策略无效",
        )
    if not _same_window(proof.expected_window, expected) or not all(required_proof):
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "无法权威证明系统日期时间栏和任务栏/Dock可见",
        )


def _capture_validation_code(policy: MacCapturePolicy) -> str:
    if policy is MacCapturePolicy.STRICT:
        return "CAPTURE_OK"
    if policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA:
        return "CAPTURE_OK_MAC_VISUAL_REVIEW"
    raise make_capture_error(
        "CAPTURE_ENVIRONMENT",
        "截图策略无效",
    )


def _same_window(
    actual: BrowserWindowIdentity,
    expected: BrowserWindowIdentity,
) -> bool:
    return (
        actual.platform == expected.platform
        and actual.process_id == expected.process_id
        and actual.window_handle == expected.window_handle
    )


def _validate_browser_coverage(
    display: DisplayBounds,
    browser: DisplayBounds,
) -> None:
    x_tolerance = min(32, max(8, round(display.width * 0.02)))
    y_tolerance = min(32, max(8, round(display.height * 0.02)))
    inside = (
        browser.x >= display.x - x_tolerance
        and browser.y >= display.y - y_tolerance
        and browser.x + browser.width
        <= display.x + display.width + x_tolerance
        and browser.y + browser.height
        <= display.y + display.height + y_tolerance
    )
    sufficiently_maximized = (
        browser.width >= display.width * 0.9
        and browser.height >= display.height * 0.75
    )
    if not inside or not sufficiently_maximized:
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "浏览器未在主屏幕内保持最大化可见状态",
        )


def _validate_extent_inside(
    left: float,
    top: float,
    right: float,
    bottom: float,
    bounds: DisplayBounds,
    name: str,
) -> None:
    if (
        left < bounds.x - _GEOMETRY_TOLERANCE_PX
        or top < bounds.y - _GEOMETRY_TOLERANCE_PX
        or right > bounds.x + bounds.width + _GEOMETRY_TOLERANCE_PX
        or bottom > bounds.y + bounds.height + _GEOMETRY_TOLERANCE_PX
        or right <= left
        or bottom <= top
    ):
        raise GeometryError(f"viewport extent is outside {name} bounds")


def _load_capture(path: Path) -> Image.Image:
    try:
        with Image.open(path) as opened:
            opened.load()
            return opened.convert("RGB")
    except OSError as error:
        raise make_capture_error(
            "CAPTURE_FAILED",
            "操作系统截图文件无法读取",
        ) from error


def _validate_positive_finite(value: object, name: str) -> None:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")


def _nonnegative_finite(value: object, name: str) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

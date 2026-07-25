from __future__ import annotations

import hashlib
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from PIL import Image

from quote_app.evidence.annotations import (
    AnnotationError,
    draw_red_annotations,
    validate_annotation_roles,
)
from quote_app.evidence.geometry import (
    CssRect,
    DisplayBounds,
    GeometryError,
    ViewportGeometry,
    css_to_capture_rect,
    validate_screen_rect,
)
from quote_app.evidence.models import EvidenceRecord, EvidenceState
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
    {"CAPTURE_PERMISSION", "CAPTURE_ENVIRONMENT"}
)
_CAPTURE_CODES = _RETRYABLE_CAPTURE_CODES | _NON_RETRYABLE_CAPTURE_CODES
_GEOMETRY_TOLERANCE_PX = 2.0
_SCALE_TOLERANCE = 0.03


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


@dataclass(frozen=True, slots=True)
class SystemUIProof:
    """Authoritative platform proof that required system chrome is visible."""

    expected_window: BrowserWindowIdentity
    system_bar_visible: bool
    date_time_visible: bool
    intersects_primary_display: bool
    authoritative: bool
    source: str

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
    ) -> None:
        self.environment = environment
        self.sleeper = sleeper
        self.now = now or (lambda: datetime.now(timezone.utc))

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        if not isinstance(request, CaptureRequest):
            raise ValueError("request must be a CaptureRequest")
        destination = request.destination
        temporary_path: Path | None = None
        try:
            self._require_capture_permission()
            self._prepare_browser(request.expected_window)
            snapshot = self._geometry_snapshot(request.expected_window)
            _validate_geometry_snapshot(snapshot, request.expected_window)
            proof = self._system_ui_proof(snapshot)
            _validate_system_ui_proof(proof, snapshot.expected_window)

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
            annotations = draw_red_annotations(
                image,
                request.state,
                screen_rectangles,
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
                validation_code="CAPTURE_OK",
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
        except EvidenceCaptureError:
            raise
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

    def _require_capture_permission(self) -> None:
        try:
            allowed = self.environment.screen_capture_permission()
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
) -> None:
    if not _same_window(proof.expected_window, expected) or not all(
        (
            proof.authoritative,
            proof.system_bar_visible,
            proof.date_time_visible,
            proof.intersects_primary_display,
        )
    ):
        raise make_capture_error(
            "CAPTURE_ENVIRONMENT",
            "无法权威证明系统日期时间栏和任务栏/Dock可见",
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


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

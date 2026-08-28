from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass

from quote_app.evidence.chromium import ChromiumWindowSample
from quote_app.evidence.geometry import (
    DisplayBounds,
    GeometryError,
    ViewportGeometry,
)
from quote_app.evidence.macos_native import (
    NativeSystemUISample,
    NativeWebAreaSample,
    NativeWindowSample,
    safe_capture_window_has_required_coverage,
)
from quote_app.evidence.models import MacCapturePolicy
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureGeometrySnapshot,
    SystemUIProof,
)

WINDOW_BOUNDS_TOLERANCE_PX = 2
VIEWPORT_BOUNDS_TOLERANCE_DIP = 2
SAMPLE_MAX_AGE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class BoundMacWindow:
    identity: BrowserWindowIdentity
    chromium: ChromiumWindowSample
    native: NativeWindowSample

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BrowserWindowIdentity):
            raise ValueError("identity must be BrowserWindowIdentity")
        if not isinstance(self.chromium, ChromiumWindowSample):
            raise ValueError("chromium must be ChromiumWindowSample")
        if not isinstance(self.native, NativeWindowSample):
            raise ValueError("native must be NativeWindowSample")
        if (
            self.identity.platform not in {"darwin", "macos"}
            or self.identity.process_id != self.chromium.browser_pid
            or self.identity.process_id != self.native.process_id
            or self.identity.window_handle != str(self.native.window_id)
        ):
            raise ValueError("bound macOS window identity is inconsistent")


class MacWindowBinder:
    def __init__(self, *, window_coordinate_scale: float | None = None) -> None:
        if window_coordinate_scale is not None and (
            not math.isfinite(window_coordinate_scale)
            or window_coordinate_scale <= 0
        ):
            raise ValueError("window_coordinate_scale must be positive and finite")
        self._window_coordinate_scale = window_coordinate_scale

    def bind(
        self,
        chromium: ChromiumWindowSample,
        native_windows: tuple[NativeWindowSample, ...],
        *,
        now_monotonic: float | None = None,
    ) -> BoundMacWindow:
        if not isinstance(chromium, ChromiumWindowSample):
            raise ValueError("chromium must be ChromiumWindowSample")
        if type(native_windows) is not tuple or any(
            not isinstance(sample, NativeWindowSample)
            for sample in native_windows
        ):
            raise ValueError(
                "native_windows must be a tuple of NativeWindowSample"
            )
        _validate_chromium(chromium)
        coordinate_scale = (
            chromium.device_pixel_ratio
            if self._window_coordinate_scale is None
            else self._window_coordinate_scale
        )

        matches = tuple(
            native
            for native in native_windows
            if (
                native.process_id == chromium.browser_pid
                and native.on_screen
                and not native.minimized
                and native.layer == 0
                and _bounds_match_scaled(
                    chromium.bounds_dip,
                    native.bounds_px,
                    coordinate_scale,
                )
            )
        )
        if len(matches) != 1:
            raise ValueError(
                "exactly one safe native macOS window must match Chromium"
            )
        native = matches[0]
        _validate_freshness(
            (chromium, native),
            now_monotonic=now_monotonic,
            error_type=ValueError,
        )
        return BoundMacWindow(
            identity=BrowserWindowIdentity(
                "macos",
                native.process_id,
                str(native.window_id),
            ),
            chromium=chromium,
            native=native,
        )


def make_geometry_snapshot(
    bound: BoundMacWindow,
    web_area: NativeWebAreaSample,
    display_bounds: DisplayBounds,
    *,
    now_monotonic: float | None = None,
) -> CaptureGeometrySnapshot:
    try:
        _validate_bound(bound)
        if not isinstance(web_area, NativeWebAreaSample):
            raise ValueError("web_area must be NativeWebAreaSample")
        _validate_primary_display(display_bounds)
        chromium = bound.chromium
        native = bound.native
        _validate_freshness(
            (chromium, native, web_area),
            now_monotonic=now_monotonic,
            error_type=GeometryError,
        )
        if chromium.fullscreen:
            raise GeometryError("Chromium must not be fullscreen")
        if web_area.window_id != native.window_id:
            raise GeometryError(
                "AXWebArea is not bound to the native browser window"
            )
        if not _bounds_match_scaled(
            chromium.bounds_dip,
            native.bounds_px,
            chromium.device_pixel_ratio,
        ):
            raise GeometryError(
                "browser DIP-to-physical equations do not hold"
            )

        scale_x = native.bounds_px.width / chromium.bounds_dip.width
        scale_y = native.bounds_px.height / chromium.bounds_dip.height
        scale_x_error = (
            WINDOW_BOUNDS_TOLERANCE_PX / chromium.bounds_dip.width
        )
        scale_y_error = (
            WINDOW_BOUNDS_TOLERANCE_PX / chromium.bounds_dip.height
        )
        if (
            abs(scale_x - chromium.device_pixel_ratio) > scale_x_error
            or abs(scale_y - chromium.device_pixel_ratio) > scale_y_error
            or abs(scale_x - scale_y) > scale_x_error + scale_y_error
        ):
            raise GeometryError("browser X and Y physical scales differ")
        if not _contains(display_bounds, native.bounds_px):
            raise GeometryError(
                "browser window is outside the primary display"
            )
        if web_area.bounds_dip.y <= chromium.bounds_dip.y:
            raise GeometryError(
                "AXWebArea origin is not below browser chrome"
            )
        if not _scaled_extent_inside(
            web_area.bounds_dip,
            native.bounds_px,
            chromium.device_pixel_ratio,
        ):
            raise GeometryError(
                "AXWebArea is outside the physical browser window"
            )
        if (
            abs(web_area.bounds_dip.width - chromium.viewport_width_css)
            > VIEWPORT_BOUNDS_TOLERANCE_DIP
            or abs(
                web_area.bounds_dip.height
                - chromium.viewport_height_css
            )
            > VIEWPORT_BOUNDS_TOLERANCE_DIP
        ):
            raise GeometryError(
                "Chromium viewport and AXWebArea dimensions differ"
            )

        return CaptureGeometrySnapshot(
            expected_window=bound.identity,
            display_physical_bounds=display_bounds,
            browser_physical_bounds=native.bounds_px,
            viewport_geometry=ViewportGeometry(
                client_origin_x_dip=web_area.bounds_dip.x,
                client_origin_y_dip=web_area.bounds_dip.y,
                scale_x=chromium.device_pixel_ratio,
                scale_y=chromium.device_pixel_ratio,
                capture_origin_x_px=display_bounds.x,
                capture_origin_y_px=display_bounds.y,
            ),
            viewport_width_css=chromium.viewport_width_css,
            viewport_height_css=chromium.viewport_height_css,
            maximized=chromium.maximized,
            fullscreen=chromium.fullscreen,
            device_pixel_ratio=chromium.device_pixel_ratio,
            sample_id=_sample_digest(
                chromium.sample_id,
                native.sample_id,
                web_area.sample_id,
            ),
        )
    except GeometryError:
        raise
    except (TypeError, ValueError) as error:
        raise GeometryError(str(error)) from error


def make_system_ui_proof(
    bound: BoundMacWindow,
    system_ui: NativeSystemUISample,
    display_bounds: DisplayBounds,
    *,
    now_monotonic: float | None = None,
    policy: MacCapturePolicy = MacCapturePolicy.STRICT,
) -> SystemUIProof:
    if browser_overlaps_system_ui(
        bound,
        system_ui,
        display_bounds,
        now_monotonic=now_monotonic,
        policy=policy,
    ):
        raise ValueError("browser window overlaps required macOS system UI")
    if bound.chromium.maximized or bound.chromium.fullscreen:
        raise ValueError("browser must be a normal non-fullscreen window")
    if (
        policy is not MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
        and not safe_capture_window_has_required_coverage(
        bound.native.bounds_px,
        menu_bar=system_ui.menu_bar_bounds_px,
        dock=system_ui.dock_bounds_px,
        display=display_bounds,
        )
    ):
        raise ValueError(
            "browser window does not cover the macOS safe work area"
        )

    date_time = system_ui.date_time_bounds_px
    return SystemUIProof(
        expected_window=bound.identity,
        system_bar_visible=True,
        date_time_visible=date_time is not None,
        intersects_primary_display=True,
        authoritative=True,
        source=(
            "macos-native-system-ui-sha256:"
            + _sample_digest(
                bound.chromium.sample_id,
                bound.native.sample_id,
                system_ui.sample_id,
            )
        ),
        visual_review_required=system_ui.visual_review_required,
    )


def browser_overlaps_system_ui(
    bound: BoundMacWindow,
    system_ui: NativeSystemUISample,
    display_bounds: DisplayBounds,
    *,
    now_monotonic: float | None = None,
    policy: MacCapturePolicy = MacCapturePolicy.STRICT,
) -> bool:
    """Return overlap only after validating an authoritative native sample."""
    _validate_bound(bound)
    if not isinstance(system_ui, NativeSystemUISample):
        raise ValueError("system_ui must be NativeSystemUISample")
    if not isinstance(policy, MacCapturePolicy):
        raise ValueError("policy must be MacCapturePolicy")
    _validate_primary_display(display_bounds)
    _validate_freshness(
        (bound.chromium, bound.native, system_ui),
        now_monotonic=now_monotonic,
        error_type=ValueError,
    )

    menu_bar = system_ui.menu_bar_bounds_px
    date_time = system_ui.date_time_bounds_px
    dock = system_ui.dock_bounds_px
    for name, bounds in (
        ("menu bar", menu_bar),
        ("Dock", dock),
    ):
        if not isinstance(bounds, DisplayBounds):
            raise ValueError(f"{name} bounds must be DisplayBounds")
        if not _contains(display_bounds, bounds):
            raise ValueError(f"{name} is outside the primary display")
    if system_ui.visual_review_required:
        if policy is not MacCapturePolicy.MAC_VISUAL_REVIEW_BETA:
            raise ValueError("visual review requires the macOS beta policy")
        if date_time is not None:
            raise ValueError("visual review must not claim date-time bounds")
    elif date_time is None:
        raise ValueError("date-time bounds are required without visual review")
    else:
        if not _contains(display_bounds, date_time):
            raise ValueError("date-time menu item is outside the primary display")
        if not _contains(menu_bar, date_time):
            raise ValueError("date-time menu item is outside the menu bar")
    if _intersects(dock, menu_bar) or (
        date_time is not None and _intersects(dock, date_time)
    ):
        raise ValueError("Dock overlaps the menu bar or date-time item")
    return _intersects(bound.native.bounds_px, menu_bar) or _intersects(
        bound.native.bounds_px,
        dock,
    )


def _validate_bound(bound: object) -> None:
    if not isinstance(bound, BoundMacWindow):
        raise ValueError("bound must be BoundMacWindow")
    _validate_chromium(bound.chromium)
    if (
        not bound.native.on_screen
        or bound.native.minimized
        or bound.native.layer != 0
    ):
        raise ValueError("bound native macOS window is not safe")
    if (
        bound.identity.process_id != bound.chromium.browser_pid
        or bound.identity.process_id != bound.native.process_id
        or bound.identity.window_handle != str(bound.native.window_id)
    ):
        raise ValueError("bound macOS window identity changed")


def _validate_chromium(chromium: ChromiumWindowSample) -> None:
    if type(chromium.browser_pid) is not int or chromium.browser_pid <= 0:
        raise ValueError("Chromium browser PID must be positive")
    if not isinstance(chromium.bounds_dip, DisplayBounds):
        raise ValueError("Chromium bounds must be DisplayBounds")
    _positive_finite(
        chromium.viewport_width_css,
        "Chromium viewport width",
    )
    _positive_finite(
        chromium.viewport_height_css,
        "Chromium viewport height",
    )
    _positive_finite(
        chromium.device_pixel_ratio,
        "Chromium device pixel ratio",
    )
    if (
        type(chromium.maximized) is not bool
        or type(chromium.fullscreen) is not bool
    ):
        raise ValueError("Chromium window state flags must be booleans")
    _sample_id(chromium.sample_id)


def _validate_primary_display(display_bounds: object) -> None:
    if not isinstance(display_bounds, DisplayBounds):
        raise ValueError("display_bounds must be DisplayBounds")
    if display_bounds.x < 0 or display_bounds.y < 0:
        raise ValueError(
            "primary display origin must not be negative"
        )


def _validate_freshness(
    samples: tuple[object, ...],
    *,
    now_monotonic: float | None,
    error_type: type[ValueError],
) -> None:
    now = (
        time.monotonic()
        if now_monotonic is None
        else _nonnegative_finite(now_monotonic, "now_monotonic")
    )
    timestamps: list[float] = []
    for sample in samples:
        sample_id = _sample_id(getattr(sample, "sample_id", None))
        del sample_id
        sampled_at = _nonnegative_finite(
            getattr(sample, "sampled_at_monotonic", None),
            "sampled_at_monotonic",
        )
        if sampled_at > now:
            raise error_type("sample timestamp is from the future")
        if now - sampled_at > SAMPLE_MAX_AGE_SECONDS:
            raise error_type("sample timestamp is stale")
        timestamps.append(sampled_at)
    if timestamps and max(timestamps) - min(timestamps) > SAMPLE_MAX_AGE_SECONDS:
        raise error_type("participating sample timestamps are incoherent")


def _sample_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sample_id must be a nonblank opaque string")
    return value


def _nonnegative_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be exact, finite, and non-negative")
    return float(value)


def _positive_finite(value: object, name: str) -> float:
    result = _nonnegative_finite(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _bounds_match_scaled(
    bounds_dip: DisplayBounds,
    bounds_px: DisplayBounds,
    scale: float,
) -> bool:
    return all(
        abs(actual - expected * scale) <= WINDOW_BOUNDS_TOLERANCE_PX
        for actual, expected in (
            (bounds_px.x, bounds_dip.x),
            (bounds_px.y, bounds_dip.y),
            (bounds_px.width, bounds_dip.width),
            (bounds_px.height, bounds_dip.height),
        )
    )


def _scaled_extent_inside(
    inner_dip: DisplayBounds,
    outer_px: DisplayBounds,
    scale: float,
) -> bool:
    left = inner_dip.x * scale
    top = inner_dip.y * scale
    right = (inner_dip.x + inner_dip.width) * scale
    bottom = (inner_dip.y + inner_dip.height) * scale
    return (
        left >= outer_px.x
        and top >= outer_px.y
        and right <= outer_px.x + outer_px.width
        and bottom <= outer_px.y + outer_px.height
    )


def _contains(outer: DisplayBounds, inner: DisplayBounds) -> bool:
    return (
        inner.x >= outer.x
        and inner.y >= outer.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def _intersects(first: DisplayBounds, second: DisplayBounds) -> bool:
    return (
        first.x < second.x + second.width
        and first.x + first.width > second.x
        and first.y < second.y + second.height
        and first.y + first.height > second.y
    )


def _sample_digest(*sample_ids: str) -> str:
    payload = json.dumps(
        sample_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()

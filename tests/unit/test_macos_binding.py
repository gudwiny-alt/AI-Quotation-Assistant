from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from quote_app.evidence.chromium import ChromiumWindowSample
from quote_app.evidence.geometry import DisplayBounds, GeometryError, ViewportGeometry
from quote_app.evidence.macos_binding import (
    BoundMacWindow,
    MacWindowBinder,
    make_geometry_snapshot,
    make_system_ui_proof,
)
from quote_app.evidence.macos_native import (
    NativeSystemUISample,
    NativeWebAreaSample,
    NativeWindowSample,
)
from quote_app.evidence.models import MacCapturePolicy
from quote_app.evidence.platform import BrowserWindowIdentity

_NOW = 100.0
_DISPLAY = DisplayBounds(0, 0, 3024, 1964)


def _chromium(**changes: object) -> ChromiumWindowSample:
    sample = ChromiumWindowSample(
        cdp_window_id=731,
        browser_pid=5172,
        bounds_dip=DisplayBounds(0, 25, 1512, 941),
        viewport_width_css=1472.0,
        viewport_height_css=870.0,
        device_pixel_ratio=2.0,
        maximized=False,
        fullscreen=False,
        sample_id="chromium-sample",
        sampled_at_monotonic=99.2,
    )
    return replace(sample, **changes)


def _native(**changes: object) -> NativeWindowSample:
    sample = NativeWindowSample(
        process_id=5172,
        window_id=901,
        bounds_px=DisplayBounds(0, 50, 3024, 1882),
        layer=0,
        on_screen=True,
        minimized=False,
        sample_id="native-window-sample",
        sampled_at_monotonic=99.3,
    )
    return replace(sample, **changes)


def _web_area(**changes: object) -> NativeWebAreaSample:
    sample = NativeWebAreaSample(
        window_id=901,
        bounds_dip=DisplayBounds(20, 70, 1472, 870),
        sample_id="native-web-area-sample",
        sampled_at_monotonic=99.4,
    )
    return replace(sample, **changes)


def _system_ui(**changes: object) -> NativeSystemUISample:
    sample = NativeSystemUISample(
        menu_bar_bounds_px=DisplayBounds(0, 0, 3024, 50),
        date_time_bounds_px=DisplayBounds(2700, 0, 250, 50),
        dock_bounds_px=DisplayBounds(0, 1932, 3024, 32),
        sample_id="native-system-ui-sample",
        sampled_at_monotonic=99.4,
    )
    return replace(sample, **changes)


def _bound(
    *,
    chromium: ChromiumWindowSample | None = None,
    native: NativeWindowSample | None = None,
) -> BoundMacWindow:
    chromium_sample = chromium or _chromium()
    native_sample = native or _native()
    return MacWindowBinder().bind(
        chromium_sample,
        (native_sample,),
        now_monotonic=_NOW,
    )


def _digest(*sample_ids: str) -> str:
    payload = json.dumps(
        sample_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_binder_returns_exact_pid_and_physical_bounds_match() -> None:
    bound = _bound()

    assert bound == BoundMacWindow(
        identity=BrowserWindowIdentity("macos", 5172, "901"),
        chromium=_chromium(),
        native=_native(),
    )
    assert bound.identity.window_handle == str(bound.native.window_id)
    assert bound.identity.window_handle != str(bound.chromium.cdp_window_id)


def test_binder_can_match_logical_macos_window_coordinates_on_retina() -> None:
    """CGWindow and CDP can both report logical screen coordinates."""

    logical_native = _native(bounds_px=_chromium().bounds_dip)

    bound = MacWindowBinder(window_coordinate_scale=1.0).bind(
        _chromium(),
        (logical_native,),
        now_monotonic=_NOW,
    )

    assert bound.native is logical_native
    assert bound.chromium.device_pixel_ratio == 2.0


@pytest.mark.parametrize(
    "windows",
    [
        (),
        (_native(process_id=9999),),
        (_native(bounds_px=DisplayBounds(3, 50, 3024, 1882)),),
        (_native(), _native(window_id=902)),
    ],
)
def test_binder_rejects_zero_or_multiple_exact_candidates(
    windows: tuple[NativeWindowSample, ...],
) -> None:
    with pytest.raises(ValueError):
        MacWindowBinder().bind(_chromium(), windows, now_monotonic=_NOW)


@pytest.mark.parametrize("delta", [1, 2])
def test_binder_tolerates_at_most_two_physical_pixels_of_rounding(
    delta: int,
) -> None:
    native = _native(
        bounds_px=DisplayBounds(delta, 50 - delta, 3024 + delta, 1882 - delta)
    )

    bound = MacWindowBinder().bind(
        _chromium(),
        (native,),
        now_monotonic=_NOW,
    )

    assert bound.native is native


@pytest.mark.parametrize(
    "native",
    [
        _native(minimized=True),
        _native(on_screen=False),
        _native(layer=1),
    ],
)
def test_binder_rejects_unsafe_native_window(native: NativeWindowSample) -> None:
    with pytest.raises(ValueError):
        MacWindowBinder().bind(
            _chromium(),
            (native,),
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    ("chromium_time", "native_time", "now"),
    [
        (98.99, 99.3, 100.0),
        (100.01, 99.3, 100.0),
        (99.0, 100.01, 100.01),
    ],
)
def test_binder_rejects_stale_future_or_incoherent_samples(
    chromium_time: float,
    native_time: float,
    now: float,
) -> None:
    with pytest.raises(ValueError):
        MacWindowBinder().bind(
            _chromium(sampled_at_monotonic=chromium_time),
            (_native(sampled_at_monotonic=native_time),),
            now_monotonic=now,
        )


def test_binder_ignores_stale_unrelated_native_window() -> None:
    current = _native()
    stale_unrelated = _native(
        process_id=9999,
        window_id=902,
        sampled_at_monotonic=1.0,
    )

    bound = MacWindowBinder().bind(
        _chromium(),
        (stale_unrelated, current),
        now_monotonic=_NOW,
    )

    assert bound.native is current


def test_binder_rejects_unique_matching_stale_window() -> None:
    with pytest.raises(ValueError):
        MacWindowBinder().bind(
            _chromium(),
            (_native(sampled_at_monotonic=1.0),),
            now_monotonic=_NOW,
        )


def test_binder_checks_uniqueness_before_matching_window_freshness() -> None:
    with pytest.raises(ValueError):
        MacWindowBinder().bind(
            _chromium(),
            (
                _native(),
                _native(
                    window_id=902,
                    sampled_at_monotonic=1.0,
                ),
            ),
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    "native",
    [
        _native(on_screen=False),
        _native(minimized=True),
        _native(layer=1),
    ],
)
@pytest.mark.parametrize("composition", ["geometry", "system_ui"])
def test_composition_rejects_manually_bound_unsafe_native_window(
    native: NativeWindowSample,
    composition: str,
) -> None:
    bound = BoundMacWindow(
        identity=BrowserWindowIdentity("macos", 5172, "901"),
        chromium=_chromium(),
        native=native,
    )

    if composition == "geometry":
        with pytest.raises(GeometryError):
            make_geometry_snapshot(
                bound,
                _web_area(),
                _DISPLAY,
                now_monotonic=_NOW,
            )
    else:
        with pytest.raises(ValueError):
            make_system_ui_proof(
                bound,
                _system_ui(),
                _DISPLAY,
                now_monotonic=_NOW,
            )


def test_geometry_snapshot_proves_retina_mapping_and_binds_sample_ids() -> None:
    snapshot = make_geometry_snapshot(
        _bound(),
        _web_area(),
        _DISPLAY,
        now_monotonic=_NOW,
    )

    assert snapshot.expected_window == BrowserWindowIdentity("macos", 5172, "901")
    assert snapshot.display_physical_bounds == _DISPLAY
    assert snapshot.browser_physical_bounds == DisplayBounds(0, 50, 3024, 1882)
    assert snapshot.viewport_geometry == ViewportGeometry(
        client_origin_x_dip=20,
        client_origin_y_dip=70,
        scale_x=2,
        scale_y=2,
        capture_origin_x_px=0,
        capture_origin_y_px=0,
    )
    assert snapshot.viewport_width_css == 1472
    assert snapshot.viewport_height_css == 870
    assert snapshot.device_pixel_ratio == 2
    assert snapshot.maximized is False
    assert snapshot.fullscreen is False
    assert snapshot.sample_id == _digest(
        "chromium-sample",
        "native-window-sample",
        "native-web-area-sample",
    )
    assert all(
        sample_id not in snapshot.sample_id
        for sample_id in (
            "chromium-sample",
            "native-window-sample",
            "native-web-area-sample",
        )
    )


def test_safe_window_geometry_accepts_normal_chromium_window() -> None:
    snapshot = make_geometry_snapshot(
        _bound(chromium=_chromium(maximized=False)),
        _web_area(),
        _DISPLAY,
        now_monotonic=_NOW,
    )

    assert snapshot.maximized is False
    assert snapshot.fullscreen is False


def test_safe_window_proof_accepts_exact_ninety_percent_coverage() -> None:
    safe_width = round(3024 * 0.9)
    safe_height = round((1932 - 50) * 0.9)
    chromium = _chromium(
        maximized=False,
        bounds_dip=DisplayBounds(
            0,
            25,
            safe_width // 2,
            safe_height // 2,
        ),
        viewport_width_css=(safe_width // 2) - 40,
        viewport_height_css=(safe_height // 2) - 71,
    )
    native = _native(
        bounds_px=DisplayBounds(0, 50, safe_width, safe_height),
    )

    proof = make_system_ui_proof(
        _bound(chromium=chromium, native=native),
        _system_ui(),
        _DISPLAY,
        now_monotonic=_NOW,
    )

    assert proof.authoritative is True


@pytest.mark.parametrize(
    "native_bounds",
    [
        DisplayBounds(0, 50, 2720, 1882),
        DisplayBounds(0, 50, 3024, 1693),
        DisplayBounds(0, 49, 3024, 1882),
        DisplayBounds(0, 51, 3024, 1882),
    ],
)
def test_safe_window_proof_rejects_insufficient_coverage_or_system_ui_overlap(
    native_bounds: DisplayBounds,
) -> None:
    chromium = _chromium(
        maximized=False,
        bounds_dip=DisplayBounds(
            native_bounds.x // 2,
            native_bounds.y // 2,
            native_bounds.width // 2,
            native_bounds.height // 2,
        ),
    )

    with pytest.raises(ValueError):
        make_system_ui_proof(
            _bound(
                chromium=chromium,
                native=_native(bounds_px=native_bounds),
            ),
            _system_ui(),
            _DISPLAY,
            now_monotonic=_NOW,
        )


def test_safe_window_proof_rejects_maximized_chromium_state() -> None:
    with pytest.raises(ValueError, match="normal"):
        make_system_ui_proof(
            _bound(chromium=_chromium(maximized=True)),
            _system_ui(),
            _DISPLAY,
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    "web_area",
    [
        _web_area(bounds_dip=DisplayBounds(20, 25, 1472, 870)),
        _web_area(bounds_dip=DisplayBounds(-1, 70, 1472, 870)),
        _web_area(bounds_dip=DisplayBounds(20, 70, 1493, 870)),
        _web_area(window_id=902),
    ],
)
def test_geometry_rejects_web_area_not_below_and_inside_bound_window(
    web_area: NativeWebAreaSample,
) -> None:
    with pytest.raises(GeometryError):
        make_geometry_snapshot(
            _bound(),
            web_area,
            _DISPLAY,
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    "display",
    [
        DisplayBounds(-1, 0, 3024, 1964),
        DisplayBounds(0, -1, 3024, 1964),
    ],
)
def test_geometry_rejects_negative_primary_display_origin(
    display: DisplayBounds,
) -> None:
    with pytest.raises(GeometryError):
        make_geometry_snapshot(
            _bound(),
            _web_area(),
            display,
            now_monotonic=_NOW,
        )


def test_geometry_rejects_mismatched_axis_scale() -> None:
    native = _native(bounds_px=DisplayBounds(0, 50, 3024, 941))
    bound = BoundMacWindow(
        identity=BrowserWindowIdentity("macos", 5172, "901"),
        chromium=_chromium(),
        native=native,
    )

    with pytest.raises(GeometryError):
        make_geometry_snapshot(
            bound,
            _web_area(),
            _DISPLAY,
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    "chromium",
    [
        _chromium(fullscreen=True),
        _chromium(viewport_width_css=1469.0),
        _chromium(viewport_height_css=867.0),
    ],
)
def test_geometry_rejects_unsafe_window_state_or_viewport_disagreement(
    chromium: ChromiumWindowSample,
) -> None:
    with pytest.raises(GeometryError):
        make_geometry_snapshot(
            _bound(chromium=chromium),
            _web_area(),
            _DISPLAY,
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    ("chromium_time", "native_time", "web_time", "now"),
    [
        (99.2, 99.3, 98.99, 100.0),
        (99.2, 99.3, 100.01, 100.0),
        (99.0, 99.5, 100.01, 100.01),
    ],
)
def test_geometry_rejects_stale_future_or_incoherent_samples(
    chromium_time: float,
    native_time: float,
    web_time: float,
    now: float,
) -> None:
    bound = BoundMacWindow(
        identity=BrowserWindowIdentity("macos", 5172, "901"),
        chromium=_chromium(sampled_at_monotonic=chromium_time),
        native=_native(sampled_at_monotonic=native_time),
    )

    with pytest.raises(GeometryError):
        make_geometry_snapshot(
            bound,
            _web_area(sampled_at_monotonic=web_time),
            _DISPLAY,
            now_monotonic=now,
        )


def test_system_ui_proof_requires_contained_nonoverlapping_current_ui() -> None:
    proof = make_system_ui_proof(
        _bound(),
        _system_ui(),
        _DISPLAY,
        now_monotonic=_NOW,
    )

    assert proof.expected_window == BrowserWindowIdentity("macos", 5172, "901")
    assert proof.system_bar_visible is True
    assert proof.date_time_visible is True
    assert proof.intersects_primary_display is True
    assert proof.authoritative is True
    assert proof.source == (
        "macos-native-system-ui-sha256:"
        + _digest(
            "chromium-sample",
            "native-window-sample",
            "native-system-ui-sample",
        )
    )
    assert all(
        sample_id not in proof.source
        for sample_id in (
            "chromium-sample",
            "native-window-sample",
            "native-system-ui-sample",
        )
    )


def test_system_ui_proof_preserves_clock_identity_gap_for_beta_review() -> None:
    proof = make_system_ui_proof(
        _bound(),
        _system_ui(
            date_time_bounds_px=None,
            visual_review_required=True,
        ),
        _DISPLAY,
        now_monotonic=_NOW,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert proof.system_bar_visible is True
    assert proof.date_time_visible is False
    assert proof.intersects_primary_display is True
    assert proof.authoritative is True
    assert proof.visual_review_required is True


def test_system_ui_proof_rejects_visual_clock_identity_gap_under_strict_policy() -> None:
    with pytest.raises(ValueError):
        make_system_ui_proof(
            _bound(),
            _system_ui(
                date_time_bounds_px=None,
                visual_review_required=True,
            ),
            _DISPLAY,
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    "system_ui",
    [
        _system_ui(menu_bar_bounds_px=DisplayBounds(0, 0, 3025, 50)),
        _system_ui(date_time_bounds_px=DisplayBounds(3000, 0, 50, 50)),
        _system_ui(dock_bounds_px=DisplayBounds(0, 1950, 3024, 32)),
        _system_ui(date_time_bounds_px=DisplayBounds(0, 50, 250, 50)),
        _system_ui(dock_bounds_px=DisplayBounds(0, 25, 3024, 100)),
        _system_ui(dock_bounds_px=DisplayBounds(0, 1900, 3024, 64)),
    ],
)
def test_system_ui_rejects_missing_offscreen_or_invalid_overlap(
    system_ui: NativeSystemUISample,
) -> None:
    with pytest.raises(ValueError):
        make_system_ui_proof(
            _bound(),
            system_ui,
            _DISPLAY,
            now_monotonic=_NOW,
        )


def test_system_ui_rejects_missing_bounds() -> None:
    with pytest.raises(ValueError):
        make_system_ui_proof(
            _bound(),
            replace(_system_ui(), dock_bounds_px=None),  # type: ignore[arg-type]
            _DISPLAY,
            now_monotonic=_NOW,
        )


@pytest.mark.parametrize(
    ("chromium_time", "native_time", "ui_time", "now"),
    [
        (99.2, 99.3, 98.99, 100.0),
        (99.2, 99.3, 100.01, 100.0),
        (99.0, 99.5, 100.01, 100.01),
    ],
)
def test_system_ui_rejects_stale_future_or_incoherent_samples(
    chromium_time: float,
    native_time: float,
    ui_time: float,
    now: float,
) -> None:
    bound = BoundMacWindow(
        identity=BrowserWindowIdentity("macos", 5172, "901"),
        chromium=_chromium(sampled_at_monotonic=chromium_time),
        native=_native(sampled_at_monotonic=native_time),
    )

    with pytest.raises(ValueError):
        make_system_ui_proof(
            bound,
            _system_ui(sampled_at_monotonic=ui_time),
            _DISPLAY,
            now_monotonic=now,
        )


@pytest.mark.parametrize("bad_value", [-1.0, float("nan"), float("inf"), True])
@pytest.mark.parametrize(
    "sample",
    [_chromium(), _native(), _web_area(), _system_ui()],
)
def test_all_sample_contracts_reject_invalid_monotonic_timestamp(
    sample: object,
    bad_value: object,
) -> None:
    with pytest.raises(ValueError, match="sampled_at_monotonic"):
        replace(sample, sampled_at_monotonic=bad_value)

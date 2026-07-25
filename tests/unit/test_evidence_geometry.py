from __future__ import annotations

import math

import pytest

from quote_app.evidence.geometry import (
    CssRect,
    GeometryError,
    ScreenRect,
    ViewportGeometry,
    css_to_capture_rect,
    validate_screen_rect,
)


def test_css_rect_converts_to_retina_capture_pixels() -> None:
    geometry = ViewportGeometry(
        client_origin_x_dip=100,
        client_origin_y_dip=180,
        scale_x=2.0,
        scale_y=2.0,
        capture_origin_x_px=0,
        capture_origin_y_px=0,
    )

    assert css_to_capture_rect(
        CssRect(10, 20, 50, 30, "capacity"),
        geometry,
    ) == ScreenRect(220, 400, 100, 60, "capacity")


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        (1.0, ScreenRect(120, 100, 40, 20, "color")),
        (1.25, ScreenRect(150, 125, 50, 25, "color")),
        (1.5, ScreenRect(180, 150, 60, 30, "color")),
    ],
)
def test_windows_scaling_converts_css_rectangles(
    scale: float,
    expected: ScreenRect,
) -> None:
    geometry = ViewportGeometry(
        client_origin_x_dip=100,
        client_origin_y_dip=80,
        scale_x=scale,
        scale_y=scale,
        capture_origin_x_px=0,
        capture_origin_y_px=0,
    )

    assert css_to_capture_rect(
        CssRect(20, 20, 40, 20, "color"),
        geometry,
    ) == expected


def test_window_factory_applies_nonzero_browser_chrome_inset() -> None:
    geometry = ViewportGeometry.from_window(
        window_origin_x_dip=50,
        window_origin_y_dip=20,
        chrome_inset_x_dip=8,
        chrome_inset_y_dip=72,
        scale_x=2,
        scale_y=2,
        capture_origin_x_px=0,
        capture_origin_y_px=0,
    )

    assert css_to_capture_rect(
        CssRect(10, 5, 20, 10, "price"),
        geometry,
    ) == ScreenRect(136, 194, 40, 20, "price")


def test_negative_display_origin_is_removed_from_capture_coordinates() -> None:
    geometry = ViewportGeometry(
        client_origin_x_dip=-900,
        client_origin_y_dip=100,
        scale_x=2,
        scale_y=2,
        capture_origin_x_px=-2000,
        capture_origin_y_px=0,
    )

    assert css_to_capture_rect(
        CssRect(10, 20, 50, 30, "result_region"),
        geometry,
    ) == ScreenRect(220, 240, 100, 60, "result_region")


def test_fractional_rectangle_rounds_outward_to_cover_dom_target() -> None:
    geometry = ViewportGeometry(
        client_origin_x_dip=0,
        client_origin_y_dip=0,
        scale_x=1.25,
        scale_y=1.25,
        capture_origin_x_px=0,
        capture_origin_y_px=0,
    )

    assert css_to_capture_rect(
        CssRect(1, 1, 2, 2, "stock_status"),
        geometry,
    ) == ScreenRect(1, 1, 3, 3, "stock_status")


def test_rectangle_touching_image_edge_is_valid() -> None:
    rectangle = ScreenRect(90, 80, 10, 20, "capacity")

    assert validate_screen_rect(rectangle, image_width=100, image_height=100) is rectangle


@pytest.mark.parametrize(
    "rectangle",
    [
        ScreenRect(-1, 0, 10, 10, "capacity"),
        ScreenRect(0, -1, 10, 10, "capacity"),
        ScreenRect(91, 80, 10, 20, "capacity"),
        ScreenRect(90, 81, 10, 20, "capacity"),
    ],
)
def test_rectangle_exceeding_image_bounds_is_rejected(
    rectangle: ScreenRect,
) -> None:
    with pytest.raises(GeometryError, match="CAPTURE_GEOMETRY"):
        validate_screen_rect(rectangle, image_width=100, image_height=100)


@pytest.mark.parametrize(
    "bad_value",
    [math.nan, math.inf, -math.inf],
)
def test_geometry_rejects_nonfinite_values(bad_value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        CssRect(bad_value, 0, 10, 10, "capacity")

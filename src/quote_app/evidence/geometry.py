from __future__ import annotations

import math
from dataclasses import dataclass


class GeometryError(ValueError):
    code = "CAPTURE_GEOMETRY"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True, slots=True)
class CssRect:
    x: float
    y: float
    width: float
    height: float
    role: str

    def __post_init__(self) -> None:
        _validate_finite((self.x, self.y, self.width, self.height))
        if self.width <= 0 or self.height <= 0:
            raise ValueError("CSS rectangle dimensions must be positive")
        _validate_role(self.role)


@dataclass(frozen=True, slots=True)
class ScreenRect:
    x: int
    y: int
    width: int
    height: int
    role: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not int
            for value in (self.x, self.y, self.width, self.height)
        ):
            raise ValueError("screen rectangle values must be integers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("screen rectangle dimensions must be positive")
        _validate_role(self.role)


@dataclass(frozen=True, slots=True)
class DisplayBounds:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int
            for value in (self.x, self.y, self.width, self.height)
        ):
            raise ValueError("display bounds must be integers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("display dimensions must be positive")


@dataclass(frozen=True, slots=True)
class ViewportGeometry:
    client_origin_x_dip: float
    client_origin_y_dip: float
    scale_x: float
    scale_y: float
    capture_origin_x_px: float
    capture_origin_y_px: float

    def __post_init__(self) -> None:
        _validate_finite(
            (
                self.client_origin_x_dip,
                self.client_origin_y_dip,
                self.scale_x,
                self.scale_y,
                self.capture_origin_x_px,
                self.capture_origin_y_px,
            )
        )
        if self.scale_x <= 0 or self.scale_y <= 0:
            raise ValueError("viewport scales must be positive")

    @classmethod
    def from_window(
        cls,
        *,
        window_origin_x_dip: float,
        window_origin_y_dip: float,
        chrome_inset_x_dip: float,
        chrome_inset_y_dip: float,
        scale_x: float,
        scale_y: float,
        capture_origin_x_px: float,
        capture_origin_y_px: float,
    ) -> ViewportGeometry:
        return cls(
            client_origin_x_dip=window_origin_x_dip + chrome_inset_x_dip,
            client_origin_y_dip=window_origin_y_dip + chrome_inset_y_dip,
            scale_x=scale_x,
            scale_y=scale_y,
            capture_origin_x_px=capture_origin_x_px,
            capture_origin_y_px=capture_origin_y_px,
        )


def css_to_capture_rect(
    rectangle: CssRect,
    geometry: ViewportGeometry,
) -> ScreenRect:
    if not isinstance(rectangle, CssRect):
        raise ValueError("rectangle must be a CssRect")
    if not isinstance(geometry, ViewportGeometry):
        raise ValueError("geometry must be a ViewportGeometry")

    left = math.floor(
        (geometry.client_origin_x_dip + rectangle.x) * geometry.scale_x
        - geometry.capture_origin_x_px
    )
    top = math.floor(
        (geometry.client_origin_y_dip + rectangle.y) * geometry.scale_y
        - geometry.capture_origin_y_px
    )
    right = math.ceil(
        (geometry.client_origin_x_dip + rectangle.x + rectangle.width)
        * geometry.scale_x
        - geometry.capture_origin_x_px
    )
    bottom = math.ceil(
        (geometry.client_origin_y_dip + rectangle.y + rectangle.height)
        * geometry.scale_y
        - geometry.capture_origin_y_px
    )
    return ScreenRect(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
        role=rectangle.role,
    )


def validate_screen_rect(
    rectangle: ScreenRect,
    *,
    image_width: int,
    image_height: int,
) -> ScreenRect:
    if not isinstance(rectangle, ScreenRect):
        raise ValueError("rectangle must be a ScreenRect")
    if (
        type(image_width) is not int
        or type(image_height) is not int
        or image_width <= 0
        or image_height <= 0
    ):
        raise ValueError("image dimensions must be positive integers")
    if (
        rectangle.x < 0
        or rectangle.y < 0
        or rectangle.x + rectangle.width > image_width
        or rectangle.y + rectangle.height > image_height
    ):
        raise GeometryError(
            f"rectangle role {rectangle.role!r} exceeds captured image bounds"
        )
    return rectangle


def _validate_finite(values: tuple[float, ...]) -> None:
    if any(
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in values
    ):
        raise ValueError("geometry values must be finite numbers")


def _validate_role(role: object) -> None:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("rectangle role must not be blank")

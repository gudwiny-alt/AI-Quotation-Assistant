from __future__ import annotations

from PIL import Image, ImageDraw

from quote_app.evidence.geometry import ScreenRect
from quote_app.evidence.models import EvidenceRectangle, EvidenceState


class AnnotationError(ValueError):
    code = "CAPTURE_GEOMETRY"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


_EXACT_ROLES = {
    EvidenceState.NORMAL: frozenset(),
    EvidenceState.NO_MODEL: frozenset({"search_keyword", "result_region"}),
    EvidenceState.CAPACITY_UNAVAILABLE: frozenset({"capacity"}),
    EvidenceState.SOLD_OUT: frozenset({"stock_status"}),
}


def validate_annotation_roles(
    state: EvidenceState,
    rectangles: tuple[ScreenRect, ...],
    *,
    expected_roles: tuple[str, ...],
) -> tuple[ScreenRect, ...]:
    if not isinstance(state, EvidenceState):
        raise ValueError("state must be an EvidenceState")
    if not isinstance(rectangles, tuple | list) or not all(
        isinstance(rectangle, ScreenRect) for rectangle in rectangles
    ):
        raise ValueError("rectangles must contain ScreenRect values")
    if not isinstance(expected_roles, tuple | list) or not all(
        isinstance(role, str) and role.strip() for role in expected_roles
    ):
        raise ValueError("expected_roles must contain nonblank strings")

    normalized_rectangles = tuple(rectangles)
    normalized_expected = tuple(role.strip() for role in expected_roles)
    actual_roles = tuple(rectangle.role for rectangle in normalized_rectangles)
    if len(set(actual_roles)) != len(actual_roles):
        raise AnnotationError("annotation roles must not be duplicated")
    if actual_roles != normalized_expected:
        raise AnnotationError("DOM annotation roles do not match expected roles")

    actual_set = frozenset(actual_roles)
    if state is EvidenceState.COLOR_UNAVAILABLE:
        allowed = {
            frozenset({"color"}),
            frozenset({"capacity", "color"}),
        }
        if actual_set not in allowed:
            raise AnnotationError(
                "color-unavailable evidence requires a color frame"
            )
    elif actual_set != _EXACT_ROLES[state]:
        raise AnnotationError(
            f"annotation roles do not satisfy evidence state {state.value}"
        )
    return normalized_rectangles


def draw_red_annotations(
    image: Image.Image,
    state: EvidenceState,
    rectangles: tuple[ScreenRect, ...],
    *,
    line_width: int = 6,
) -> tuple[EvidenceRectangle, ...]:
    if not isinstance(image, Image.Image):
        raise ValueError("image must be a Pillow Image")
    if not isinstance(state, EvidenceState):
        raise ValueError("state must be an EvidenceState")
    if type(line_width) is not int or line_width <= 0:
        raise ValueError("line_width must be a positive integer")
    if state is EvidenceState.NORMAL:
        if rectangles:
            raise AnnotationError("normal evidence must not contain red frames")
        return ()

    draw = ImageDraw.Draw(image)
    annotations: list[EvidenceRectangle] = []
    for rectangle in rectangles:
        right = rectangle.x + rectangle.width - 1
        bottom = rectangle.y + rectangle.height - 1
        draw.rectangle(
            (rectangle.x, rectangle.y, right, bottom),
            outline=(255, 0, 0),
            width=line_width,
        )
        annotations.append(
            EvidenceRectangle(
                role=rectangle.role,
                x=rectangle.x,
                y=rectangle.y,
                width=rectangle.width,
                height=rectangle.height,
            )
        )
    return tuple(annotations)

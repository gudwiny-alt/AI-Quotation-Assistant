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
_COMPLETE_GROUP_ROLES = {
    EvidenceState.CAPACITY_UNAVAILABLE: frozenset(
        {frozenset({"capacity"}), frozenset({"title", "capacity_group"})}
    ),
    EvidenceState.COLOR_UNAVAILABLE: frozenset(
        {
            frozenset({"color"}),
            frozenset({"capacity", "color"}),
            frozenset({"title", "color_group"}),
        }
    ),
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
    if state in _COMPLETE_GROUP_ROLES:
        if actual_set not in _COMPLETE_GROUP_ROLES[state]:
            raise AnnotationError(
                f"annotation roles do not satisfy evidence state {state.value}"
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
    line_width: int = 8,
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
    rendered_rectangles = (
        (_union_rectangle(rectangles),)
        if state is EvidenceState.NO_MODEL and rectangles
        else rectangles
    )
    for rectangle in rendered_rectangles:
        right = rectangle.x + rectangle.width - 1
        bottom = rectangle.y + rectangle.height - 1
        draw.rectangle(
            (rectangle.x, rectangle.y, right, bottom),
            outline=(255, 0, 0),
            width=line_width,
        )

    annotations: list[EvidenceRectangle] = []
    for rectangle in rectangles:
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


def _union_rectangle(rectangles: tuple[ScreenRect, ...]) -> ScreenRect:
    left = min(rectangle.x for rectangle in rectangles)
    top = min(rectangle.y for rectangle in rectangles)
    right = max(rectangle.x + rectangle.width for rectangle in rectangles)
    bottom = max(rectangle.y + rectangle.height for rectangle in rectangles)
    return ScreenRect(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
        role="result_region",
    )

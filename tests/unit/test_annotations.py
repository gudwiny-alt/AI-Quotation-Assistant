from __future__ import annotations

from PIL import Image
import pytest

from quote_app.evidence.annotations import (
    AnnotationError,
    draw_red_annotations,
    validate_annotation_roles,
)
from quote_app.evidence.geometry import ScreenRect
from quote_app.evidence.models import EvidenceState


def _rect(role: str, x: int = 10, y: int = 10) -> ScreenRect:
    return ScreenRect(x, y, 40, 20, role)


def test_no_model_requires_search_keyword_and_result_region_frames() -> None:
    rectangles = (
        _rect("search_keyword"),
        _rect("result_region", y=50),
    )

    assert validate_annotation_roles(
        EvidenceState.NO_MODEL,
        rectangles,
        expected_roles=("search_keyword", "result_region"),
    ) == rectangles


@pytest.mark.parametrize(
    ("state", "rectangles", "expected_roles"),
    [
        (EvidenceState.NORMAL, (), ()),
        (EvidenceState.CAPACITY_UNAVAILABLE, (_rect("capacity"),), ("capacity",)),
        (EvidenceState.COLOR_UNAVAILABLE, (_rect("color"),), ("color",)),
        (
            EvidenceState.COLOR_UNAVAILABLE,
            (_rect("capacity"), _rect("color", y=40)),
            ("capacity", "color"),
        ),
        (EvidenceState.SOLD_OUT, (_rect("stock_status"),), ("stock_status",)),
    ],
)
def test_business_state_role_matrix_is_accepted(
    state: EvidenceState,
    rectangles: tuple[ScreenRect, ...],
    expected_roles: tuple[str, ...],
) -> None:
    assert validate_annotation_roles(
        state,
        rectangles,
        expected_roles=expected_roles,
    ) == rectangles


@pytest.mark.parametrize(
    ("state", "rectangles", "expected_roles"),
    [
        (EvidenceState.NORMAL, (_rect("price"),), ("price",)),
        (EvidenceState.NO_MODEL, (_rect("result_region"),), ("result_region",)),
        (
            EvidenceState.CAPACITY_UNAVAILABLE,
            (_rect("color"),),
            ("color",),
        ),
        (EvidenceState.COLOR_UNAVAILABLE, (_rect("capacity"),), ("capacity",)),
        (EvidenceState.SOLD_OUT, (_rect("price"),), ("price",)),
        (
            EvidenceState.NO_MODEL,
            (_rect("search_keyword"), _rect("search_keyword", y=50)),
            ("search_keyword", "search_keyword"),
        ),
    ],
)
def test_missing_unexpected_or_duplicate_roles_are_rejected(
    state: EvidenceState,
    rectangles: tuple[ScreenRect, ...],
    expected_roles: tuple[str, ...],
) -> None:
    with pytest.raises(AnnotationError, match="CAPTURE_GEOMETRY"):
        validate_annotation_roles(
            state,
            rectangles,
            expected_roles=expected_roles,
        )


def test_normal_sale_draws_no_red_frames() -> None:
    image = Image.new("RGB", (100, 100), "white")

    annotations = draw_red_annotations(
        image,
        EvidenceState.NORMAL,
        (),
        line_width=3,
    )

    assert annotations == ()
    assert image.getpixel((10, 10)) == (255, 255, 255)


def test_no_model_draws_one_bright_union_frame_around_search_and_results() -> None:
    """Break caught: no-model evidence shows two thin boxes instead of one proof frame."""
    image = Image.new("RGB", (140, 140), "white")
    rectangles = (
        ScreenRect(10, 10, 80, 20, "search_keyword"),
        ScreenRect(20, 50, 100, 70, "result_region"),
    )

    annotations = draw_red_annotations(
        image,
        EvidenceState.NO_MODEL,
        rectangles,
    )

    assert tuple(annotation.role for annotation in annotations) == (
        "search_keyword",
        "result_region",
    )
    assert image.getpixel((10, 10)) == (255, 0, 0)
    assert image.getpixel((119, 119)) == (255, 0, 0)
    assert image.getpixel((60, 13)) == (255, 0, 0)
    assert image.getpixel((60, 50)) == (255, 255, 255)


@pytest.mark.parametrize(
    ("state", "rectangles", "expected_roles"),
    [
        (
            EvidenceState.NO_MODEL,
            (_rect("search_keyword"), _rect("result_region", y=50)),
            ("search_keyword", "result_region"),
        ),
        (
            EvidenceState.CAPACITY_UNAVAILABLE,
            (_rect("capacity"),),
            ("capacity",),
        ),
        (
            EvidenceState.COLOR_UNAVAILABLE,
            (_rect("color"),),
            ("color",),
        ),
        (
            EvidenceState.SOLD_OUT,
            (_rect("stock_status"),),
            ("stock_status",),
        ),
    ],
)
def test_non_normal_business_state_draws_exact_red_frames(
    state: EvidenceState,
    rectangles: tuple[ScreenRect, ...],
    expected_roles: tuple[str, ...],
) -> None:
    image = Image.new("RGB", (100, 100), "white")
    validate_annotation_roles(
        state,
        rectangles,
        expected_roles=expected_roles,
    )

    annotations = draw_red_annotations(
        image,
        state,
        rectangles,
        line_width=3,
    )

    assert tuple(annotation.role for annotation in annotations) == expected_roles
    for rectangle in rectangles:
        assert image.getpixel((rectangle.x, rectangle.y)) == (255, 0, 0)

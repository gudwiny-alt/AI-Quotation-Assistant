from __future__ import annotations

from pathlib import Path

import pytest

from quote_app.evidence.annotations import validate_annotation_roles
from quote_app.evidence.geometry import CssRect, ScreenRect
from quote_app.evidence.models import EvidenceState
from quote_app.evidence.platform import BrowserWindowIdentity, CaptureContext
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.official_brands.models import (
    OfficialBusinessState,
    OfficialCaptureView,
)
from quote_app.sites.protocol import AdapterObservation
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from quote_app.tasks.runner import WebsiteTaskRunner


class _StableProbe:
    def semantic_hash(self) -> str:
        return "stable"


@pytest.mark.parametrize(
    ("outcome", "roles", "state"),
    [
        (
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            ("title", "capacity_group"),
            EvidenceState.CAPACITY_UNAVAILABLE,
        ),
        (
            BusinessOutcome.COLOR_UNAVAILABLE,
            ("title", "color_group"),
            EvidenceState.COLOR_UNAVAILABLE,
        ),
    ],
)
def test_huawei_complete_group_roles_cross_all_evidence_boundaries(
    tmp_path: Path,
    outcome: BusinessOutcome,
    roles: tuple[str, str],
    state: EvidenceState,
) -> None:
    css = tuple(
        CssRect(10.0, 20.0 + index * 60, 300.0, 40.0, role)
        for index, role in enumerate(roles)
    )
    business = OfficialBusinessState.legal_no(
        canonical_url="https://item.vmall.com/product/10086259366534.html",
        brand="华为",
        model_name="HUAWEI Mate 70 Pro",
        capacity="8GB+256GB",
        color="曜石黑",
        outcome=outcome,
        capture_view=OfficialCaptureView(css),
        detail_identity="10086259366534",
    )
    semantic = VerifiedSemanticState(
        canonical_url=business.canonical_url,
        brand=business.brand,
        model_name=business.model_name,
        capacity=business.capacity,
        color=business.color,
        current_sku="not-applicable",
        region="not-applicable",
        stock_state="not-applicable",
        price=None,
        outcome=outcome,
        css_rectangles=css,
    )
    observation = AdapterObservation(
        outcome=outcome,
        price=None,
        url=business.canonical_url,
        css_rectangles=css,
        semantic_state=semantic,
    )
    task = WebsiteTask(
        task_id="huawei-role-boundary",
        run_id="run",
        source_row_number=2,
        output_row_number=2,
        material_code="MAT-HUAWEI",
        brand="华为",
        model_name="HUAWEI Mate 70 Pro",
        ram="8GB",
        storage="256GB",
        color="曜石黑",
        channel=WebsiteChannel.OFFICIAL,
    )
    runner = object.__new__(WebsiteTaskRunner)
    runner.minimum_stability_interval_seconds = 0.15
    runner.capture_context_provider = lambda *_args: CaptureContext(
        expected_window=BrowserWindowIdentity("darwin", 1, "window"),
        stability_probe=_StableProbe(),
    )
    request = runner._capture_request(
        task,
        object(),
        observation,
        tmp_path / "capture.png",
    )
    screen = tuple(
        ScreenRect(10, 20 + index * 60, 300, 40, role)
        for index, role in enumerate(roles)
    )

    assert request.expected_roles == roles
    assert validate_annotation_roles(
        state,
        screen,
        expected_roles=request.expected_roles,
    ) == screen


@pytest.mark.parametrize(
    ("outcome", "roles"),
    [
        (BusinessOutcome.CAPACITY_UNAVAILABLE, ("title", "capacity")),
        (BusinessOutcome.COLOR_UNAVAILABLE, ("title", "color")),
        (BusinessOutcome.CAPACITY_UNAVAILABLE, ("capacity_group",)),
        (BusinessOutcome.COLOR_UNAVAILABLE, ("color_group",)),
    ],
)
def test_unapproved_group_role_combinations_remain_closed(
    outcome: BusinessOutcome,
    roles: tuple[str, ...],
) -> None:
    rectangles = tuple(
        CssRect(10.0, 20.0 + index * 60, 300.0, 40.0, role)
        for index, role in enumerate(roles)
    )

    with pytest.raises(ValueError, match="roles"):
        OfficialBusinessState.legal_no(
            canonical_url="https://item.vmall.com/product/10086259366534.html",
            brand="华为",
            model_name="HUAWEI Mate 70 Pro",
            capacity="8GB+256GB",
            color="曜石黑",
            outcome=outcome,
            capture_view=OfficialCaptureView(rectangles),
            detail_identity="10086259366534",
        )

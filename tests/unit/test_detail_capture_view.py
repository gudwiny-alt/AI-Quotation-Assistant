from __future__ import annotations

import pytest

from quote_app.sites.detail_capture_view import (
    apply_capture_scale,
    ensure_capture_scale,
    fit_search_results_for_capture,
    position_detail_for_capture,
    position_result_cards_for_capture,
    restore_capture_scale,
)
from quote_app.tasks.retry import LayoutRecognitionError


class _Page:
    def __init__(
        self,
        *,
        can_position: bool = True,
        succeeds_at_scale: float = 1.0,
        viewport_scale_thresholds: dict[str, float] | None = None,
        scale_samples: tuple[tuple[float, float], ...] | None = None,
        upward_recovery_ready_after: int | None = None,
        title_clipped: bool = False,
        title_unclipped_after: int | None = None,
    ) -> None:
        self.in_viewport = {
            "title": False,
            "price": False,
            "capacity": False,
            "color": False,
            "card": False,
            "search": False,
            "result": False,
        }
        self.centered: list[str] = []
        self.scroll_targets: list[str] = []
        self.position_scripts: list[str] = []
        self.waits: list[int] = []
        self.can_position = can_position
        self.succeeds_at_scale = succeeds_at_scale
        self.viewport_scale_thresholds = viewport_scale_thresholds or {}
        self.current_scale = 1.0
        self.capture_scales: list[float] = []
        self.scale_restored = False
        self.scale_samples = scale_samples
        self.scale_sample_index = 0
        self.upward_recovery_ready_after = upward_recovery_ready_after
        self.upward_scrolls = 0
        self.title_clipped = title_clipped
        self.title_unclipped_after = title_unclipped_after

    def evaluate(
        self,
        script: str,
        value: float | None = None,
    ) -> bool | dict[str, str]:
        if "quotation-capture-scale" not in script:
            assert "getComputedStyle" in script
            return self._scale_sample()
        if "root.removeAttribute" in script:
            self.scale_restored = True
            self.current_scale = 1.0
            return True
        assert value is not None
        self.current_scale = value
        self.capture_scales.append(value)
        if self.current_scale <= self.succeeds_at_scale:
            self.in_viewport["search"] = True
            self.in_viewport["result"] = True
        for name, threshold in self.viewport_scale_thresholds.items():
            self.in_viewport[name] = self.current_scale <= threshold
        return self._scale_sample()

    def _scale_sample(self) -> dict[str, str]:
        if self.scale_samples is None:
            inline_zoom = computed_zoom = self.current_scale
        else:
            inline_zoom, computed_zoom = self.scale_samples[
                min(self.scale_sample_index, len(self.scale_samples) - 1)
            ]
        self.scale_sample_index += 1
        return {
            "inlineZoom": str(inline_zoom),
            "computedZoom": str(computed_zoom),
        }

    def center(self, name: str) -> None:
        self.centered.append(name)
        self.scroll_targets.append(name)
        if name == "capacity" and self.can_position and (
            self.current_scale <= self.succeeds_at_scale
        ):
            self.in_viewport = {key: True for key in self.in_viewport}
            if self.upward_recovery_ready_after is not None:
                self.in_viewport["title"] = False
        elif name == "card" and self.can_position and (
            self.current_scale <= self.succeeds_at_scale
        ):
            self.in_viewport["card"] = True
            self.in_viewport["title"] = True
            self.in_viewport["price"] = True
        elif self.can_position:
            self.in_viewport[name] = True

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def nudge_up(self) -> None:
        self.upward_scrolls += 1
        if (
            self.title_unclipped_after is not None
            and self.upward_scrolls >= self.title_unclipped_after
        ):
            self.title_clipped = False
        if (
            self.upward_recovery_ready_after is not None
            and self.upward_scrolls >= self.upward_recovery_ready_after
        ):
            self.in_viewport["title"] = True


class _Locator:
    def __init__(self, page: _Page, name: str) -> None:
        self.page = page
        self.name = name

    def evaluate(self, script: str) -> bool | None:
        if "scrollIntoView" in script:
            self.page.position_scripts.append(script)
            self.page.center(self.name)
            return None
        if "quotationSmallUpwardNudge" in script:
            self.page.position_scripts.append(script)
            self.page.nudge_up()
            return True
        if "quotationTitleClipped" in script:
            return self.page.title_clipped
        if "getBoundingClientRect" in script:
            return self.page.in_viewport[self.name]
        raise AssertionError(f"unexpected locator script: {script}")


def test_positions_nearest_detail_scroll_area_until_title_price_and_skus_share_viewport() -> None:
    page = _Page()

    position_detail_for_capture(
        page,
        title=_Locator(page, "title"),
        prices=(_Locator(page, "price"),),
        capacity=_Locator(page, "capacity"),
        color=_Locator(page, "color"),
        site_name="JD",
    )

    assert page.centered == ["capacity"]
    assert page.waits == [300]
    assert page.capture_scales == []


def test_keeps_an_already_valid_detail_capture_view_still() -> None:
    page = _Page()
    for key in ("title", "price", "capacity", "color"):
        page.in_viewport[key] = True

    position_detail_for_capture(
        page,
        title=_Locator(page, "title"),
        prices=(_Locator(page, "price"),),
        capacity=_Locator(page, "capacity"),
        color=_Locator(page, "color"),
        site_name="JD",
        upward_recovery_steps=6,
        preserve_ready_position=True,
    )

    assert page.centered == []
    assert page.upward_scrolls == 0
    assert page.waits == []


def test_tmall_detail_only_nudges_until_a_clipped_title_is_fully_revealed() -> None:
    page = _Page(title_clipped=True, title_unclipped_after=2)
    for key in ("title", "price", "capacity", "color"):
        page.in_viewport[key] = True

    position_detail_for_capture(
        page,
        title=_Locator(page, "title"),
        prices=(_Locator(page, "price"),),
        capacity=_Locator(page, "capacity"),
        color=_Locator(page, "color"),
        site_name="Tmall",
        preserve_ready_position=True,
        reveal_clipped_title=True,
    )

    assert page.centered == []
    assert page.upward_scrolls == 2
    assert page.waits == [120, 120]


def test_tmall_detail_does_not_move_a_fully_visible_title() -> None:
    page = _Page(title_clipped=False)
    for key in ("title", "price", "capacity", "color"):
        page.in_viewport[key] = True

    position_detail_for_capture(
        page,
        title=_Locator(page, "title"),
        prices=(_Locator(page, "price"),),
        capacity=_Locator(page, "capacity"),
        color=_Locator(page, "color"),
        site_name="Tmall",
        preserve_ready_position=True,
        reveal_clipped_title=True,
    )

    assert page.centered == []
    assert page.upward_scrolls == 0
    assert page.waits == []


def test_default_detail_capture_still_centers_capacity_when_already_valid() -> None:
    page = _Page()
    for key in ("title", "price", "capacity", "color"):
        page.in_viewport[key] = True

    position_detail_for_capture(
        page,
        title=_Locator(page, "title"),
        prices=(_Locator(page, "price"),),
        capacity=_Locator(page, "capacity"),
        color=_Locator(page, "color"),
        site_name="Tmall",
    )

    assert page.centered == ["capacity"]
    assert page.waits == [300]


def test_jd_detail_recovers_a_hidden_title_with_small_bounded_upward_nudges() -> None:
    page = _Page(upward_recovery_ready_after=2)

    position_detail_for_capture(
        page,
        title=_Locator(page, "title"),
        prices=(_Locator(page, "price"),),
        capacity=_Locator(page, "capacity"),
        color=_Locator(page, "color"),
        site_name="JD",
        upward_recovery_steps=6,
    )

    assert page.centered == ["capacity"]
    assert page.upward_scrolls == 2
    assert page.waits == [300, 120, 120]


def test_applies_one_fixed_capture_scale_and_waits_for_layout() -> None:
    page = _Page()

    proof = apply_capture_scale(page, scale=0.9)

    assert proof.inline_zoom == 0.9
    assert proof.computed_zoom == 0.9
    assert proof.sample_count == 2
    assert page.capture_scales == [0.9]
    assert page.waits == [300]


def test_ensure_capture_scale_does_not_reapply_an_active_scale() -> None:
    page = _Page()
    page.current_scale = 0.8

    proof = ensure_capture_scale(page, scale=0.8)

    assert proof.inline_zoom == 0.8
    assert proof.computed_zoom == 0.8
    assert proof.sample_count == 1
    assert page.capture_scales == []
    assert page.waits == []


def test_rejects_capture_scale_when_computed_zoom_does_not_match_inline_zoom() -> None:
    page = _Page(scale_samples=((0.8, 1.0), (0.8, 1.0)))

    with pytest.raises(
        LayoutRecognitionError,
        match="capture scale 0.8 did not become visually stable",
    ):
        apply_capture_scale(page, scale=0.8)


def test_accepts_capture_scale_only_after_two_matching_visual_samples() -> None:
    page = _Page(scale_samples=((0.8, 0.8), (0.8, 0.8)))

    proof = apply_capture_scale(page, scale=0.8)

    assert proof.inline_zoom == 0.8
    assert proof.computed_zoom == 0.8
    assert proof.sample_count == 2


def test_restores_the_original_capture_scale() -> None:
    page = _Page()
    apply_capture_scale(page, scale=0.9)

    restore_capture_scale(page)

    assert page.scale_restored is True


def test_rejects_capture_when_title_cannot_share_the_viewport_with_price_and_skus() -> None:
    page = _Page(can_position=False)

    with pytest.raises(
        LayoutRecognitionError,
        match="title, price, capacity and color",
    ):
        position_detail_for_capture(
            page,
            title=_Locator(page, "title"),
            prices=(_Locator(page, "price"),),
            capacity=_Locator(page, "capacity"),
            color=_Locator(page, "color"),
            site_name="Tmall",
        )

    assert page.centered == ["capacity"]
    assert page.waits == [300]


def test_positions_a_no_model_result_by_its_card_so_the_name_is_brought_onscreen() -> None:
    page = _Page()
    product_name = _Locator(page, "title")
    product_card = _Locator(page, "card")

    position_result_cards_for_capture(
        page,
        product_name=product_name,
        product_card=product_card,
        site_name="JD",
    )

    assert page.centered == ["card"]
    assert "block: 'end'" in page.position_scripts[0]
    assert page.waits == [300]
    assert page.capture_scales == []


def test_search_anchor_result_capture_can_anchor_search_before_validating_card_name() -> None:
    page = _Page()
    search = _Locator(page, "search")
    title = _Locator(page, "title")
    card = _Locator(page, "card")
    page.in_viewport["title"] = True

    position_result_cards_for_capture(
        page,
        search_input=search,
        product_name=title,
        product_card=card,
        site_name="JD",
        prefer_search_anchor=True,
    )

    assert page.scroll_targets == ["search"]


def test_result_card_without_a_post_position_name_fails_closed() -> None:
    page = _Page()

    with pytest.raises(LayoutRecognitionError, match="product card name"):
        position_result_cards_for_capture(
            page,
            search_input=_Locator(page, "search"),
            product_name=None,
            product_card=_Locator(page, "card"),
            site_name="JD",
            prefer_search_anchor=True,
        )


def test_no_model_result_requires_search_input_and_card_name_in_viewport() -> None:
    page = _Page()

    with pytest.raises(LayoutRecognitionError, match="search input"):
        position_result_cards_for_capture(
            page,
            search_input=_Locator(page, "search"),
            product_name=_Locator(page, "title"),
            product_card=_Locator(page, "card"),
            site_name="JD",
        )


def test_no_model_result_accepts_search_input_and_card_name_together() -> None:
    page = _Page()
    page.in_viewport["search"] = True

    position_result_cards_for_capture(
        page,
        search_input=_Locator(page, "search"),
        product_name=_Locator(page, "title"),
        product_card=_Locator(page, "card"),
        site_name="JD",
    )

    assert page.centered == ["card"]


def test_jd_no_model_reduces_scale_until_search_and_all_results_are_visible() -> None:
    page = _Page(succeeds_at_scale=0.6)

    proof = fit_search_results_for_capture(
        page,
        search_input=_Locator(page, "search"),
        result_region=_Locator(page, "result"),
        site_name="JD",
        scales=(0.8, 0.7, 0.6, 0.5),
    )

    assert proof.inline_zoom == 0.6
    assert proof.computed_zoom == 0.6
    assert page.capture_scales == [0.8, 0.7, 0.6]
    assert page.scroll_targets == ["search", "search", "search"]


def test_jd_no_model_fits_real_search_cards_without_shrinking_for_recommendations() -> None:
    page = _Page(
        succeeds_at_scale=0.5,
        viewport_scale_thresholds={"search": 0.8, "card": 0.8},
    )

    proof = fit_search_results_for_capture(
        page,
        search_input=_Locator(page, "search"),
        result_region=_Locator(page, "result"),
        result_targets=(_Locator(page, "card"),),
        site_name="JD",
        scales=(0.8, 0.7, 0.6, 0.5),
    )

    assert proof.inline_zoom == 0.8
    assert proof.computed_zoom == 0.8
    assert page.capture_scales == [0.8]


def test_jd_no_model_fails_when_all_results_do_not_fit_even_at_minimum_scale() -> None:
    page = _Page(can_position=False, succeeds_at_scale=0.4)

    with pytest.raises(
        LayoutRecognitionError,
        match="search input and complete result region",
    ):
        fit_search_results_for_capture(
            page,
            search_input=_Locator(page, "search"),
            result_region=_Locator(page, "result"),
            site_name="JD",
            scales=(0.8, 0.7, 0.6, 0.5),
        )

    assert page.capture_scales == [0.8, 0.7, 0.6, 0.5]

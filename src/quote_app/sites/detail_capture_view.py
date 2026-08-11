from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from quote_app.tasks.retry import LayoutRecognitionError

_CENTER_IN_NEAREST_SCROLL_AREA = (
    "(element) => element.scrollIntoView({block: 'center', inline: 'nearest'})"
)
_ALIGN_RESULT_CARD_TO_VIEWPORT_BOTTOM = (
    "(element) => element.scrollIntoView({block: 'end', inline: 'nearest'})"
)
_ALIGN_SEARCH_TO_VIEWPORT_TOP = (
    "(element) => element.scrollIntoView({block: 'start', inline: 'nearest'})"
)
_IN_VIEWPORT = """
(element) => {
  const rect = element.getBoundingClientRect();
  return rect.width > 0
    && rect.height > 0
    && rect.top >= 0
    && rect.left >= 0
    && rect.bottom <= window.innerHeight
    && rect.right <= window.innerWidth;
}
"""
_POSITION_WAIT_MS = 300
_CAPTURE_SCALE_WAIT_MS = 300
_CAPTURE_SCALE_ATTRIBUTE = "data-quotation-capture-scale-original"
_CAPTURE_VIEW_SAFE_STAGES = frozenset(
    {"缩放验证", "搜索框定位", "结果区域定位"}
)
_APPLY_CAPTURE_SCALE = f"""
(scale) => {{
  const root = document.documentElement;
  const attribute = "{_CAPTURE_SCALE_ATTRIBUTE}";
  if (!root.hasAttribute(attribute)) {{
    root.setAttribute(attribute, root.style.zoom || "");
  }}
  root.style.zoom = String(scale);
  return {{
    inlineZoom: root.style.zoom,
    computedZoom: getComputedStyle(root).zoom,
  }};
}}
"""
_READ_CAPTURE_SCALE = """
() => {
  const root = document.documentElement;
  return {
    inlineZoom: root.style.zoom,
    computedZoom: getComputedStyle(root).zoom,
  };
}
"""
_RESTORE_CAPTURE_SCALE = f"""
() => {{
  const root = document.documentElement;
  const attribute = "{_CAPTURE_SCALE_ATTRIBUTE}";
  if (!root.hasAttribute(attribute)) return true;
  const original = root.getAttribute(attribute) || "";
  if (original) {{
    root.style.zoom = original;
  }} else {{
    root.style.removeProperty("zoom");
  }}
  root.removeAttribute(attribute);
  return !root.hasAttribute(attribute);
}}
"""


@dataclass(frozen=True, slots=True)
class CaptureScaleProof:
    inline_zoom: float
    computed_zoom: float
    sample_count: int


class CaptureViewGeometryError(LayoutRecognitionError):
    """A proven capture-only scale or viewport geometry failure."""

    def __init__(self, message: str, *, safe_stage: str) -> None:
        if safe_stage not in _CAPTURE_VIEW_SAFE_STAGES:
            raise ValueError("capture view safe stage is not approved")
        self.safe_stage = safe_stage
        super().__init__(message)


def apply_capture_scale(page: Any, *, scale: float) -> CaptureScaleProof:
    """Apply one reversible visual scale for the pending formal capture."""

    if not 0.5 <= scale <= 1.0:
        raise ValueError("capture scale must be between 0.5 and 1.0")
    raw_samples = [page.evaluate(_APPLY_CAPTURE_SCALE, scale)]
    page.wait_for_timeout(_CAPTURE_SCALE_WAIT_MS)
    raw_samples.append(page.evaluate(_READ_CAPTURE_SCALE))
    samples = tuple(_capture_scale_sample(sample) for sample in raw_samples)
    if not all(
        inline_zoom == scale and computed_zoom == scale
        for inline_zoom, computed_zoom in samples
    ):
        raise CaptureViewGeometryError(
            f"capture scale {scale} did not become visually stable",
            safe_stage="缩放验证",
        )
    inline_zoom, computed_zoom = samples[-1]
    return CaptureScaleProof(
        inline_zoom=inline_zoom,
        computed_zoom=computed_zoom,
        sample_count=len(samples),
    )


def ensure_capture_scale(page: Any, *, scale: float) -> CaptureScaleProof:
    """Ensure one capture scale without rewriting an already stable value."""

    if not 0.5 <= scale <= 1.0:
        raise ValueError("capture scale must be between 0.5 and 1.0")
    inline_zoom, computed_zoom = _capture_scale_sample(
        page.evaluate(_READ_CAPTURE_SCALE)
    )
    if inline_zoom == scale and computed_zoom == scale:
        return CaptureScaleProof(
            inline_zoom=inline_zoom,
            computed_zoom=computed_zoom,
            sample_count=1,
        )
    return apply_capture_scale(page, scale=scale)


def restore_capture_scale(page: Any) -> None:
    """Restore the page scale saved by :func:`apply_capture_scale`."""

    restored = page.evaluate(_RESTORE_CAPTURE_SCALE)
    if restored is not True:
        raise CaptureViewGeometryError(
            "capture scale could not be restored",
            safe_stage="缩放验证",
        )
    page.wait_for_timeout(_CAPTURE_SCALE_WAIT_MS)


def position_detail_for_capture(
    page: Any,
    *,
    title: Any,
    prices: Sequence[Any],
    capacity: Any,
    color: Any,
    site_name: str,
) -> None:
    """Keep one detail page and find a viewport suitable for formal capture.

    ``scrollIntoView`` scrolls the nearest scrollable ancestor first.  That is
    important for marketplace detail pages with an independently scrollable
    right-hand SKU panel.  We do not use document-level scrolling here because
    it can move the page while leaving that panel unchanged.
    """

    if not prices:
        raise CaptureViewGeometryError(
            f"{site_name} product detail has no visible selling price for capture",
            safe_stage="结果区域定位",
        )

    # Capacity is the lowest required SKU field on the current marketplace
    # layouts.  Center it once: with the fixed tall Mac browser frame this
    # keeps the title, price, colour and capacity together without the former
    # up/down recovery loop that could move a valid view away before capture.
    capacity.evaluate(_CENTER_IN_NEAREST_SCROLL_AREA)
    page.wait_for_timeout(_POSITION_WAIT_MS)
    if (
        _in_viewport(title, safe_stage="结果区域定位")
        and any(
            _in_viewport(price, safe_stage="结果区域定位")
            for price in prices
        )
        and _in_viewport(capacity, safe_stage="结果区域定位")
        and _in_viewport(color, safe_stage="结果区域定位")
    ):
        return

    raise CaptureViewGeometryError(
        f"{site_name} detail capture requires title, price, capacity and color "
        "in the same viewport",
        safe_stage="结果区域定位",
    )


def position_result_cards_for_capture(
    page: Any,
    *,
    search_input: Any | None = None,
    product_name: Any | None,
    product_card: Any | None,
    site_name: str,
    prefer_search_anchor: bool = False,
    product_name_reader: Callable[[], Any | None] | None = None,
    empty_state: Any | None = None,
) -> None:
    """Place a legal no-model result where its visible card name is readable.

    A no-model result can still contain related models.  The formal screenshot
    must therefore show the card titles, rather than only their images, so a
    reviewer can verify that the requested base model is absent.  Empty-result
    pages have no card title and are already self-explanatory, so they do not
    need an artificial scroll.
    """

    if product_card is None:
        if prefer_search_anchor and search_input is not None:
            search_input.evaluate(_ALIGN_SEARCH_TO_VIEWPORT_TOP)
            page.wait_for_timeout(_POSITION_WAIT_MS)
        if search_input is not None and not _in_viewport(
            search_input,
            safe_stage="搜索框定位",
        ):
            raise CaptureViewGeometryError(
                f"{site_name} no-model search input is not visible for capture",
                safe_stage="搜索框定位",
            )
        if prefer_search_anchor and (
            empty_state is None
            or not _in_viewport(empty_state, safe_stage="结果区域定位")
        ):
            raise CaptureViewGeometryError(
                f"{site_name} no-model empty state is not visible for capture",
                safe_stage="结果区域定位",
            )
        return
    # JD's no-model screenshot must establish the searched keyword first while
    # retaining the visible product title used to rule out the requested model.
    if prefer_search_anchor and search_input is not None:
        search_input.evaluate(_ALIGN_SEARCH_TO_VIEWPORT_TOP)
    else:
        # The default result-card framing retains Tmall's existing behavior.
        product_card.evaluate(_ALIGN_RESULT_CARD_TO_VIEWPORT_BOTTOM)
    page.wait_for_timeout(_POSITION_WAIT_MS)
    if search_input is not None and not _in_viewport(
        search_input,
        safe_stage="搜索框定位",
    ):
        raise CaptureViewGeometryError(
            f"{site_name} no-model search input is not visible for capture",
            safe_stage="搜索框定位",
        )
    if product_name_reader is not None:
        product_name = product_name_reader()
    if product_name is None or not _in_viewport(
        product_name,
        safe_stage="结果区域定位",
    ):
        raise CaptureViewGeometryError(
            f"{site_name} no-model product card name is not visible for capture",
            safe_stage="结果区域定位",
        )


def _in_viewport(locator: Any, *, safe_stage: str) -> bool:
    value = locator.evaluate(_IN_VIEWPORT)
    if type(value) is not bool:
        raise CaptureViewGeometryError(
            "detail capture viewport state is unavailable",
            safe_stage=safe_stage,
        )
    return value


def _capture_scale_sample(value: Any) -> tuple[float, float]:
    try:
        return float(value["inlineZoom"]), float(value["computedZoom"])
    except (KeyError, TypeError, ValueError):
        return float("nan"), float("nan")

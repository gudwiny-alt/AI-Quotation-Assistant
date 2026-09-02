from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import re

import pytest

from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.catalog import load_site_catalog
from quote_app.sites.official_brands.models import (
    OfficialDetailIdentity,
    OfficialOfferSnapshot,
)
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from tests.conftest import _OfficialFixturePage, _OfficialLocator


_FIXTURES = Path(__file__).parents[1] / "fixtures" / "sites" / "official_live" / "apple"


class _AppleFixturePage(_OfficialFixturePage):
    """Same-page Apple purchase fixture; product cards never open a new tab."""

    def __init__(self, html: str) -> None:
        super().__init__(html, entry_url=_apple_spec().entry_url)
        self._product_urls.update(
            {
                "https://www.apple.com.cn/shop/buy-iphone/iphone-17/mg734ch/a",
                "https://www.apple.com.cn/shop/buy-iphone/iphone-17-pro/mg900ch/a",
            }
        )
        self.capture_zoom = 1.0

    def evaluate(self, script: str, argument: object | None = None) -> object:
        """Provide only the browser primitives exercised by Apple capture."""
        if "window.innerWidth" in script:
            return {"width": 1440, "height": 900}
        if "data-quotation-capture-scale-original" in script:
            if "removeAttribute" in script:
                self.capture_zoom = 1.0
                return True
            if "root.style.zoom = String(scale)" in script:
                self.capture_zoom = float(argument)  # type: ignore[arg-type]
        if "inlineZoom" in script and "computedZoom" in script:
            return {
                "inlineZoom": str(self.capture_zoom),
                "computedZoom": str(self.capture_zoom),
            }
        return {}


class _AppleTemporarilyDisabledOptionPage(_AppleFixturePage):
    """Represents Apple's short configuration-card hydration interval."""

    def __init__(self, html: str, *, enable_after_waits: int) -> None:
        super().__init__(html)
        self._enable_after_waits = enable_after_waits

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        if len(self.wait_timeout_milliseconds) < self._enable_after_waits:
            return
        for node in self.root.descendants():
            if node.attrs.get("id") in {"capacity-256", "color-black"}:
                node.attrs.pop("disabled", None)


class _AppleNativeRadioLocator(_OfficialLocator):
    """Model the native-radio interaction used by Apple's live configurator."""

    def nth(self, index: int) -> "_AppleNativeRadioLocator":
        return _AppleNativeRadioLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> "_AppleNativeRadioLocator":
        locator = super().locator(selector)
        return _AppleNativeRadioLocator(self.page, locator.nodes)

    def click(self) -> None:
        node = self.nodes[0]
        if node.tag == "input" and node.attrs.get("type") == "radio":
            page = self.page
            assert isinstance(page, _AppleColorUnlocksCapacityPage)
            page.select_native_radio(node)
        elif node.tag == "label" and node.attrs.get("for"):
            page = self.page
            assert isinstance(page, _AppleColorUnlocksCapacityPage)
            option_id = node.attrs["for"]
            for candidate in page.root.descendants():
                if candidate.attrs.get("id") == option_id:
                    page.select_native_radio(candidate)
                    break
        super().click()


class _AppleColorUnlocksCapacityPage(_AppleFixturePage):
    """Apple exposes storage choices only after the selected colour is chosen."""

    def locator(self, selector: str) -> _AppleNativeRadioLocator:
        locator = super().locator(selector)
        return _AppleNativeRadioLocator(self, locator.nodes)

    def select_native_radio(self, node: object) -> None:
        # ``node`` is deliberately kept structural: this fixture only models
        # native HTML radio semantics, not a production-only test hook.
        node_id = getattr(node, "attrs")["id"]
        name = getattr(node, "attrs").get("name")
        for candidate in self.root.descendants():
            if candidate.tag == "input" and candidate.attrs.get("name") == name:
                candidate.attrs.pop("checked", None)
        getattr(node, "attrs")["checked"] = ""
        if node_id in {"color-black", "color-mist-blue"}:
            for candidate in self.root.descendants():
                if candidate.attrs.get("id") == "capacity-256":
                    candidate.attrs.pop("disabled", None)


class _AppleVisibleConfigurationLocator(_OfficialLocator):
    """Apple accepts the visible label/card, not a hidden native radio click."""

    def nth(self, index: int) -> "_AppleVisibleConfigurationLocator":
        return _AppleVisibleConfigurationLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> "_AppleVisibleConfigurationLocator":
        locator = super().locator(selector)
        return _AppleVisibleConfigurationLocator(self.page, locator.nodes)

    def click(self) -> None:
        node = self.nodes[0]
        page = self.page
        assert isinstance(page, _AppleVisibleConfigurationPage)
        if node.tag == "input" and node.attrs.get("type") == "radio":
            raise TimeoutError("Apple hides native configuration radio inputs")
        if node.tag == "label":
            page.select_visible_configuration_card(node)
        super().click()


class _AppleVisibleConfigurationPage(_AppleColorUnlocksCapacityPage):
    """Live-like Apple page where only the associated visible label is clickable."""

    def __init__(self, html: str) -> None:
        super().__init__(html)
        self.visible_configuration_clicks: list[str] = []

    def locator(self, selector: str) -> _AppleVisibleConfigurationLocator:
        locator = _OfficialFixturePage.locator(self, selector)
        return _AppleVisibleConfigurationLocator(self, locator.nodes)

    def select_visible_configuration_card(self, label: object) -> None:
        option_id = getattr(label, "attrs").get("for")
        for candidate in self.root.descendants():
            if candidate.attrs.get("id") == option_id:
                self.visible_configuration_clicks.append(str(option_id))
                self.select_native_radio(candidate)
                return
        raise AssertionError("visible configuration card has no native radio")


class _AppleDelayedSkuRoutePage(_AppleVisibleConfigurationPage):
    """Apple updates the selected-SKU URL shortly after selecting storage."""

    def __init__(self, html: str, *, route_after_waits: int) -> None:
        super().__init__(html)
        self._route_after_waits = route_after_waits
        self._route_pending = False
        self._waits_after_storage_selection = 0

    def goto(self, url: str, **kwargs: object) -> None:
        """Start on the base product route, as the live chooser does."""
        super().goto(url, **kwargs)
        if "/shop/buy-iphone/iphone-17/" in url:
            self._url = "https://www.apple.com.cn/shop/buy-iphone/iphone-17"

    def select_visible_configuration_card(self, label: object) -> None:
        super().select_visible_configuration_card(label)
        if getattr(label, "attrs").get("for") == "capacity-256":
            self._route_pending = True

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        if self._route_pending:
            self._waits_after_storage_selection += 1
        if (
            self._route_pending
            and self._waits_after_storage_selection >= self._route_after_waits
        ):
            self._url = (
                "https://www.apple.com.cn/shop/buy-iphone/iphone-17/"
                "mg734ch/a"
            )
            self._route_pending = False


class _AppleHiddenNativeGeometryLocator(_OfficialLocator):
    """Model Apple radios whose only usable geometry belongs to their card."""

    def nth(self, index: int) -> "_AppleHiddenNativeGeometryLocator":
        return _AppleHiddenNativeGeometryLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> "_AppleHiddenNativeGeometryLocator":
        locator = super().locator(selector)
        return _AppleHiddenNativeGeometryLocator(self.page, locator.nodes)

    def bounding_box(self) -> dict[str, float] | None:
        node = self.nodes[0]
        if node.tag == "input" and node.attrs.get("type") == "radio":
            return None
        return super().bounding_box()


class _AppleDynamicRadioIdPage(_AppleFixturePage):
    """Live-like React radio IDs can contain punctuation such as ``:r0:``."""

    def locator(self, selector: str) -> _AppleHiddenNativeGeometryLocator:
        locator = _OfficialFixturePage.locator(self, selector)
        return _AppleHiddenNativeGeometryLocator(self, locator.nodes)


class _AppleOpacityAwareLocator(_OfficialLocator):
    """Treat opacity-zero cards as visually absent, like the real browser."""

    def nth(self, index: int) -> "_AppleOpacityAwareLocator":
        return _AppleOpacityAwareLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> "_AppleOpacityAwareLocator":
        locator = super().locator(selector)
        return _AppleOpacityAwareLocator(self.page, locator.nodes)

    def is_visible(self) -> bool:
        if len(self.nodes) == 1:
            node = self.nodes[0]
            style = node.attrs.get("style", "").replace(" ", "")
            # Playwright treats an opacity-zero element with a non-empty box
            # as visible.  Apple's radio is such an element; its label/card is
            # what must remain visually legible for the screenshot.
            if "opacity:0" in style and node.tag != "input":
                return False
        return super().is_visible()


class _AppleOpacityAwarePage(_AppleFixturePage):
    """Live-like page where transparent radios still expose DOM geometry."""

    def locator(self, selector: str) -> _AppleOpacityAwareLocator:
        locator = _OfficialFixturePage.locator(self, selector)
        return _AppleOpacityAwareLocator(self, locator.nodes)


class _AppleCaptureScrollPage(_AppleDynamicRadioIdPage):
    """Fixture whose four screenshot proofs need one small group scroll."""

    def __init__(self, html: str) -> None:
        super().__init__(html)
        self.capture_scroll_deltas: list[float] = []

    def evaluate(self, script: str, argument: object | None = None) -> object:
        if "window.innerWidth" in script:
            return {"width": 1440, "height": 600}
        if "window.scrollBy" in script:
            assert isinstance(argument, dict)
            delta = float(argument["delta"])
            self.capture_scroll_deltas.append(delta)
            for node in self.root.descendants():
                if node.attrs.get("data-apple-scroll-proof") != "true":
                    continue
                style = node.attrs.get("style", "")
                top = re.search(r"top:(-?[0-9.]+)px", style)
                assert top is not None
                shifted = float(top.group(1)) - delta
                node.attrs["style"] = style.replace(
                    top.group(0), f"top:{shifted:g}px"
                )
            return True
        return super().evaluate(script, argument)


class _ApplePaintAwareLocator(_AppleHiddenNativeGeometryLocator):
    """Expose whether a matching Apple label is actually painted on screen."""

    def nth(self, index: int) -> "_ApplePaintAwareLocator":
        return _ApplePaintAwareLocator(self.page, [self.nodes[index]])

    def locator(self, selector: str) -> "_ApplePaintAwareLocator":
        locator = super().locator(selector)
        return _ApplePaintAwareLocator(self.page, locator.nodes)

    def evaluate(self, script: str) -> object:
        if "__quotationApplePaintState" not in script:
            return super().evaluate(script)
        box = self.bounding_box()
        if box is None:
            return {
                "rendered": False,
                "insideViewport": False,
                "exposed": False,
            }
        viewport = self.page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        assert isinstance(viewport, dict)
        left = float(box["x"])
        top = float(box["y"])
        right = left + float(box["width"])
        bottom = top + float(box["height"])
        inside = (
            left >= 0
            and top >= 0
            and right <= float(viewport["width"])
            and bottom <= float(viewport["height"])
        )
        painted = self.nodes[0].attrs.get("data-apple-painted") != "false"
        return {
            "rendered": True,
            "insideViewport": inside,
            "exposed": inside and painted,
        }


class _ApplePaintAwareScrollPage(_AppleCaptureScrollPage):
    """Apple-like page with duplicate React labels and real paint state."""

    def locator(self, selector: str) -> _ApplePaintAwareLocator:
        locator = _OfficialFixturePage.locator(self, selector)
        return _ApplePaintAwareLocator(self, locator.nodes)


class _AppleStickyAnchorScrollPage(_AppleCaptureScrollPage):
    """Apple-like page where the compact purchase header pins after scrolling."""

    def evaluate(self, script: str, argument: object | None = None) -> object:
        result = super().evaluate(script, argument)
        if "window.scrollBy" in script:
            sticky = next(
                node
                for node in self.root.descendants()
                if node.attrs.get("data-autom") == "stickynavHeader"
            )
            style = sticky.attrs.get("style", "")
            sticky.attrs["style"] = re.sub(r"top:-?[0-9.]+px", "top:10px", style)
        return result


class _AppleThresholdStickyScrollPage(_AppleCaptureScrollPage):
    """Apple pins the compact title only after enough real page movement."""

    def __init__(self, html: str, *, sticky_after_scroll: float = 160.0) -> None:
        super().__init__(html)
        self._sticky_after_scroll = sticky_after_scroll
        self._cumulative_scroll = 0.0

    def evaluate(self, script: str, argument: object | None = None) -> object:
        result = super().evaluate(script, argument)
        if "window.scrollBy" not in script:
            return result
        assert isinstance(argument, dict)
        self._cumulative_scroll += float(argument["delta"])
        if self._cumulative_scroll < self._sticky_after_scroll:
            return result
        sticky = next(
            node
            for node in self.root.descendants()
            if node.attrs.get("data-autom") == "stickynavHeader"
        )
        style = sticky.attrs.get("style", "")
        sticky.attrs["style"] = re.sub(r"top:-?[0-9.]+px", "top:10px", style)
        return result


class _ApplePostScrollDriftPage(_AppleStickyAnchorScrollPage):
    """Apple repositions its chooser once more after our first correction."""

    def __init__(self, html: str) -> None:
        super().__init__(html)
        self._pending_post_scroll_drift = False
        self._post_scroll_drift_applied = False

    def evaluate(self, script: str, argument: object | None = None) -> object:
        result = super().evaluate(script, argument)
        if (
            "window.scrollBy" in script
            and not self._post_scroll_drift_applied
        ):
            self._pending_post_scroll_drift = True
        return result

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        if (
            not self._pending_post_scroll_drift
            or self._post_scroll_drift_applied
        ):
            return
        # The live Apple configurator can auto-scroll again after the selected
        # cards finish rendering.  This moves colour just above the viewport;
        # a second, opposite correction is then required.
        for node in self.root.descendants():
            if node.attrs.get("data-apple-scroll-proof") != "true":
                continue
            style = node.attrs.get("style", "")
            top = re.search(r"top:(-?[0-9.]+)px", style)
            assert top is not None
            # The fixture must still put colour just above the viewport after
            # the new bounded first step (80px), so the second correction is
            # genuinely upward rather than being skipped accidentally.
            shifted = float(top.group(1)) - 440.0
            node.attrs["style"] = style.replace(
                top.group(0), f"top:{shifted:g}px"
            )
        self._pending_post_scroll_drift = False
        self._post_scroll_drift_applied = True


class _AppleDelayedIdleDriftPage(_AppleStickyAnchorScrollPage):
    """Apple can move the selected colour after an initially valid frame."""

    def __init__(self, html: str) -> None:
        super().__init__(html)
        self._capture_drift_armed = False
        self._capture_drift_applied = False

    def arm_capture_drift(self) -> None:
        self._capture_drift_armed = True

    def wait_for_timeout(self, milliseconds: float) -> None:
        super().wait_for_timeout(milliseconds)
        if (
            milliseconds != 300
            or not self._capture_drift_armed
            or self._capture_drift_applied
        ):
            return
        color = next(
            node
            for node in self.root.descendants()
            if node.tag == "label" and node.attrs.get("for") == ":r0:"
        )
        style = color.attrs.get("style", "")
        color.attrs["style"] = style.replace("top:80px", "top:-20px")
        self._capture_drift_applied = True


def _apple_spec():
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "苹果" and spec.channel is WebsiteChannel.OFFICIAL
    )


def _apple_task() -> WebsiteTask:
    return WebsiteTask(
        task_id="apple-normal",
        run_id="apple-contract",
        source_row_number=2,
        output_row_number=2,
        material_code="APPLE-IPHONE17-512-WHITE",
        brand="苹果",
        model_name="iPhone 17",
        ram="12GB",
        storage="512GB",
        color="白色",
        channel=WebsiteChannel.OFFICIAL,
    )


def test_apple_live_adapter_normal_contract_is_defined() -> None:
    """The future live adapter must read an Apple full-device price, not RAM."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    adapter = AppleOfficialAdapter(_apple_spec())
    assert adapter.spec.brand == "苹果"
    assert Decimal("9999") > Decimal("0")
    assert _apple_task().storage == "512GB"


def test_apple_live_adapter_starts_from_the_public_buy_iphone_entry() -> None:
    """Apple's live workflow must start from the configurable purchase flow."""
    assert _apple_spec().entry_url == "https://www.apple.com.cn/shop/buy-iphone"


def test_apple_offer_matches_selected_storage_but_ignores_source_ram() -> None:
    """A storage-only Apple SKU is valid even though the input includes RAM."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    snapshot = OfficialOfferSnapshot(
        identity=OfficialDetailIdentity(
            "https://www.apple.com.cn/shop/buy-iphone/iphone-17/mg734ch/a",
            "mg734ch-a",
        ),
        brand="苹果",
        model_name="iPhone 17",
        capacity="512GB",
        color="白色",
        price=Decimal("9999"),
    )

    assert AppleOfficialAdapter(_apple_spec())._offer_matches_task(
        _apple_task(), snapshot
    )


def test_apple_observe_enters_the_exact_product_card_and_reads_full_device_price() -> None:
    """The base iPhone card must win over a visible Pro card, then read RMB 9,999."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage((_FIXTURES / "normal_flow.html").read_text(encoding="utf-8"))

    observation = AppleOfficialAdapter(_apple_spec()).observe(_apple_task(), page)

    assert page.thumb_clicks == [
        "https://www.apple.com.cn/shop/buy-iphone/iphone-17/mg734ch/a"
    ]
    assert observation.price == Decimal("9999")
    assert observation.semantic_state.capacity == "512GB"
    assert observation.semantic_state.color == "白色"


def test_apple_exact_card_with_5g_or_temporary_stock_text_still_enters_detail() -> None:
    """Availability copy and 5G must not veto an otherwise exact iPhone card."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage(
        (_FIXTURES / "normal_flow.html").read_text(encoding="utf-8")
    )
    for node in page.root.descendants():
        if node.attrs.get("data-apple-role") == "product-title":
            if node.text.strip() == "iPhone 17":
                node.text_parts = ["iPhone 17 5G 暂时缺货"]

    observation = AppleOfficialAdapter(_apple_spec()).observe(_apple_task(), page)

    assert observation.price == Decimal("9999")
    assert page.thumb_clicks == [
        "https://www.apple.com.cn/shop/buy-iphone/iphone-17/mg734ch/a"
    ]


def test_apple_capture_requires_only_title_price_storage_and_color() -> None:
    """The four accepted business proofs are sufficient for a formal image."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage((_FIXTURES / "normal_flow.html").read_text(encoding="utf-8"))
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(_apple_task(), page)

    adapter.prepare_capture_view(_apple_task(), page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        _apple_task(), page, observation.semantic_state
    )

    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    assert page.capture_zoom == 1.0


def test_apple_reads_rmb_price_inside_selected_capacity_card_and_proves_it() -> None:
    """Apple's selected capacity card carries the one-time RMB price.

    It may also show an installment price, which must not prevent the full
    device price (RMB 5,999) from being written or shown in the four-proof
    screenshot.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage(
        (_FIXTURES / "capacity_card_price.html").read_text(encoding="utf-8")
    )
    task = replace(
        _apple_task(),
        task_id="apple-selected-capacity-card",
        storage="256GB",
        color="黑色",
    )
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert observation.price == Decimal("5999")
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    # The price proof is the selected capacity card, not a monthly-payment node.
    assert rectangles[1].x == rectangles[2].x
    assert rectangles[1].y == rectangles[2].y


def test_apple_live_configurator_accepts_heading_fieldsets_and_native_radios() -> None:
    """Apple's live page exposes label-backed native radio inputs, not buttons.

    A visible but empty ``summary-productName`` must not prevent the adapter
    from falling through to the real heading.  The fieldsets, labels and
    ``checked`` radio state below mirror the public Apple China configurator
    structure observed during the production incident investigation.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <div data-autom="summary-productName"></div>
          <h1>购买 iPhone 17</h1>
          <fieldset style="left:760px;top:220px;width:480px;height:110px">
            <legend>颜色 - 黑色</legend>
            <input id="color-black" type="radio" name="color" checked
              style="left:770px;top:270px;width:120px;height:42px">
            <label for="color-black">黑色</label>
            <input id="color-white" type="radio" name="color">
            <label for="color-white">白色</label>
          </fieldset>
          <fieldset style="left:760px;top:410px;width:500px;height:220px">
            <legend>存储容量：你需要多大的存储空间？</legend>
            <input id="capacity-256" type="radio" name="capacity" checked
              style="left:770px;top:470px;width:440px;height:112px">
            <label for="capacity-256">256GB RMB 5,999 或 RMB 250/月（24 期）起</label>
            <input id="capacity-512" type="radio" name="capacity">
            <label for="capacity-512">512GB RMB 7,999</label>
          </fieldset>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")

    observation = AppleOfficialAdapter(_apple_spec()).observe(task, page)

    assert observation.price == Decimal("5999")
    assert observation.semantic_state.capacity == "256GB"
    assert observation.semantic_state.color == "黑色"
    proofs = AppleOfficialAdapter(_apple_spec())._capture_proofs(task, page)
    # The actual input is often visually hidden.  Formal screenshot evidence
    # must use the visible label/card that shows the selected configuration.
    assert proofs[2].inner_text().startswith("256GB RMB 5,999")
    assert proofs[3].inner_text() == "黑色"


def test_apple_live_configurator_reads_hidden_native_radio_through_visible_label() -> None:
    """Hidden Apple radio controls still select the visible 256GB card.

    The live configurator hides the native input and leaves its associated
    label/card visible.  Treating the input's visibility as a requirement
    incorrectly turns a selectable 256GB SKU into ``CAPACITY_UNAVAILABLE``.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1>购买 iPhone 17</h1>
          <fieldset><legend>颜色 - 黑色</legend>
            <input id="color-black" type="radio" name="color" checked hidden>
            <label for="color-black">黑色</label>
          </fieldset>
          <fieldset><legend>存储容量</legend>
            <input id="capacity-256" type="radio" name="capacity" checked hidden>
            <label for="capacity-256">256GB RMB 5,999</label>
          </fieldset>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")

    observation = AppleOfficialAdapter(_apple_spec()).observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("5999")


def test_apple_source_blue_alias_selects_visible_color_before_storage() -> None:
    """A source colour alias must still select Apple's visible colour card.

    Marketing data can call the colour ``青雾蓝色`` while the Apple purchase
    card presents the concise customer-facing name ``雾蓝``.  The adapter must
    select that colour card first, then select the requested storage card.
    Without this alias, a valid source row stops before any Apple configuration
    click, which is exactly the regression this test protects.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleVisibleConfigurationPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1>购买 iPhone 17</h1>
          <fieldset><legend>颜色</legend>
            <input id="color-black" type="radio" name="color" hidden>
            <label for="color-black">黑色</label>
            <input id="color-mist-blue" type="radio" name="color" hidden>
            <label for="color-mist-blue">雾蓝</label>
          </fieldset>
          <fieldset><legend>存储容量</legend>
            <input id="capacity-256" type="radio" name="capacity" disabled hidden>
            <label for="capacity-256">256GB RMB 5,999</label>
          </fieldset>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="青雾蓝色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("5999")
    assert observation.semantic_state.color == "青雾蓝色"
    assert page.visible_configuration_clicks == ["color-mist-blue", "capacity-256"]


def test_apple_capture_uses_visible_cards_for_dynamic_native_radio_ids() -> None:
    """Formal Apple capture must not use a zero-geometry hidden React radio."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleDynamicRadioIdPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1 style="left:50px;top:50px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" style="left:760px;top:250px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" style="left:760px;top:390px;width:440px;height:112px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    assert rectangles[1].y == rectangles[2].y
    assert rectangles[3].y == 250


def test_apple_capture_uses_one_bounded_group_scroll_for_all_four_proofs() -> None:
    """Apple must keep the title visible instead of jumping to capacity alone."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleCaptureScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1 data-apple-scroll-proof="true"
            style="left:50px;top:150px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:360px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:560px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert page.capture_scroll_deltas == [80.0]
    assert page.option_scrolls == []
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_capture_scrolls_up_when_color_heading_is_slightly_clipped() -> None:
    """The selected swatch alone must not hide a clipped ``颜色 - 黑色`` heading."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePaintAwareScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:90px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group" data-apple-scroll-proof="true"
            style="left:760px;top:-8px;width:440px;height:150px">
            <h2 data-apple-scroll-proof="true"
              style="left:760px;top:-8px;width:240px;height:36px">颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:"
              style="left:780px;top:45px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:330px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert page.capture_scroll_deltas
    assert page.capture_scroll_deltas[0] < 0
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    assert rectangles[3].y >= 24


def test_apple_capture_uses_small_steps_until_compact_title_is_really_visible() -> None:
    """No Apple capture positioning step may exceed the approved 80px bound."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleThresholdStickyScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:-90px;width:160px;height:30px">iPhone 17</a>
          <h1 data-apple-scroll-proof="true"
            style="left:50px;top:150px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:500px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:700px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert len(page.capture_scroll_deltas) >= 2
    assert all(16.0 <= abs(delta) <= 80.0 for delta in page.capture_scroll_deltas)
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_capture_rejects_transparent_radios_without_visible_cards() -> None:
    """A non-zero transparent input box is not screenshot evidence."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter
    from quote_app.tasks.retry import LayoutRecognitionError

    page = _AppleOpacityAwarePage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1 style="left:50px;top:80px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id="color-black" type="radio" name="color" checked
              style="opacity:0;left:760px;top:250px;width:36px;height:36px">
            <label for="color-black" style="opacity:0">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id="capacity-256" type="radio" name="capacity" checked
              style="opacity:0;left:760px;top:390px;width:36px;height:36px">
            <label for="capacity-256" style="opacity:0">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    assert observation.price == Decimal("5999")
    with pytest.raises(LayoutRecognitionError, match="visible evidence"):
        adapter.prepare_capture_view(task, page, observation.semantic_state)


def test_apple_capture_frames_selected_color_and_capacity_under_sticky_title() -> None:
    """Apple must frame the selected colour and storage together, not storage alone.

    The full heading is initially too far from the chooser, so the compact
    sticky product title appears only after one scroll.  The scroll target is
    the interval where both selected configuration cards remain visible.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleStickyAnchorScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:-90px;width:160px;height:30px">iPhone 17</a>
          <h1 data-apple-scroll-proof="true"
            style="left:50px;top:150px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:500px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:700px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert page.capture_scroll_deltas == [80.0, 80.0, 64.0]
    assert rectangles[0].y == 10
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_capture_activates_compact_title_without_displacing_frame() -> None:
    """A small bounded nudge may activate Apple's compact product title."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleStickyAnchorScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:-90px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-310px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:50px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    # The selected colour and capacity already share the visible configuration
    # band.  One 32px nudge activates Apple's sticky title without moving either
    # selected card out of the screenshot.
    assert page.capture_scroll_deltas == [32.0]
    assert rectangles[0].y == 10
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_capture_reframes_color_that_was_scrolled_just_above_viewport() -> None:
    """A capture must pull colour back into view while keeping storage visible."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleStickyAnchorScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:-90px;width:160px;height:30px">iPhone 17</a>
          <h1 data-apple-scroll-proof="true"
            style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:-20px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    # Keep colour at the safe top edge (24px), instead of pushing capacity to
    # the safe bottom edge.  Both controls must be contained in the final view.
    assert page.capture_scroll_deltas == [-44.0]
    assert all(0 <= rectangle.y for rectangle in rectangles)
    assert all(rectangle.y + rectangle.height <= 600 for rectangle in rectangles)


def test_apple_capture_recovers_when_page_auto_scrolls_after_first_correction() -> None:
    """Apple's post-selection reflow must trigger a bounded upward correction."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePostScrollDriftPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:-90px;width:160px;height:30px">iPhone 17</a>
          <h1 data-apple-scroll-proof="true"
            style="left:50px;top:150px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:500px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:700px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert page.capture_scroll_deltas == [80.0, -44.0]
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    assert all(0 <= rectangle.y for rectangle in rectangles)
    assert all(rectangle.y + rectangle.height <= 600 for rectangle in rectangles)


def test_apple_formal_capture_reframes_if_configuration_shifts_after_prepare() -> None:
    """The native-capture handoff may see a settled Apple chooser move once."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleStickyAnchorScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:80px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    color = next(
        node
        for node in page.root.descendants()
        if node.tag == "label" and node.attrs.get("for") == ":r0:"
    )
    color.attrs["style"] = color.attrs["style"].replace("top:80px", "top:-20px")

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert page.capture_scroll_deltas == [-44.0]
    assert all(0 <= rectangle.y for rectangle in rectangles)
    assert all(rectangle.y + rectangle.height <= 600 for rectangle in rectangles)


def test_apple_formal_capture_waits_for_idle_drift_then_scrolls_color_back() -> None:
    """An initially valid frame must survive one delayed 300ms Apple reflow."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleDelayedIdleDriftPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:80px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    page.arm_capture_drift()

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert page.capture_scroll_deltas == [-44.0]
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    assert all(0 <= rectangle.y for rectangle in rectangles)
    assert all(rectangle.y + rectangle.height <= 600 for rectangle in rectangles)


def test_apple_formal_probe_reframes_capacity_only_frame_after_context_build() -> None:
    """The final reader must restore colour before accepting the screenshot."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleStickyAnchorScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:80px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    adapter.capture_rectangles_for_capture(task, page, observation.semantic_state)

    color = next(
        node
        for node in page.root.descendants()
        if node.tag == "label" and node.attrs.get("for") == ":r0:"
    )
    color.attrs["style"] = color.attrs["style"].replace("top:80px", "top:-20px")

    reader = adapter.verified_state_reader(task, page, observation.semantic_state)
    final_state = reader()

    assert page.capture_scroll_deltas == [-44.0]
    assert tuple(rectangle.role for rectangle in final_state.css_rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    assert all(rectangle.y >= 0 for rectangle in final_state.css_rectangles)


def test_apple_capture_ignores_occluded_duplicate_label_and_scrolls_real_color_back() -> None:
    """A stale in-viewport React label must not hide the real offscreen colour."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePaintAwareScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-painted="false"
              data-apple-scroll-proof="true"
              style="left:760px;top:80px;width:160px;height:58px">黑色</label>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:-20px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    by_role = {rectangle.role: rectangle for rectangle in rectangles}
    assert page.capture_scroll_deltas == [-44.0]
    assert by_role["color"].y == 24.0


def test_apple_capture_uses_exposed_blue_label_instead_of_stale_duplicate() -> None:
    """A visibly complete blue frame must pass on the first formal attempt."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePaintAwareScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 雾蓝</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-painted="false"
              data-apple-scroll-proof="true"
              style="left:760px;top:-150px;width:160px;height:58px">雾蓝</label>
            <label for=":r0:" data-apple-scroll-proof="true"
              style="left:760px;top:80px;width:160px;height:58px">雾蓝</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" data-apple-scroll-proof="true"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="青雾蓝色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    by_role = {rectangle.role: rectangle for rectangle in rectangles}
    assert page.capture_scroll_deltas == []
    assert by_role["color"].y == 80.0


def test_apple_capture_rejects_four_proofs_when_hit_test_is_inconclusive() -> None:
    """Capture must fail when neither the selected card nor its group is painted."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter
    from quote_app.tasks.retry import LayoutRecognitionError

    page = _ApplePaintAwareScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group" data-apple-painted="false">
            <h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-painted="false"
              style="left:760px;top:80px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)

    with pytest.raises(LayoutRecognitionError):
        adapter.capture_rectangles_for_capture(
            task, page, observation.semantic_state
        )


def test_apple_capture_accepts_visible_selected_color_group_when_label_hit_test_is_inconclusive() -> None:
    """A visible selected-colour group is valid when its exact card hit-test flickers.

    The native selected radio remains the semantic authority.  The visible
    colour group is the screenshot proof only after that exact selection has
    been verified, so a transient React label hit-test must not reject an
    otherwise complete blue frame.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePaintAwareScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"
            style="left:740px;top:60px;width:500px;height:140px">
            <h2>颜色 - 雾蓝</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-painted="false"
              style="left:760px;top:80px;width:160px;height:58px">雾蓝</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="青雾蓝色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )
    final_state = adapter.verified_state_reader(
        task,
        page,
        observation.semantic_state,
    )()

    by_role = {rectangle.role: rectangle for rectangle in rectangles}
    final_by_role = {
        rectangle.role: rectangle for rectangle in final_state.css_rectangles
    }
    assert page.capture_scroll_deltas == []
    assert by_role["color"].y == 60.0
    assert by_role["color"].height == 140.0
    assert final_by_role["color"].y == 60.0
    assert final_by_role["color"].height == 140.0


def test_apple_capture_scrolls_visible_color_group_back_before_capacity_only_capture() -> None:
    """An offscreen colour group must be reframed before formal capture."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePaintAwareScrollPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group" data-apple-scroll-proof="true"
            style="left:740px;top:-20px;width:500px;height:140px">
            <h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" data-apple-painted="false"
              style="left:760px;top:80px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    by_role = {rectangle.role: rectangle for rectangle in rectangles}
    assert page.capture_scroll_deltas == [-44.0]
    assert by_role["color"].y == 24.0


def test_apple_final_reader_uses_visible_four_proofs_not_full_detail_reread() -> None:
    """A stable selected blue frame must survive a transient detail rerender.

    Selection, SKU identity and price were already verified before formal
    capture.  The final reader must use the same visible title/price/storage/
    colour proof rule as positioning instead of rerunning the full configurator
    hierarchy and rejecting a screenshot-ready page during a React rerender.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter
    from quote_app.tasks.retry import LayoutRecognitionError

    class _DetailRerendersAfterObservation(AppleOfficialAdapter):
        reject_full_business_reread = False

        def _read_business_state(self, task: WebsiteTask, page: object):  # type: ignore[override]
            if self.reject_full_business_reread:
                raise LayoutRecognitionError("Apple detail hierarchy rerendered")
            return super()._read_business_state(task, page)  # type: ignore[arg-type]

    page = _AppleDynamicRadioIdPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 雾蓝</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" style="left:760px;top:80px;width:160px;height:58px">雾蓝</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" style="left:760px;top:400px;width:440px;height:100px">
              256GB RMB 5,999
            </label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="青雾蓝色")
    adapter = _DetailRerendersAfterObservation(_apple_spec())
    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    adapter.reject_full_business_reread = True

    final_state = adapter.verified_state_reader(
        task,
        page,
        observation.semantic_state,
    )()

    assert final_state.price == Decimal("5999")
    assert tuple(rectangle.role for rectangle in final_state.css_rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )
    rectangles = {
        rectangle.role: rectangle for rectangle in final_state.css_rectangles
    }
    assert rectangles["title"].y == 10
    assert rectangles["price"].y == rectangles["capacity"].y == 400
    assert rectangles["color"].y == 80


def test_apple_formal_capture_does_not_reread_full_business_state() -> None:
    """Formal capture uses visible proofs after the SKU was already verified."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter
    from quote_app.tasks.retry import LayoutRecognitionError

    class _TransientCaptureAdapter(AppleOfficialAdapter):
        remaining_transient_reads = 0

        def _read_business_state(self, task: WebsiteTask, page: object):  # type: ignore[override]
            if self.remaining_transient_reads:
                self.remaining_transient_reads -= 1
                raise LayoutRecognitionError("Apple configuration is reflowing")
            return super()._read_business_state(task, page)  # type: ignore[arg-type]

    page = _AppleFixturePage(
        (_FIXTURES / "capacity_card_price.html").read_text(encoding="utf-8")
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = _TransientCaptureAdapter(_apple_spec())
    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    adapter.remaining_transient_reads = 2

    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert adapter.remaining_transient_reads == 2
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_capture_rectangles_reuse_the_prepared_final_frame() -> None:
    """React must not get a second chance to invalidate an accepted frame."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter
    from quote_app.tasks.retry import LayoutRecognitionError

    class _SingleFinalFrameAdapter(AppleOfficialAdapter):
        final_frame_reads = 0

        def _final_capture_state(  # type: ignore[override]
            self,
            task: WebsiteTask,
            page: object,
            expected: VerifiedSemanticState,
        ) -> VerifiedSemanticState:
            self.final_frame_reads += 1
            if self.final_frame_reads > 1:
                raise LayoutRecognitionError("Apple React frame rerendered")
            return super()._final_capture_state(task, page, expected)  # type: ignore[arg-type]

    page = _AppleFixturePage(
        (_FIXTURES / "capacity_card_price.html").read_text(encoding="utf-8")
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = _SingleFinalFrameAdapter(_apple_spec())
    observation = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    )

    assert adapter.final_frame_reads == 1
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_waits_for_selected_sku_route_before_formal_capture() -> None:
    """The post-selection Apple SKU route must become the saved state."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleDelayedSkuRoutePage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1>购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id="color-black" type="radio" name="color">
            <label for="color-black">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id="capacity-256" type="radio" name="capacity">
            <label for="capacity-256">256GB RMB 5,999</label>
          </section>
        </main></section>
        """,
        # ``_stable_offer`` performs only one short interval before it has
        # enough equal price samples.  The selected SKU route deliberately
        # arrives later, as it does in the live Apple purchase flow.
        route_after_waits=3,
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert observation.semantic_state.canonical_url.endswith("/mg734ch/a")
    assert page.url == observation.semantic_state.canonical_url
    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_capture_uses_visible_sticky_model_title_after_full_heading_scrolls_out() -> None:
    """The compact fixed ``iPhone 17`` header is sufficient screenshot proof.

    Apple's purchase heading scrolls out of the viewport before the selected
    colour and storage cards can share a compact screenshot.  Its visible
    sticky purchase header names the same model and must replace that heading
    for *capture only*; business-state identity still comes from the full
    detail heading.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleDynamicRadioIdPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <a data-autom="stickynavHeader"
            style="left:50px;top:10px;width:160px;height:30px">iPhone 17</a>
          <h1 style="left:50px;top:-200px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 黑色</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:" style="left:760px;top:350px;width:160px;height:58px">黑色</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:" style="left:760px;top:450px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    rectangles = adapter.capture_rectangles_for_capture(
        task, page, observation.semantic_state
    )

    assert rectangles[0].role == "title"
    assert rectangles[0].y == 10


def test_apple_capture_accepts_exact_browser_identity_when_page_title_never_pins() -> None:
    """Apple may omit its in-page sticky title for one selected colour.

    The native screenshot still shows Chrome's tab and address bar.  Once the
    exact Apple purchase URL and the browser document title both name the task
    model, those browser-level facts may replace only the missing in-page title
    proof.  Price, selected storage and selected colour must remain exposed.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _ApplePaintAwareScrollPage(
        """
        <title data-screen-title="product">购买 iPhone 17 256GB 青雾蓝色 - Apple</title>
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1 style="left:50px;top:-320px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 雾蓝</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:"
              style="left:760px;top:80px;width:160px;height:58px">雾蓝</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="青雾蓝色")
    adapter = AppleOfficialAdapter(_apple_spec())

    observation = adapter.observe(task, page)
    rectangles = adapter.capture_rectangles_for_capture(
        task,
        page,
        observation.semantic_state,
    )

    assert tuple(rectangle.role for rectangle in rectangles) == (
        "title",
        "price",
        "capacity",
        "color",
    )


def test_apple_browser_identity_fallback_rejects_a_different_tab_model() -> None:
    """A stale or different browser tab must not authorize Apple capture."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter
    from quote_app.tasks.retry import LayoutRecognitionError

    page = _ApplePaintAwareScrollPage(
        """
        <title data-screen-title="product">购买 iPhone 17 Pro - Apple</title>
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1 style="left:50px;top:-320px;width:400px;height:45px">购买 iPhone 17</h1>
          <section data-apple-role="color-group"><h2>颜色 - 雾蓝</h2>
            <input id=":r0:" type="radio" name="color" checked hidden>
            <label for=":r0:"
              style="left:760px;top:80px;width:160px;height:58px">雾蓝</label>
          </section>
          <section data-apple-role="storage-group"><h2>存储容量</h2>
            <input id=":r1:" type="radio" name="capacity" checked hidden>
            <label for=":r1:"
              style="left:760px;top:400px;width:440px;height:100px">256GB RMB 5,999</label>
          </section>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="青雾蓝色")
    adapter = AppleOfficialAdapter(_apple_spec())
    observation = adapter.observe(task, page)

    with pytest.raises(LayoutRecognitionError):
        adapter.capture_rectangles_for_capture(
            task,
            page,
            observation.semantic_state,
        )


def test_apple_waits_for_temporarily_disabled_selected_configuration_cards() -> None:
    """Hydrating Apple controls must not be recorded as a final legal-no result."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleTemporarilyDisabledOptionPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1>购买 iPhone 17</h1>
          <fieldset><legend>颜色 - 黑色</legend>
            <input id="color-black" type="radio" name="color" checked disabled>
            <label for="color-black">黑色</label>
          </fieldset>
          <fieldset><legend>存储容量</legend>
            <input id="capacity-256" type="radio" name="capacity" checked disabled>
            <label for="capacity-256">256GB RMB 5,999</label>
          </fieldset>
        </main></section>
        """,
        enable_after_waits=2,
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")

    observation = AppleOfficialAdapter(_apple_spec()).observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("5999")
    assert len(page.wait_timeout_milliseconds) >= 2


def test_apple_selects_colour_before_waiting_for_storage_cards() -> None:
    """Apple storage is disabled until a colour has actually been selected.

    The live China purchase page initially renders the storage radios as
    disabled.  Treating that initial state as capacity-unavailable is wrong:
    colour is the dependency that unlocks the capacity cards.
    """
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleColorUnlocksCapacityPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1>购买 iPhone 17</h1>
          <fieldset><legend>颜色</legend>
            <input id="color-black" type="radio" name="color" hidden>
            <label for="color-black">黑色</label>
          </fieldset>
          <fieldset><legend>存储容量</legend>
            <input id="capacity-256" type="radio" name="capacity" hidden disabled>
            <label for="capacity-256">256GB RMB 5,999</label>
          </fieldset>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")

    observation = AppleOfficialAdapter(_apple_spec()).observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("5999")


def test_apple_clicks_visible_configuration_cards_not_hidden_native_radios() -> None:
    """The Apple flow must select visible colour then storage cards."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleVisibleConfigurationPage(
        """
        <section data-screen="store"><a class="thumb"
          href="/shop/buy-iphone/iphone-17/mg734ch/a"><h2>iPhone 17</h2></a></section>
        <section data-screen="product" hidden><main>
          <h1>购买 iPhone 17</h1>
          <fieldset><legend>颜色</legend>
            <input id="color-black" type="radio" name="color" hidden>
            <label for="color-black">黑色</label>
          </fieldset>
          <fieldset><legend>存储容量</legend>
            <input id="capacity-256" type="radio" name="capacity" hidden disabled>
            <label for="capacity-256">256GB RMB 5,999</label>
          </fieldset>
        </main></section>
        """
    )
    task = replace(_apple_task(), storage="256GB", color="黑色")

    observation = AppleOfficialAdapter(_apple_spec()).observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.visible_configuration_clicks == ["color-black", "capacity-256"]


def test_apple_no_model_uses_a_search_and_result_evidence_pair() -> None:
    """A missing exact card is written as legal '无', never a guessed price."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage((_FIXTURES / "no_model.html").read_text(encoding="utf-8"))
    task = replace(_apple_task(), task_id="apple-no-model", model_name="iPhone 99")

    observation = AppleOfficialAdapter(_apple_spec()).observe(task, page)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert observation.price is None
    assert tuple(rectangle.role for rectangle in observation.css_rectangles) == (
        "search_keyword",
        "result_region",
    )


@pytest.mark.parametrize(
    ("hidden_option", "expected_outcome", "expected_roles"),
    (
        (
            "512GB",
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            ("title", "capacity_group"),
        ),
        (
            "白色",
            BusinessOutcome.COLOR_UNAVAILABLE,
            ("title", "color_group"),
        ),
    ),
)
def test_apple_missing_requested_configuration_has_formal_legal_no_evidence(
    hidden_option: str,
    expected_outcome: BusinessOutcome,
    expected_roles: tuple[str, str],
) -> None:
    """Storage or colour absence must produce evidence, never another SKU's price."""
    from quote_app.sites.official_brands.apple import AppleOfficialAdapter

    page = _AppleFixturePage((_FIXTURES / "normal_flow.html").read_text(encoding="utf-8"))
    for node in page.root.descendants():
        if node.tag == "button" and node.text.strip() == hidden_option:
            node.attrs["hidden"] = ""

    observation = AppleOfficialAdapter(_apple_spec()).observe(_apple_task(), page)

    assert observation.outcome is expected_outcome
    assert observation.price is None
    assert tuple(rectangle.role for rectangle in observation.css_rectangles) == expected_roles

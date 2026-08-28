from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from quote_app.evidence.chromium import (
    CdpWebAreaSample,
    ChromiumWindowMode,
    ChromiumWindowSample,
)
from quote_app.evidence.geometry import CssRect, DisplayBounds
from quote_app.evidence.macos import (
    MacOSCaptureEnvironment,
    MacOSEvidenceCapture,
)
from quote_app.evidence.macos_binding import (
    BoundMacWindow,
    MacWindowBinder,
    make_system_ui_proof,
)
from quote_app.evidence.macos_native import (
    MacPermissionState,
    NativeSystemUISample,
    NativeWebAreaSample,
    NativeWindowSample,
)
from quote_app.evidence.macos_runtime import MacFormalCaptureRuntime
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureContext,
    CaptureRequest,
    EvidenceCaptureError,
    NonRetryableEvidenceCaptureError,
    RetryableEvidenceCaptureError,
    make_macos_full_display_geometry_snapshot,
    make_capture_error,
)
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.evidence.models import EvidenceState, MacCapturePolicy
from quote_app.sites.catalog import SiteSpec, load_site_catalog
from quote_app.sites.detail_capture_view import CaptureViewGeometryError
from quote_app.sites.official import OfficialSiteAdapter
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from quote_app.tasks.retry import (
    LayoutRecognitionError,
    SecurityVerificationRequired,
)

_NOW = 100.0
_DISPLAY = DisplayBounds(0, 0, 3024, 1964)
_IDENTITY = BrowserWindowIdentity("macos", 5172, "901")


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: MacOSEvidenceCapture(
            policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        ),
        lambda: MacFormalCaptureRuntime(
            policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        ),
    ],
)
def test_visual_review_beta_is_rejected_outside_darwin(
    monkeypatch: pytest.MonkeyPatch,
    constructor: Callable[[], object],
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")

    with pytest.raises(ValueError, match="only on Darwin"):
        constructor()


def test_darwin_beta_manual_layout_skips_startup_window_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    calls: list[str] = []
    runtime, _, bridge = _runtime(
        calls=calls,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert runtime.browser_launch_args() == (
        "--window-position=24,49",
        "--window-size=1464,893",
    )
    assert runtime.browser_startup_preflight() is None
    context = runtime.capture_context_provider(_task(), object(), _state())

    assert context.expected_window == _IDENTITY
    assert bridge.recoveries == []


def test_darwin_beta_manual_layout_preserves_current_chromium_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    runtime = MacFormalCaptureRuntime(
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert runtime._sampler._window_mode is ChromiumWindowMode.PRESERVE


def test_darwin_beta_binds_retina_window_in_logical_screen_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    calls: list[str] = []
    logical_native = replace(
        _fixed_native(sample_id="native-logical"),
        bounds_px=DisplayBounds(24, 49, 1464, 893),
    )
    runtime = MacFormalCaptureRuntime(
        bridge=_Bridge(calls, native_window_samples=(logical_native,)),
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        monotonic_clock=lambda: _NOW,
    )

    bound = runtime._bind(_fixed_chromium(sample_id="chromium-retina"))

    assert bound.native is logical_native
    assert bound.chromium.device_pixel_ratio == 2.0


def test_full_display_beta_geometry_does_not_require_css_window_bounds_to_match(
) -> None:
    """Whole-display review cannot use browser CSS bounds for annotations."""

    bound = BoundMacWindow(
        identity=_IDENTITY,
        chromium=replace(
            _chromium(sample_id="chromium-after-repaint"),
            bounds_dip=DisplayBounds(0, 25, 1508, 937),
        ),
        native=_native(sample_id="native-current"),
    )

    snapshot = make_macos_full_display_geometry_snapshot(
        bound,
        _DISPLAY,
        now_monotonic=_NOW,
    )

    assert snapshot.expected_window == _IDENTITY
    assert snapshot.browser_physical_bounds == bound.native.bounds_px


def test_darwin_beta_evidence_capture_exposes_its_selected_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    capture = MacFormalCaptureRuntime(
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    ).evidence_capture()

    assert capture.policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA


def _task(**changes: object) -> WebsiteTask:
    task = WebsiteTask(
        task_id="task-1",
        run_id="run-1",
        source_row_number=7,
        output_row_number=8,
        material_code="MAT-1",
        brand="HONOR",
        model_name="HONOR 400",
        ram="12GB",
        storage="256GB",
        color="幻夜黑",
        channel=WebsiteChannel.OFFICIAL,
    )
    return replace(task, **changes)


def _state(**changes: object) -> VerifiedSemanticState:
    state = VerifiedSemanticState(
        canonical_url="https://www.honor.com/cn/shop/product/12345.html",
        brand="HONOR",
        model_name="HONOR 400",
        capacity="12GB+256GB",
        color="幻夜黑",
        current_sku="1002",
        region="福建省 福州市 台江区",
        stock_state="现货",
        price=Decimal("3999"),
        outcome=BusinessOutcome.PRICE_FOUND,
        css_rectangles=(),
    )
    return replace(state, **changes)


def _chromium(
    *,
    sample_id: str,
    browser_pid: int = 5172,
) -> ChromiumWindowSample:
    return ChromiumWindowSample(
        cdp_window_id=731,
        browser_pid=browser_pid,
        bounds_dip=DisplayBounds(0, 25, 1512, 941),
        viewport_width_css=1472.0,
        viewport_height_css=870.0,
        device_pixel_ratio=2.0,
        maximized=False,
        fullscreen=False,
        sample_id=sample_id,
        sampled_at_monotonic=99.5,
    )


def _fixed_chromium(*, sample_id: str) -> ChromiumWindowSample:
    return replace(
        _chromium(sample_id=sample_id),
        bounds_dip=DisplayBounds(24, 49, 1464, 893),
        viewport_width_css=1424.0,
        viewport_height_css=822.0,
    )


def _cdp_web_area(**changes: object) -> CdpWebAreaSample:
    sample = CdpWebAreaSample(
        browser_pid=5172,
        viewport_width_css=1472.0,
        viewport_height_css=870.0,
        device_pixel_ratio=2.0,
        client_origin_x_dip=0.0,
        client_origin_y_dip=25.0,
        outer_width_css=1512.0,
        outer_height_css=941.0,
        layout_page_x_css=0.0,
        layout_page_y_css=0.0,
        layout_viewport_width_css=1472.0,
        layout_viewport_height_css=870.0,
        sample_id="cdp-web-area",
        sampled_at_monotonic=99.6,
    )
    return replace(sample, **changes)


def _native(
    *,
    sample_id: str,
    process_id: int = 5172,
    window_id: int = 901,
) -> NativeWindowSample:
    return NativeWindowSample(
        process_id=process_id,
        window_id=window_id,
        bounds_px=DisplayBounds(0, 50, 3024, 1882),
        layer=0,
        on_screen=True,
        minimized=False,
        sample_id=sample_id,
        sampled_at_monotonic=99.5,
    )


def _fixed_native(*, sample_id: str) -> NativeWindowSample:
    return replace(
        _native(sample_id=sample_id),
        bounds_px=DisplayBounds(48, 98, 2928, 1786),
    )


def _capture_changed_native(
    change: str,
) -> NativeWindowSample:
    fixed = _fixed_native(sample_id=f"capture-{change}")
    if change == "one-pixel-drift":
        return replace(
            fixed,
            bounds_px=DisplayBounds(49, 98, 2928, 1786),
        )
    if change == "system-ui-overlap":
        return replace(
            fixed,
            bounds_px=DisplayBounds(48, 49, 2928, 1786),
        )
    if change == "dock-overlap":
        return replace(
            fixed,
            bounds_px=DisplayBounds(48, 98, 2928, 1835),
        )
    if change == "minimized":
        return replace(fixed, minimized=True)
    if change == "layer-changed":
        return replace(fixed, layer=1)
    if change == "identity-changed":
        return replace(fixed, process_id=6000)
    raise AssertionError(f"unsupported capture change: {change}")


def _darwin_beta_capture_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    MacFormalCaptureRuntime,
    CaptureContext,
    _Bridge,
    list[str],
]:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    calls: list[str] = []
    visual_system_ui = replace(
        _system_ui(),
        date_time_bounds_px=None,
        visual_review_required=True,
    )
    bridge = _Bridge(
        calls,
        system_ui_sample=visual_system_ui,
        native_window_samples=(
            _fixed_native(sample_id="native-before"),
            _fixed_native(sample_id="native-activated"),
        ),
        window_bounds_samples=(_fixed_native(sample_id="window-fixed"),),
    )
    sampler = _Sampler(
        calls,
        samples=(
            _fixed_chromium(sample_id="chromium-before"),
            _fixed_chromium(sample_id="chromium-activated"),
        ),
    )
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=sampler,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )
    return (
        runtime,
        runtime.capture_context_provider(_task(), object(), _state()),
        bridge,
        calls,
    )

def _bound_with_native_bounds(bounds_px: DisplayBounds) -> BoundMacWindow:
    return BoundMacWindow(
        identity=_IDENTITY,
        chromium=_chromium(sample_id="chromium-system-ui-proof"),
        native=replace(
            _native(sample_id="native-system-ui-proof"),
            bounds_px=bounds_px,
        ),
    )


def _web_area() -> NativeWebAreaSample:
    return NativeWebAreaSample(
        window_id=901,
        bounds_dip=DisplayBounds(20, 70, 1472, 870),
        sample_id="web-area",
        sampled_at_monotonic=99.6,
    )


def _system_ui() -> NativeSystemUISample:
    return NativeSystemUISample(
        menu_bar_bounds_px=DisplayBounds(0, 0, 3024, 50),
        date_time_bounds_px=DisplayBounds(2700, 0, 250, 50),
        dock_bounds_px=DisplayBounds(0, 1932, 3024, 32),
        sample_id="system-ui",
        sampled_at_monotonic=99.6,
    )


def _overlapping_system_ui(
    *,
    sample_id: str = "system-ui-overlap",
) -> NativeSystemUISample:
    return replace(
        _system_ui(),
        menu_bar_bounds_px=DisplayBounds(0, 0, 3024, 51),
        sample_id=sample_id,
    )


def _recovery_sampler(calls: list[str]) -> _Sampler:
    return _Sampler(
        calls,
        samples=(
            _chromium(sample_id="chromium-before"),
            _chromium(sample_id="chromium-activated"),
            _chromium(sample_id="chromium-recovered"),
        ),
    )


class _Sampler:
    def __init__(
        self,
        calls: list[str],
        samples: tuple[ChromiumWindowSample, ...] | None = None,
        *,
        cdp_web_area_samples: tuple[CdpWebAreaSample, ...] | None = None,
        error_on_call: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls = calls
        self.samples = list(
            samples
            or (
                _chromium(sample_id="chromium-before"),
                _chromium(sample_id="chromium-after"),
                _chromium(sample_id="chromium-safe-window"),
            )
        )
        self.error_on_call = error_on_call
        self.error = error or ValueError("CDP unavailable")
        self.call_count = 0
        self.cdp_web_area_samples = list(
            cdp_web_area_samples
            or tuple(
                replace(
                    _cdp_web_area(),
                    sample_id=f"cdp-web-area-{index}",
                )
                for index in range(3)
            )
        )
        self.cdp_web_area_call_count = 0

    def prepare_and_sample(self, page: Any) -> ChromiumWindowSample:
        del page
        self.call_count += 1
        self.calls.append(f"sample:{self.call_count}")
        if self.call_count == self.error_on_call:
            raise self.error
        return self.samples.pop(0)

    def sample_web_area(self, page: Any) -> CdpWebAreaSample:
        del page
        self.cdp_web_area_call_count += 1
        self.calls.append(
            f"cdp-web-area:{self.cdp_web_area_call_count}"
        )
        return self.cdp_web_area_samples.pop(0)


class _Binder:
    def __init__(
        self,
        calls: list[str],
        *,
        error_on_call: int | None = None,
        second_identity: BrowserWindowIdentity | None = None,
        third_identity: BrowserWindowIdentity | None = None,
    ) -> None:
        self.calls = calls
        self.error_on_call = error_on_call
        self.second_identity = second_identity
        self.third_identity = third_identity
        self.call_count = 0

    def bind(
        self,
        chromium: ChromiumWindowSample,
        native_windows: tuple[NativeWindowSample, ...],
        *,
        now_monotonic: float | None = None,
    ) -> BoundMacWindow:
        self.call_count += 1
        self.calls.append(f"bind:{self.call_count}")
        if self.call_count == self.error_on_call:
            raise ValueError("binding failed")
        bound = MacWindowBinder().bind(
            chromium,
            native_windows,
            now_monotonic=now_monotonic,
        )
        replacement_identity = {
            2: self.second_identity,
            3: self.third_identity,
        }.get(self.call_count)
        if replacement_identity is not None:
            return BoundMacWindow(
                identity=replacement_identity,
                chromium=replace(
                    chromium,
                    browser_pid=replacement_identity.process_id,
                ),
                native=replace(
                    native_windows[0],
                    process_id=replacement_identity.process_id,
                    window_id=int(replacement_identity.window_handle),
                ),
            )
        return bound


class _Bridge:
    def __init__(
        self,
        calls: list[str],
        *,
        permissions: object = MacPermissionState(True, True),
        permission_error: BaseException | None = None,
        activate_error: BaseException | None = None,
        activation_errors: tuple[BaseException | None, ...] | None = None,
        web_area_error: BaseException | None = None,
        display_error: BaseException | None = None,
        system_ui_error: BaseException | None = None,
        system_ui_sample: NativeSystemUISample | None = None,
        system_ui_samples: tuple[NativeSystemUISample, ...] | None = None,
        recover_error: BaseException | None = None,
        native_window_samples: tuple[NativeWindowSample, ...] | None = None,
        native_window_sequences: tuple[
            tuple[NativeWindowSample, ...], ...
        ] | None = None,
        window_bounds_samples: tuple[NativeWindowSample, ...] | None = None,
        window_bounds_error: BaseException | None = None,
    ) -> None:
        self.calls = calls
        self.permission_result = permissions
        self.permission_error = permission_error
        self.activate_error = activate_error
        self.activation_errors = list(activation_errors or ())
        self.web_area_error = web_area_error
        self.display_error = display_error
        self.system_ui_error = system_ui_error
        self.system_ui_sample = system_ui_sample
        self.system_ui_samples = list(system_ui_samples or ())
        self.recover_error = recover_error
        self.native_window_samples = list(native_window_samples or ())
        self.native_window_sequences = list(native_window_sequences or ())
        self.window_bounds_samples = list(window_bounds_samples or ())
        self.window_bounds_error = window_bounds_error
        self.system_ui_policies: list[MacCapturePolicy] = []
        self.window_calls = 0
        self.web_area_calls = 0
        self.display_calls = 0
        self.focused_calls = 0
        self.activated: list[BrowserWindowIdentity] = []
        self.recoveries: list[
            tuple[
                BrowserWindowIdentity,
                DisplayBounds,
                DisplayBounds,
                DisplayBounds,
            ]
        ] = []
        self.focused = _native(sample_id="focused")

    def permissions(self) -> object:
        self.calls.append("permissions")
        if self.permission_error is not None:
            raise self.permission_error
        return self.permission_result

    def windows(self) -> tuple[NativeWindowSample, ...]:
        self.window_calls += 1
        self.calls.append(f"windows:{self.window_calls}")
        if self.native_window_sequences:
            return self.native_window_sequences.pop(0)
        if self.native_window_samples:
            return (self.native_window_samples.pop(0),)
        return (_native(sample_id=f"native-{self.window_calls}"),)

    def activate_window(self, identity: BrowserWindowIdentity) -> None:
        self.calls.append("activate")
        self.activated.append(identity)
        if self.activation_errors:
            error = self.activation_errors.pop(0)
            if error is not None:
                raise error
        if self.activate_error is not None:
            raise self.activate_error

    def web_area(
        self,
        identity: BrowserWindowIdentity,
    ) -> NativeWebAreaSample:
        assert identity == _IDENTITY
        self.web_area_calls += 1
        self.calls.append("geometry")
        if self.web_area_error is not None:
            raise self.web_area_error
        return _web_area()

    def primary_display_bounds(self) -> DisplayBounds:
        self.display_calls += 1
        if self.display_error is not None:
            raise self.display_error
        return _DISPLAY

    def system_ui(
        self,
        identity: BrowserWindowIdentity,
        *,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> NativeSystemUISample:
        assert identity == _IDENTITY
        self.calls.append("system-ui")
        self.system_ui_policies.append(policy)
        if self.system_ui_error is not None:
            raise self.system_ui_error
        if self.system_ui_samples:
            return self.system_ui_samples.pop(0)
        return self.system_ui_sample or _system_ui()

    def prepare_safe_capture_window(
        self,
        identity: BrowserWindowIdentity,
        *,
        menu_bar: DisplayBounds,
        dock: DisplayBounds,
        display: DisplayBounds,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> None:
        del policy
        self.calls.append("recover")
        self.recoveries.append((identity, menu_bar, dock, display))
        if self.recover_error is not None:
            raise self.recover_error

    def window_bounds(
        self,
        identity: BrowserWindowIdentity,
    ) -> NativeWindowSample:
        assert identity == _IDENTITY
        self.calls.append("window-bounds")
        if self.window_bounds_error is not None:
            raise self.window_bounds_error
        if self.window_bounds_samples:
            return self.window_bounds_samples.pop(0)
        return replace(
            _native(sample_id="window-bounds"),
            bounds_px=DisplayBounds(0, 51, 3024, 1880),
        )

    def focused_window(self) -> NativeWindowSample:
        self.focused_calls += 1
        return self.focused


class _RawCapturePipeline:
    def __init__(
        self,
        *,
        result: object | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.requests: list[CaptureRequest] = []

    def capture(self, request: CaptureRequest) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class _OrderedRawCapturePipeline(_RawCapturePipeline):
    def __init__(
        self,
        events: list[str],
        *,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(result=object(), error=error)
        self.events = events

    def capture(self, request: CaptureRequest) -> object:
        self.events.append("capture")
        return super().capture(request)


class _ProbeCapturePipeline:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def capture(self, request: CaptureRequest) -> object:
        self.events.append("capture")
        return request.stability_probe.semantic_hash()


ReaderFactory = Callable[
    [WebsiteTask, Any, VerifiedSemanticState],
    Callable[[], VerifiedSemanticState],
]


def _reader_factory(
    calls: list[str],
) -> ReaderFactory:
    def factory(
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        del task, page
        calls.append("reader")
        return lambda: state

    return factory


def _runtime(
    *,
    calls: list[str] | None = None,
    bridge: _Bridge | None = None,
    sampler: _Sampler | None = None,
    binder: _Binder | None = None,
    reader_factories: (
        dict[tuple[str, WebsiteChannel], ReaderFactory] | None
    ) = None,
    environments: list[MacOSCaptureEnvironment] | None = None,
    policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    sleeper: Callable[[float], None] | None = None,
    monotonic_clock: Callable[[], float] | None = None,
) -> tuple[MacFormalCaptureRuntime, list[str], _Bridge]:
    observed = calls if calls is not None else []
    selected_bridge = bridge or _Bridge(observed)
    selected_sampler = sampler or _Sampler(observed)
    selected_binder = binder or _Binder(observed)
    factories = (
        {("HONOR", WebsiteChannel.OFFICIAL): _reader_factory(observed)}
        if reader_factories is None
        else reader_factories
    )

    def environment_factory(**providers: Any) -> MacOSCaptureEnvironment:
        environment = MacOSCaptureEnvironment(**providers)
        if environments is not None:
            environments.append(environment)
        return environment

    return (
        MacFormalCaptureRuntime(
            sampler=selected_sampler,
            bridge=selected_bridge,
            binder=selected_binder,
            reader_factories=factories,
            environment_factory=environment_factory,
            monotonic_clock=monotonic_clock or (lambda: _NOW),
            sleeper=sleeper,
            policy=policy,
        ),
        observed,
        selected_bridge,
    )


def _capture_error_code(call: Callable[[], object]) -> str:
    with pytest.raises(EvidenceCaptureError) as captured:
        call()
    return captured.value.code


def _capture_request(context: CaptureContext) -> CaptureRequest:
    return CaptureRequest(
        destination=Path("/tmp/task-6-lease.png"),
        state=EvidenceState.NORMAL,
        css_rectangles=(),
        expected_roles=(),
        expected_window=context.expected_window,
        stability_probe=context.stability_probe,
        minimum_stability_interval_seconds=0.001,
    )


def _assert_no_current_context(
    runtime: MacFormalCaptureRuntime,
    environments: list[MacOSCaptureEnvironment],
) -> None:
    assert isinstance(runtime.evidence_capture(), MacOSEvidenceCapture)
    assert len(environments) == 1
    assert (
        _capture_error_code(environments[0].screen_capture_permission)
        == "CAPTURE_ENVIRONMENT"
    )


def test_provider_builds_bundle_in_exact_fail_closed_order() -> None:
    sleeps: list[float] = []
    runtime, calls, bridge = _runtime(sleeper=sleeps.append)

    context = runtime.capture_context_provider(_task(), object(), _state())

    assert isinstance(context, CaptureContext)
    assert context.expected_window == _IDENTITY
    assert calls == [
        "permissions",
        "sample:1",
        "windows:1",
        "bind:1",
        "activate",
        "sample:2",
        "windows:2",
        "bind:2",
        "geometry",
        "system-ui",
        "recover",
        "window-bounds",
        "sample:3",
        "windows:3",
        "bind:3",
        "geometry",
        "system-ui",
        "reader",
    ]
    assert bridge.activated == [_IDENTITY]
    assert len(bridge.recoveries) == 1
    assert sleeps == []
    assert runtime.evidence_capture() is runtime.evidence_capture()


def test_mac_beta_retries_transient_foreground_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    sleeps: list[float] = []
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        activation_errors=(
            make_capture_error("CAPTURE_FOREGROUND", "not focused"),
            None,
        ),
        native_window_samples=(
            _fixed_native(sample_id="native-before"),
            _fixed_native(sample_id="native-activated"),
        ),
        window_bounds_samples=(
            _fixed_native(sample_id="window-fixed"),
        ),
        system_ui_sample=replace(
            _system_ui(),
            date_time_bounds_px=None,
            visual_review_required=True,
        ),
    )
    sampler = _Sampler(
        calls,
        samples=(
            _fixed_chromium(sample_id="chromium-before"),
            _fixed_chromium(sample_id="chromium-activated"),
        ),
    )
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=sampler,
        sleeper=sleeps.append,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    context = runtime.capture_context_provider(_task(), object(), _state())

    assert context.expected_window == _IDENTITY
    assert bridge.activated == [_IDENTITY, _IDENTITY]
    assert sleeps == [0.15]
    assert bridge.recoveries == []
    assert "recover" not in calls


def test_transient_foreground_activation_fails_after_three_attempts() -> None:
    sleeps: list[float] = []
    foreground_error = make_capture_error(
        "CAPTURE_FOREGROUND",
        "not focused",
    )
    bridge = _Bridge(
        [],
        activation_errors=(
            foreground_error,
            foreground_error,
            foreground_error,
        ),
    )
    runtime, _, _ = _runtime(bridge=bridge, sleeper=sleeps.append)

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_FOREGROUND"
    assert bridge.activated == [_IDENTITY, _IDENTITY, _IDENTITY]
    assert sleeps == [0.15, 0.15]


def test_non_foreground_activation_error_is_not_retried() -> None:
    sleeps: list[float] = []
    bridge = _Bridge(
        [],
        activation_errors=(
            make_capture_error("CAPTURE_ENVIRONMENT", "native failure"),
        ),
    )
    runtime, _, _ = _runtime(bridge=bridge, sleeper=sleeps.append)

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_ENVIRONMENT"
    assert bridge.activated == [_IDENTITY]
    assert sleeps == []


def test_capture_preparation_retries_transient_foreground_activation() -> None:
    sleeps: list[float] = []
    runtime, _, bridge = _runtime(sleeper=sleeps.append)
    context = runtime.capture_context_provider(_task(), object(), _state())
    bridge.activation_errors = [
        make_capture_error("CAPTURE_FOREGROUND", "not focused"),
        None,
    ]

    runtime._environment.prepare_browser(context.expected_window)

    assert bridge.activated == [_IDENTITY, _IDENTITY, _IDENTITY]
    assert sleeps == [0.15]


def test_runtime_default_sampler_selects_normal_window_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes: list[ChromiumWindowMode] = []
    sampler = _Sampler([])

    def sampler_factory(
        *,
        window_mode: ChromiumWindowMode,
    ) -> _Sampler:
        modes.append(window_mode)
        return sampler

    monkeypatch.setattr(
        "quote_app.evidence.macos_runtime.ChromiumCaptureSampler",
        sampler_factory,
    )

    MacFormalCaptureRuntime(
        bridge=_Bridge([]),
        binder=_Binder([]),
        reader_factories={},
    )

    assert modes == [ChromiumWindowMode.NORMAL]


def test_safe_window_is_prepared_once_even_without_initial_overlap() -> None:
    runtime, calls, bridge = _runtime(
        sampler=_recovery_sampler([]),
    )

    context = runtime.capture_context_provider(_task(), object(), _state())

    assert context.expected_window == _IDENTITY
    assert len(bridge.recoveries) == 1
    assert calls.count("recover") == 1


def test_layout_convergence_first_bounds_stale_second_succeeds_then_resamples(
) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def record_sleep(seconds: float) -> None:
        calls.append(f"sleep:{seconds}")
        sleeps.append(seconds)

    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(), _system_ui()),
        window_bounds_samples=(
            replace(
                _native(sample_id="still-overlapping"),
                bounds_px=DisplayBounds(0, 51, 3024, 1882),
            ),
            replace(
                _native(sample_id="converged"),
                bounds_px=DisplayBounds(0, 51, 3024, 1881),
            ),
        ),
    )
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        sleeper=record_sleep,
    )

    context = runtime.capture_context_provider(_task(), object(), _state())

    assert context.expected_window == _IDENTITY
    assert calls == [
        "permissions",
        "sample:1",
        "windows:1",
        "bind:1",
        "activate",
        "sample:2",
        "windows:2",
        "bind:2",
        "geometry",
        "system-ui",
        "recover",
        "window-bounds",
        "sleep:0.1",
        "window-bounds",
        "sample:3",
        "windows:3",
        "bind:3",
        "geometry",
        "system-ui",
        "reader",
    ]
    assert bridge.recoveries == [
        (
            _IDENTITY,
            DisplayBounds(0, 0, 3024, 51),
            DisplayBounds(0, 1932, 3024, 32),
            _DISPLAY,
        )
    ]
    assert bridge.focused_calls == 1
    assert bridge.web_area_calls == 2
    assert bridge.display_calls == 2
    assert sleeps == [0.1]


def test_layout_convergence_three_overlapping_samples_fail_system_ui() -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(),),
        window_bounds_samples=(
            _native(sample_id="overlap-1"),
            _native(sample_id="overlap-2"),
            _native(sample_id="overlap-3"),
        ),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
        sleeper=sleeps.append,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert calls.count("window-bounds") == 3
    assert sleeps == [0.1, 0.1]
    assert "sample:3" not in calls
    _assert_no_current_context(runtime, environments)


def test_layout_convergence_three_undercovered_samples_fail_system_ui() -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    bridge = _Bridge(
        calls,
        window_bounds_samples=tuple(
            replace(
                _native(sample_id=f"undercovered-{index}"),
                bounds_px=DisplayBounds(0, 50, 2720, 1882),
            )
            for index in range(3)
        ),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
        sleeper=sleeps.append,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert calls.count("window-bounds") == 3
    assert sleeps == [0.1, 0.1]
    assert "sample:3" not in calls
    _assert_no_current_context(runtime, environments)


def test_strict_system_ui_proof_accepts_existing_coverage_frame() -> None:
    proof = make_system_ui_proof(
        _bound_with_native_bounds(DisplayBounds(0, 50, 2722, 1882)),
        _system_ui(),
        _DISPLAY,
        now_monotonic=_NOW,
        policy=MacCapturePolicy.STRICT,
    )

    assert proof.expected_window == _IDENTITY


def test_non_darwin_beta_system_ui_proof_uses_existing_coverage_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")

    proof = make_system_ui_proof(
        _bound_with_native_bounds(DisplayBounds(0, 50, 2722, 1882)),
        _system_ui(),
        _DISPLAY,
        now_monotonic=_NOW,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert proof.expected_window == _IDENTITY


@pytest.mark.parametrize(
    "policy",
    [
        MacCapturePolicy.STRICT,
        MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    ],
)
def test_system_ui_proof_rejects_menu_bar_overlap_in_both_policies(
    policy: MacCapturePolicy,
) -> None:
    with pytest.raises(ValueError, match="overlaps required macOS system UI"):
        make_system_ui_proof(
            _bound_with_native_bounds(DisplayBounds(0, 49, 3024, 1882)),
            _system_ui(),
            _DISPLAY,
            now_monotonic=_NOW,
            policy=policy,
        )


def test_darwin_beta_system_ui_proof_defers_fixed_bounds_to_runtime_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    proof = make_system_ui_proof(
        _bound_with_native_bounds(DisplayBounds(96, 144, 2048, 1280)),
        replace(
            _system_ui(),
            date_time_bounds_px=None,
            visual_review_required=True,
        ),
        _DISPLAY,
        now_monotonic=_NOW,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    assert proof.expected_window == _IDENTITY


def test_strict_capture_keeps_existing_prepare_and_post_capture_call_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, bridge = _runtime(
        calls=calls,
        environments=environments,
    )
    context = runtime.capture_context_provider(_task(), object(), _state())
    environment = environments[0]
    snapshot = environment.geometry_snapshot(context.expected_window)
    calls_before_prepare = list(calls)

    environment.prepare_browser(context.expected_window)

    assert calls == calls_before_prepare + ["activate"]
    monkeypatch.setattr(
        "quote_app.evidence.macos._macos_main_display_physical_size",
        lambda: (_DISPLAY.width, _DISPLAY.height),
    )
    monkeypatch.setattr(
        "quote_app.evidence.macos.capture_macos_primary_display",
        lambda destination, **_kwargs: Image.new(
            "RGB",
            (_DISPLAY.width, _DISPLAY.height),
        ).save(destination),
    )
    window_bounds_before_capture = calls.count("window-bounds")
    focused_calls_before_capture = bridge.focused_calls

    environment.capture_primary_display(
        tmp_path / "strict-capture.png",
        snapshot,
    )

    assert calls.count("window-bounds") == window_bounds_before_capture
    assert bridge.focused_calls == focused_calls_before_capture


def test_darwin_beta_capture_uses_current_display_size_when_snapshot_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = make_macos_full_display_geometry_snapshot(
        BoundMacWindow(
            identity=_IDENTITY,
            chromium=_fixed_chromium(sample_id="current-display-chromium"),
            native=_fixed_native(sample_id="current-display-native"),
        ),
        _DISPLAY,
        now_monotonic=_NOW,
    )
    environment = MacOSCaptureEnvironment(
        validate_capture_scale_dpr=False,
    )
    current_size = (2940, 1912)
    observed: list[tuple[tuple[int, int], bool]] = []
    monkeypatch.setattr(
        "quote_app.evidence.macos._macos_main_display_physical_size",
        lambda: current_size,
    )

    def capture_display(
        _destination: Path,
        *,
        expected_physical_size: tuple[int, int],
        validate_scale_dpr: bool,
        **_kwargs: object,
    ) -> None:
        observed.append((expected_physical_size, validate_scale_dpr))

    monkeypatch.setattr(
        "quote_app.evidence.macos.capture_macos_primary_display",
        capture_display,
    )

    environment.capture_primary_display(
        tmp_path / "current-main-display.png",
        snapshot,
    )

    assert observed == [(current_size, False)]


def test_non_darwin_beta_keeps_existing_prepare_call_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, context, _bridge, calls = _darwin_beta_capture_context(
        monkeypatch
    )
    monkeypatch.setattr("platform.system", lambda: "Windows")
    calls_before_prepare = list(calls)

    runtime._environment.prepare_browser(context.expected_window)

    assert calls == calls_before_prepare + ["activate"]


@pytest.mark.parametrize(
    "change",
    [
        "minimized",
        "layer-changed",
        "identity-changed",
    ],
)
def test_darwin_beta_capture_revalidates_native_window_before_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    runtime, context, bridge, calls = _darwin_beta_capture_context(
        monkeypatch
    )
    bridge.window_bounds_samples = [_capture_changed_native(change)]
    monkeypatch.setattr(
        "quote_app.evidence.macos._macos_main_display_physical_size",
        lambda: (_DISPLAY.width, _DISPLAY.height),
    )
    monkeypatch.setattr(
        "quote_app.evidence.macos.capture_macos_primary_display",
        lambda *_args, **_kwargs: pytest.fail("must fail before screenshot"),
    )

    assert _capture_error_code(
        lambda: runtime.evidence_capture().capture(
            _capture_request(context)
        )
    ) in {"CAPTURE_SYSTEM_UI", "CAPTURE_GEOMETRY"}
    assert calls.count("window-bounds") == 1


@pytest.mark.parametrize("change", ["system-ui-overlap", "dock-overlap"])
def test_darwin_beta_full_display_does_not_reject_system_ui_geometry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    runtime, context, bridge, calls = _darwin_beta_capture_context(
        monkeypatch
    )
    bridge.window_bounds_samples = [_capture_changed_native(change)]
    monkeypatch.setattr(
        "quote_app.evidence.macos._macos_main_display_physical_size",
        lambda: (_DISPLAY.width, _DISPLAY.height),
    )

    def capture_display(destination: Path, **_kwargs: object) -> None:
        Image.new("RGB", (_DISPLAY.width, _DISPLAY.height)).save(destination)

    monkeypatch.setattr(
        "quote_app.evidence.macos.capture_macos_primary_display",
        capture_display,
    )
    monkeypatch.setattr(
        "quote_app.evidence.platform.assess_capture_quality",
        lambda *_args, **_kwargs: None,
    )

    evidence = runtime.evidence_capture().capture(
        _capture_request(context)
    )

    assert evidence.validation_code == "CAPTURE_OK_MAC_VISUAL_REVIEW"
    assert calls.count("window-bounds") == 1


@pytest.mark.parametrize(
    "change",
    [
        "system-ui-overlap",
        "dock-overlap",
        "minimized",
        "layer-changed",
        "identity-changed",
    ],
)
def test_darwin_beta_capture_keeps_pre_capture_validation_after_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    runtime, context, bridge, calls = _darwin_beta_capture_context(
        monkeypatch
    )
    bridge.window_bounds_samples = [_fixed_native(sample_id="before-capture")]
    monkeypatch.setattr(
        "quote_app.evidence.macos._macos_main_display_physical_size",
        lambda: (_DISPLAY.width, _DISPLAY.height),
    )

    def capture_after_change(destination: Path, **_kwargs: object) -> None:
        Image.new("RGB", (_DISPLAY.width, _DISPLAY.height)).save(destination)
        bridge.window_bounds_samples = [_capture_changed_native(change)]

    monkeypatch.setattr(
        "quote_app.evidence.macos.capture_macos_primary_display",
        capture_after_change,
    )
    monkeypatch.setattr(
        "quote_app.evidence.platform.assess_capture_quality",
        lambda *_args, **_kwargs: None,
    )

    evidence = runtime.evidence_capture().capture(
        _capture_request(context)
    )

    assert evidence.state is EvidenceState.NORMAL
    assert calls.count("window-bounds") == 1



@pytest.mark.parametrize(
    "bridge_changes",
    [
        {
            "window_bounds_samples": (
                _native(sample_id="other-pid", process_id=6000),
            ),
        },
        {"window_bounds_error": RuntimeError("CG query failed")},
    ],
)
def test_layout_convergence_identity_or_api_failure_is_system_ui(
    bridge_changes: dict[str, object],
) -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(),),
        **bridge_changes,
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
        sleeper=lambda _seconds: pytest.fail("must fail without sleeping"),
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert calls.count("window-bounds") == 1
    assert "sample:3" not in calls
    _assert_no_current_context(runtime, environments)


def test_layout_convergence_timeout_is_system_ui_within_200ms() -> None:
    calls: list[str] = []
    now = [_NOW]
    sleeps: list[float] = []

    def delayed_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += 0.201

    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(),),
        window_bounds_samples=(
            _native(sample_id="still-overlapping"),
        ),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
        sleeper=delayed_sleep,
        monotonic_clock=lambda: now[0],
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert calls.count("window-bounds") == 1
    assert sleeps == [0.1]
    assert "sample:3" not in calls
    _assert_no_current_context(runtime, environments)


def test_initial_dock_overlap_also_triggers_only_one_recovery() -> None:
    calls: list[str] = []
    overlapping_dock = DisplayBounds(200, 1931, 2624, 33)
    bridge = _Bridge(
        calls,
        system_ui_samples=(
            replace(
                _system_ui(),
                dock_bounds_px=overlapping_dock,
                sample_id="dock-overlap",
            ),
            _system_ui(),
        ),
    )
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
    )

    context = runtime.capture_context_provider(_task(), object(), _state())

    assert context.expected_window == _IDENTITY
    assert calls.count("recover") == 1
    assert bridge.recoveries[0][2] == overlapping_dock


def test_invalid_overlapping_system_ui_sample_never_triggers_recovery() -> None:
    calls: list[str] = []
    invalid_sample = replace(
        _overlapping_system_ui(),
        dock_bounds_px=DisplayBounds(0, 25, 3024, 100),
        sample_id="invalid-overlap",
    )
    bridge = _Bridge(
        calls,
        system_ui_samples=(invalid_sample,),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        environments=environments,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert bridge.recoveries == []
    _assert_no_current_context(runtime, environments)


def test_recovery_api_failure_fails_closed_without_resampling() -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(),),
        recover_error=RuntimeError("AX frame update failed"),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert calls.count("recover") == 1
    assert "sample:3" not in calls
    _assert_no_current_context(runtime, environments)


def test_repeated_overlap_fails_closed_without_a_second_recovery() -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        system_ui_samples=(
            _overlapping_system_ui(sample_id="overlap-before"),
            _overlapping_system_ui(sample_id="overlap-after"),
        ),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_SYSTEM_UI"
    assert calls.count("recover") == 1
    assert calls.count("system-ui") == 2
    assert "reader" not in calls
    _assert_no_current_context(runtime, environments)


def test_recovery_rebind_cannot_switch_browser_window_identity() -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(),),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        binder=_Binder(
            calls,
            third_identity=BrowserWindowIdentity("macos", 6000, "902"),
        ),
        environments=environments,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_WINDOW_IDENTITY"
    assert calls.count("recover") == 1
    assert calls[-1] == "bind:3"
    _assert_no_current_context(runtime, environments)


def test_recovery_rebind_must_still_be_the_foreground_window() -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        system_ui_samples=(_overlapping_system_ui(),),
    )
    bridge.focused = _native(sample_id="other-focused", window_id=902)
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=_recovery_sampler(calls),
        environments=environments,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == "CAPTURE_FOREGROUND"
    assert calls.count("recover") == 1
    assert bridge.focused_calls == 1
    assert bridge.web_area_calls == 1
    assert calls[-1] == "bind:3"
    _assert_no_current_context(runtime, environments)


def test_runtime_forwards_beta_policy_to_visual_system_ui_proof() -> None:
    calls: list[str] = []
    visual_sample = replace(
        _system_ui(),
        date_time_bounds_px=None,
        visual_review_required=True,
    )
    bridge = _Bridge(
        calls,
        system_ui_sample=visual_sample,
        native_window_samples=(
            _fixed_native(sample_id="native-before"),
            _fixed_native(sample_id="native-activated"),
        ),
        window_bounds_samples=(_fixed_native(sample_id="window-fixed"),),
    )
    sampler = _Sampler(
        calls,
        samples=(
            _fixed_chromium(sample_id="chromium-before"),
            _fixed_chromium(sample_id="chromium-activated"),
        ),
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, selected_bridge = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=sampler,
        environments=environments,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )
    context = runtime.capture_context_provider(_task(), object(), _state())
    proof = environments[0].system_ui_proof(
        environments[0].geometry_snapshot(context.expected_window)
    )

    assert all(
        policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
        for policy in selected_bridge.system_ui_policies
    )
    assert proof.date_time_visible is False
    assert proof.visual_review_required is True


def test_darwin_beta_uses_full_display_snapshot_without_web_area_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    environments: list[MacOSCaptureEnvironment] = []
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    bridge = _Bridge(
        calls,
        web_area_error=AssertionError("beta must not sample AXWebArea"),
        native_window_samples=(
            _fixed_native(sample_id="native-before"),
            _fixed_native(sample_id="native-activated"),
        ),
        window_bounds_samples=(_fixed_native(sample_id="window-fixed"),),
    )
    sampler = _Sampler(
        calls,
        samples=(
            _fixed_chromium(sample_id="chromium-before"),
            _fixed_chromium(sample_id="chromium-activated"),
        ),
    )
    sampler.sample_web_area = lambda _page: pytest.fail(
        "beta must not sample CDP web area"
    )
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        sampler=sampler,
        environments=environments,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )
    context = runtime.capture_context_provider(_task(), object(), _state())
    snapshot = environments[0].geometry_snapshot(context.expected_window)

    assert context.expected_window == _IDENTITY
    assert bridge.web_area_calls == 0
    assert sampler.cdp_web_area_call_count == 0
    assert snapshot.expected_window == _IDENTITY
    assert snapshot.browser_physical_bounds == _fixed_native(
        sample_id="expected-native"
    ).bounds_px
    assert snapshot.display_physical_bounds == _DISPLAY
    assert snapshot.maximized is False
    assert snapshot.fullscreen is False
    assert snapshot.viewport_geometry.scale_x == 2.0
    assert snapshot.viewport_geometry.scale_y == 2.0


@pytest.mark.parametrize("system_name", ["Darwin", "Windows"])
def test_beta_full_display_path_is_not_available_to_strict_or_nonmac_runtime(
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
) -> None:
    calls: list[str] = []
    sampler = _Sampler(calls)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    runtime, _, bridge = _runtime(
        calls=calls,
        sampler=sampler,
        bridge=_Bridge(
            calls,
            web_area_error=AssertionError("coordinate proof was requested"),
        ),
        policy=(
            MacCapturePolicy.STRICT
            if system_name == "Darwin"
            else MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
        ),
    )
    monkeypatch.setattr("platform.system", lambda: system_name)

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(_task(), object(), _state())
    ) == "CAPTURE_GEOMETRY"

    assert bridge.web_area_calls == 1
    assert sampler.cdp_web_area_call_count == 0


def test_strict_runtime_rejects_ax_web_area_timeout_without_cdp_sampling() -> None:
    calls: list[str] = []
    sampler = _Sampler(calls)
    runtime, _, bridge = _runtime(
        calls=calls,
        bridge=_Bridge(
            calls,
            web_area_error=NonRetryableEvidenceCaptureError(
                "CAPTURE_ACCESSIBILITY",
                "WEB_AREA_TIMEOUT",
            ),
        ),
        sampler=sampler,
        policy=MacCapturePolicy.STRICT,
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        runtime.capture_context_provider(_task(), object(), _state())

    assert captured.value.code == "CAPTURE_ACCESSIBILITY"
    assert captured.value.message == "WEB_AREA_TIMEOUT"
    assert bridge.web_area_calls == 1
    assert sampler.cdp_web_area_call_count == 0


def test_evidence_capture_is_stable_before_the_first_context() -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(environments=environments)

    capture = runtime.evidence_capture()

    assert isinstance(capture, MacOSEvidenceCapture)
    assert runtime.evidence_capture() is capture
    assert len(environments) == 1
    assert (
        _capture_error_code(environments[0].screen_capture_permission)
        == "CAPTURE_ENVIRONMENT"
    )


@pytest.mark.parametrize(
    ("permissions", "permission_error", "expected_code"),
    [
        (MacPermissionState(False, True), None, "CAPTURE_PERMISSION"),
        (MacPermissionState(True, False), None, "CAPTURE_ACCESSIBILITY"),
        (object(), None, "CAPTURE_ENVIRONMENT"),
        (MacPermissionState(True, True), RuntimeError("failed"), "CAPTURE_ENVIRONMENT"),
    ],
)
def test_permission_failure_is_stable_and_stops_before_cdp(
    permissions: object,
    permission_error: BaseException | None,
    expected_code: str,
) -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        permissions=permissions,
        permission_error=permission_error,
    )
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        environments=environments,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                object(),
                _state(),
            )
        )
        == expected_code
    )
    assert calls == ["permissions"]
    _assert_no_current_context(runtime, environments)


@pytest.mark.parametrize(
    (
        "sampler_error_call",
        "binder_error_call",
        "expected_code",
        "expected_prefix",
    ),
    [
        (1, None, "CAPTURE_ENVIRONMENT", ["permissions", "sample:1"]),
        (
            None,
            1,
            "CAPTURE_WINDOW_IDENTITY",
            ["permissions", "sample:1", "windows:1", "bind:1"],
        ),
        (
            2,
            None,
            "CAPTURE_ENVIRONMENT",
            [
                "permissions",
                "sample:1",
                "windows:1",
                "bind:1",
                "activate",
                "sample:2",
            ],
        ),
        (
            None,
            2,
            "CAPTURE_WINDOW_IDENTITY",
            [
                "permissions",
                "sample:1",
                "windows:1",
                "bind:1",
                "activate",
                "sample:2",
                "windows:2",
                "bind:2",
            ],
        ),
    ],
)
def test_sampling_or_binding_failure_never_publishes_capture(
    sampler_error_call: int | None,
    binder_error_call: int | None,
    expected_code: str,
    expected_prefix: list[str],
) -> None:
    calls: list[str] = []
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        sampler=_Sampler(calls, error_on_call=sampler_error_call),
        binder=_Binder(calls, error_on_call=binder_error_call),
        environments=environments,
    )

    assert _capture_error_code(
        lambda: runtime.capture_context_provider(
            _task(),
            object(),
            _state(),
        )
    ) == expected_code
    assert calls == expected_prefix
    _assert_no_current_context(runtime, environments)


def test_second_bind_cannot_switch_to_another_chrome_window() -> None:
    calls: list[str] = []
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(
        calls=calls,
        binder=_Binder(
            calls,
            second_identity=BrowserWindowIdentity("macos", 6000, "902"),
        ),
        environments=environments,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                object(),
                _state(),
            )
        )
        == "CAPTURE_WINDOW_IDENTITY"
    )
    assert calls[-1] == "bind:2"
    _assert_no_current_context(runtime, environments)


@pytest.mark.parametrize(
    ("bridge_changes", "expected_code", "last_call"),
    [
        (
            {"activate_error": RuntimeError("activate failed")},
            "CAPTURE_FOREGROUND",
            "activate",
        ),
        (
            {"web_area_error": RuntimeError("AX failed")},
            "CAPTURE_GEOMETRY",
            "geometry",
        ),
        (
            {"display_error": RuntimeError("display failed")},
            "CAPTURE_GEOMETRY",
            "geometry",
        ),
        (
            {"system_ui_error": RuntimeError("UI failed")},
            "CAPTURE_SYSTEM_UI",
            "system-ui",
        ),
    ],
)
def test_native_provider_failure_is_classified_and_never_published(
    bridge_changes: dict[str, BaseException],
    expected_code: str,
    last_call: str,
) -> None:
    calls: list[str] = []
    environments: list[MacOSCaptureEnvironment] = []
    bridge = _Bridge(calls, **bridge_changes)
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        environments=environments,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                object(),
                _state(),
            )
        )
        == expected_code
    )
    assert calls[-1] == last_call
    _assert_no_current_context(runtime, environments)


def test_failed_context_clears_a_previous_capture_bundle() -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, bridge = _runtime(environments=environments)
    runtime.capture_context_provider(_task(), object(), _state())
    previous_capture = runtime.evidence_capture()
    bridge.permission_result = MacPermissionState(False, True)

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                object(),
                _state(),
            )
        )
        == "CAPTURE_PERMISSION"
    )
    _assert_no_current_context(runtime, environments)
    assert runtime.evidence_capture() is previous_capture


def test_capture_provider_failure_clears_current_runtime_state() -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, bridge = _runtime(environments=environments)
    context = runtime.capture_context_provider(_task(), object(), _state())
    environment = environments[0]
    bridge.web_area_error = RuntimeError("AX failed after context")

    assert (
        _capture_error_code(
            lambda: environment.geometry_snapshot(
                context.expected_window
            )
        )
        == "CAPTURE_GEOMETRY"
    )
    assert (
        _capture_error_code(environment.screen_capture_permission)
        == "CAPTURE_ENVIRONMENT"
    )


def test_runtime_preserves_safe_web_area_subcode_for_local_failure_record() -> None:
    calls: list[str] = []
    environments: list[MacOSCaptureEnvironment] = []
    bridge = _Bridge(
        calls,
        web_area_error=NonRetryableEvidenceCaptureError(
            "CAPTURE_ACCESSIBILITY",
            "WEB_AREA_NOT_FOUND",
        ),
    )
    runtime, _, _ = _runtime(
        calls=calls,
        bridge=bridge,
        environments=environments,
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        runtime.capture_context_provider(_task(), object(), _state())

    assert captured.value.code == "CAPTURE_ACCESSIBILITY"
    assert captured.value.message == "WEB_AREA_NOT_FOUND"
    _assert_no_current_context(runtime, environments)


def test_runtime_does_not_preserve_untrusted_accessibility_error_message() -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        web_area_error=NonRetryableEvidenceCaptureError(
            "CAPTURE_ACCESSIBILITY",
            "untrusted native detail",
        ),
    )
    runtime, _, _ = _runtime(calls=calls, bridge=bridge)

    with pytest.raises(EvidenceCaptureError) as captured:
        runtime.capture_context_provider(_task(), object(), _state())

    assert captured.value.code == "CAPTURE_ACCESSIBILITY"
    assert captured.value.message != "untrusted native detail"


def test_same_window_second_context_invalidates_first_probe_lease() -> None:
    calls: list[str] = []
    samples = tuple(
        _chromium(sample_id=f"chromium-{index}")
        for index in range(6)
    )
    runtime, _, _ = _runtime(
        calls=calls,
        sampler=_Sampler(calls, samples=samples),
    )
    first = runtime.capture_context_provider(
        _task(task_id="task-a"),
        object(),
        _state(),
    )
    second = runtime.capture_context_provider(
        _task(task_id="task-b"),
        object(),
        _state(),
    )
    assert first.expected_window == second.expected_window
    assert first.stability_probe is not second.stability_probe
    raw = _RawCapturePipeline(result=object())
    runtime.evidence_capture()._pipeline = raw  # type: ignore[assignment]

    assert (
        _capture_error_code(
            lambda: runtime.evidence_capture().capture(
                _capture_request(first)
            )
        )
        in {"CAPTURE_WINDOW_IDENTITY", "CAPTURE_ENVIRONMENT"}
    )
    assert raw.requests == []
    assert (
        _capture_error_code(
            lambda: runtime.evidence_capture().capture(
                _capture_request(second)
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert raw.requests == []


def test_successful_capture_consumes_probe_lease_once() -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(environments=environments)
    context = runtime.capture_context_provider(_task(), object(), _state())
    result = object()
    raw = _RawCapturePipeline(result=result)
    runtime.evidence_capture()._pipeline = raw  # type: ignore[assignment]
    request = _capture_request(context)

    assert runtime.evidence_capture().capture(request) is result
    assert len(raw.requests) == 1
    assert (
        _capture_error_code(
            lambda: runtime.evidence_capture().capture(request)
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert len(raw.requests) == 1
    _assert_no_current_context(runtime, environments)


@pytest.mark.parametrize(
    "error",
    [
        OSError("primary display failed"),
        RetryableEvidenceCaptureError(
            "CAPTURE_UNREADABLE",
            "quality failed",
        ),
        RetryableEvidenceCaptureError(
            "CAPTURE_FAILED",
            "output failed",
        ),
        KeyboardInterrupt(),
        SystemExit(),
    ],
)
def test_whole_capture_failure_or_interrupt_consumes_lease(
    error: BaseException,
) -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, _ = _runtime(environments=environments)
    context = runtime.capture_context_provider(_task(), object(), _state())
    raw = _RawCapturePipeline(error=error)
    runtime.evidence_capture()._pipeline = raw  # type: ignore[assignment]

    with pytest.raises(type(error)) as captured:
        runtime.evidence_capture().capture(_capture_request(context))

    if isinstance(error, EvidenceCaptureError):
        assert captured.value.code == error.code
        assert captured.value is not error
    else:
        assert captured.value is error
    assert len(raw.requests) == 1
    _assert_no_current_context(runtime, environments)


def test_bound_closures_reject_every_noncurrent_identity_without_rebinding() -> None:
    other = BrowserWindowIdentity("macos", 5172, "902")

    environments: list[MacOSCaptureEnvironment] = []
    runtime, calls, _ = _runtime(environments=environments)
    runtime.capture_context_provider(_task(), object(), _state())
    environment = environments[0]
    calls_before = list(calls)
    assert (
        _capture_error_code(lambda: environment.prepare_browser(other))
        == "CAPTURE_WINDOW_IDENTITY"
    )
    assert calls == calls_before

    other_environments: list[MacOSCaptureEnvironment] = []
    other_runtime, other_calls, _ = _runtime(
        environments=other_environments
    )
    other_runtime.capture_context_provider(_task(), object(), _state())
    other_environment = other_environments[0]
    other_calls_before = list(other_calls)
    assert (
        _capture_error_code(
            lambda: other_environment.geometry_snapshot(other)
        )
        == "CAPTURE_WINDOW_IDENTITY"
    )
    assert other_calls == other_calls_before


def test_system_ui_and_foreground_closures_remain_pinned_to_current_identity() -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, calls, _ = _runtime(environments=environments)
    context = runtime.capture_context_provider(_task(), object(), _state())
    environment = environments[-1]
    current_snapshot = environment.geometry_snapshot(
        context.expected_window
    )
    calls_before = list(calls)
    other = BrowserWindowIdentity("macos", 5172, "902")

    with pytest.raises(EvidenceCaptureError) as captured:
        environment.system_ui_proof(
            replace(current_snapshot, expected_window=other)
        )
    assert captured.value.code == "CAPTURE_WINDOW_IDENTITY"
    assert calls == calls_before

    other_environments: list[MacOSCaptureEnvironment] = []
    other_runtime, other_calls, other_bridge = _runtime(
        environments=other_environments
    )
    other_runtime.capture_context_provider(_task(), object(), _state())
    other_environment = other_environments[0]
    other_calls_before = list(other_calls)
    other_bridge.focused = _native(
        sample_id="other-focused",
        window_id=902,
    )
    assert (
        _capture_error_code(other_environment.foreground_window)
        == "CAPTURE_FOREGROUND"
    )
    assert other_calls == other_calls_before


def test_unsupported_reader_fails_after_providers_with_capture_environment() -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, calls, _ = _runtime(
        reader_factories={},
        environments=environments,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(brand="小米"),
                object(),
                _state(brand="小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert calls[-2:] == ["geometry", "system-ui"]
    _assert_no_current_context(runtime, environments)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit()])
def test_process_interrupts_are_not_swallowed_and_clear_current_bundle(
    error: BaseException,
) -> None:
    environments: list[MacOSCaptureEnvironment] = []
    runtime, _, bridge = _runtime(environments=environments)
    runtime.capture_context_provider(_task(), object(), _state())
    bridge.permission_error = error

    with pytest.raises(type(error)):
        runtime.capture_context_provider(_task(), object(), _state())
    _assert_no_current_context(runtime, environments)


@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        (
            RetryableEvidenceCaptureError(
                "CAPTURE_GEOMETRY",
                "token=geometry-secret",
            ),
            "CAPTURE_GEOMETRY",
        ),
        (
            NonRetryableEvidenceCaptureError(
                "INJECTED_FAILURE",
                "token=invalid-secret",
            ),
            "CAPTURE_ENVIRONMENT",
        ),
    ],
)
def test_runtime_revalidates_injected_capture_errors(
    provider_error: EvidenceCaptureError,
    expected_code: str,
) -> None:
    calls: list[str] = []
    bridge = _Bridge(calls, permission_error=provider_error)
    runtime, _, _ = _runtime(calls=calls, bridge=bridge)

    with pytest.raises(EvidenceCaptureError) as captured:
        runtime.capture_context_provider(_task(), object(), _state())

    assert captured.value.code == expected_code
    assert "secret" not in captured.value.message


def test_runtime_invalid_geometry_capture_code_falls_back_to_environment() -> None:
    calls: list[str] = []
    bridge = _Bridge(
        calls,
        web_area_error=NonRetryableEvidenceCaptureError(
            "INJECTED_FAILURE",
            "token=invalid-geometry-secret",
        ),
    )
    runtime, _, _ = _runtime(calls=calls, bridge=bridge)

    with pytest.raises(EvidenceCaptureError) as captured:
        runtime.capture_context_provider(_task(), object(), _state())

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert "secret" not in captured.value.message


@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        (
            RetryableEvidenceCaptureError(
                "CAPTURE_GEOMETRY",
                "token=geometry-secret",
            ),
            "CAPTURE_GEOMETRY",
        ),
        (
            NonRetryableEvidenceCaptureError(
                "INJECTED_FAILURE",
                "token=invalid-secret",
            ),
            "CAPTURE_ENVIRONMENT",
        ),
    ],
)
def test_macos_environment_revalidates_injected_capture_errors(
    provider_error: EvidenceCaptureError,
    expected_code: str,
) -> None:
    def provider(
        _expected: BrowserWindowIdentity,
    ) -> object:
        raise provider_error

    environment = MacOSCaptureEnvironment(
        geometry_snapshot_provider=provider,  # type: ignore[arg-type]
    )

    with pytest.raises(EvidenceCaptureError) as captured:
        environment.geometry_snapshot(_IDENTITY)

    assert captured.value.code == expected_code
    assert "secret" not in captured.value.message


class _Locator:
    def __init__(
        self,
        *,
        text: str = "",
        attributes: dict[str, str] | None = None,
        children: dict[str, _Locator] | None = None,
        style: dict[str, object] | None = None,
        visible: bool = True,
    ) -> None:
        self.text = text
        self.attributes = attributes or {}
        self.children = children or {}
        self.style = style
        self.visible = visible

    def locator(self, selector: str) -> _Locator:
        if selector not in self.children:
            raise AssertionError(f"unexpected broad selector: {selector}")
        return self.children[selector]

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> _Locator:
        if index != 0:
            raise AssertionError("only one controlled node exists")
        return self

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    def evaluate(self, expression: str) -> dict[str, object]:
        assert "getComputedStyle" in expression
        if self.style is None:
            raise AssertionError("only controlled price nodes may be evaluated")
        return dict(self.style)


class _EmptyLocator(_Locator):
    def count(self) -> int:
        return 0


class _HonorPage(_Locator):
    def __init__(self, *, ad_text: str = "rotating-ad-a") -> None:
        self.ad_text = ad_text
        self.url = "https://www.honor.com/cn/shop/product/12345.html"
        color = _Locator(
            text="幻夜黑",
            attributes={
                "data-attrcode": "10",
                "data-skuid": "1002",
            },
        )
        version = _Locator(
            text="5G全网通 12GB+256GB",
            attributes={
                "data-attrcode": "20",
                "data-skuid": "1002",
            },
        )
        state = _Locator(text="现货")
        prompt = _Locator(
            text="现货",
            children={"span.red": state},
        )
        address = _Locator(
            children={
                "a.product-pulldown-btn": _Locator(
                    text="福建省 福州市 台江区"
                ),
                "div.product-address-prompt": prompt,
            }
        )
        hand = _Locator(
            text="预估到手价 ¥3,999",
            style={
                "color": "rgb(255, 0, 0)",
                "effectiveLineThrough": False,
            },
        )
        old = _Locator(
            text="¥4,099",
            style={
                "color": "rgb(0, 0, 0)",
                "effectiveLineThrough": True,
            },
        )
        price_region = _Locator(
            text="预估到手价 ¥3,999 ¥4,099",
            children={
                "span#pro-price-hand.hand": hand,
                "s#pro-price-old": old,
            },
        )
        super().__init__(
            children={
                "h1#pro-name": _Locator(
                    text="HONOR 400 12GB+256GB 幻夜黑 双卡 全网通版"
                ),
                (
                    '#pro-skus dl.product-choose '
                    'li.selected[data-attrname="颜色"]'
                    "[data-attrcode][data-skuid]"
                ): color,
                (
                    '#pro-skus dl.product-choose '
                    'li.selected[data-attrname="版本"]'
                    "[data-attrcode][data-skuid]"
                ): version,
                "#pro-predict.product-address": address,
                "div.product-price-info": price_region,
                '[data-official-role="login"]': _EmptyLocator(),
                '[data-official-role="risk-control"]': _EmptyLocator(),
            }
        )

    def title(self) -> str:
        return "HONOR 400 | 荣耀商城"

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 100

    @property
    def body_text(self) -> str:
        raise AssertionError("body text must never be read")

    @property
    def cookies(self) -> object:
        raise AssertionError("cookies must never be read")

    @property
    def local_storage(self) -> object:
        raise AssertionError("localStorage must never be read")

    @property
    def account(self) -> object:
        raise AssertionError("account state must never be read")


class _CustomAdapterRegistry:
    def __init__(self, adapter: object) -> None:
        self.adapter = adapter
        self.calls: list[tuple[str, WebsiteChannel]] = []

    def adapter_for(
        self,
        brand: str,
        channel: WebsiteChannel,
    ) -> object:
        self.calls.append((brand, channel))
        return self.adapter


class _VerifiedAdapter:
    def __init__(self, spec: SiteSpec) -> None:
        self.spec = spec
        self.channel = spec.channel
        self.calls: list[
            tuple[WebsiteTask, object, VerifiedSemanticState]
        ] = []

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        self.calls.append((task, page, expected))
        return lambda: expected


class _CapturePreparedAdapter(_VerifiedAdapter):
    def __init__(self, spec: SiteSpec) -> None:
        super().__init__(spec)
        self.prepare_calls: list[
            tuple[WebsiteTask, object, VerifiedSemanticState]
        ] = []
        self.restore_calls: list[
            tuple[WebsiteTask, object, VerifiedSemanticState]
        ] = []

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        self.prepare_calls.append((task, page, expected))

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        self.restore_calls.append((task, page, expected))


class _OrderedCaptureAdapter(_VerifiedAdapter):
    def __init__(self, spec: SiteSpec, events: list[str]) -> None:
        super().__init__(spec)
        self.events = events

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        del task, page, expected
        self.events.append("prepare")

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        del task, page
        self.events.append("reader")
        return lambda: expected

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        del task, page, expected
        self.events.append("restore")


class _FailingCapturePreparationAdapter(_VerifiedAdapter):
    def __init__(self, spec: SiteSpec, error: BaseException) -> None:
        super().__init__(spec)
        self.error = error

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        del task, page, expected
        raise self.error


class _CaptureGeometryAdapter(_CapturePreparedAdapter):
    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        del task, page, expected
        return (
            CssRect(30, 40, 120, 32, "search_keyword"),
            CssRect(30, 120, 720, 340, "result_region"),
        )


class _FailingCaptureGeometryAdapter(_CapturePreparedAdapter):
    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        del task, page, expected
        raise CaptureViewGeometryError(
            "fixture final geometry failed",
            safe_stage="结果区域定位",
        )


class _FailingSemanticCaptureRectangleAdapter(_CapturePreparedAdapter):
    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        del task, page, expected
        raise LayoutRecognitionError(
            "JD exact product appeared before no-model capture"
        )


class _SecurityBlockedCaptureAdapter(_VerifiedAdapter):
    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        del task, page, expected
        raise SecurityVerificationRequired("jd", "京东需要人工完成安全验证")


def _site_spec(
    channel: WebsiteChannel,
    *,
    brand: str = "HONOR",
) -> SiteSpec:
    return next(
        candidate
        for candidate in load_site_catalog()
        if candidate.brand == brand and candidate.channel is channel
    )


def _official_adapter(brand: str) -> OfficialSiteAdapter:
    spec = next(
        candidate
        for candidate in load_site_catalog()
        if (
            candidate.brand == brand
            and candidate.channel is WebsiteChannel.OFFICIAL
        )
    )
    return OfficialSiteAdapter(spec)


def test_honor_reader_uses_only_explicit_custom_adapter_registry() -> None:
    calls: list[str] = []
    registry = _CustomAdapterRegistry(_official_adapter("HONOR"))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )

    context = runtime.capture_context_provider(
        _task(),
        _HonorPage(),
        _state(),
    )

    assert registry.calls == [("HONOR", WebsiteChannel.OFFICIAL)]
    assert isinstance(context.stability_probe.semantic_hash(), str)


@pytest.mark.parametrize(
    "channel",
    (WebsiteChannel.JD, WebsiteChannel.TMALL),
)
@pytest.mark.parametrize("brand", ("HONOR", "小米", "欧珀", "维沃", "华为", "苹果"))
def test_default_marketplace_reader_supports_each_six_brand_channel(
    brand: str,
    channel: WebsiteChannel,
) -> None:
    calls: list[str] = []
    adapter = _VerifiedAdapter(_site_spec(channel, brand=brand))
    registry = _CustomAdapterRegistry(adapter)
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=channel, brand=brand)
    page = object()
    state = _state(
        brand=brand,
        canonical_url=f"https://example.test/{channel.value}/item-1"
    )

    context = runtime.capture_context_provider(task, page, state)

    assert registry.calls == [(brand, channel)]
    assert adapter.calls == [(task, page, state)]
    assert isinstance(context.stability_probe.semantic_hash(), str)


@pytest.mark.parametrize("channel", (WebsiteChannel.JD, WebsiteChannel.TMALL))
def test_capture_context_prepares_marketplace_view_once_before_formal_reader(
    channel: WebsiteChannel,
) -> None:
    calls: list[str] = []
    adapter = _CapturePreparedAdapter(_site_spec(channel))
    registry = _CustomAdapterRegistry(adapter)
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=channel)
    page = object()
    state = _state(canonical_url=f"https://example.test/{channel.value}/item-1")

    context = runtime.capture_context_provider(task, page, state)

    assert adapter.prepare_calls == [(task, page, state)]
    assert adapter.calls == [(task, page, state)]
    assert isinstance(context.stability_probe.semantic_hash(), str)


def test_runtime_restores_prepared_view_once_after_successful_capture() -> None:
    calls: list[str] = []
    adapter = _CapturePreparedAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    page = object()
    state = _state(canonical_url="https://example.test/jd/item-1")
    context = runtime.capture_context_provider(task, page, state)
    runtime.evidence_capture()._pipeline = _RawCapturePipeline(
        result=object()
    )  # type: ignore[assignment]

    runtime.evidence_capture().capture(_capture_request(context))

    assert adapter.restore_calls == [(task, page, state)]


def test_runtime_keeps_prepared_view_until_after_evidence_capture() -> None:
    events: list[str] = []
    adapter = _OrderedCaptureAdapter(
        _site_spec(WebsiteChannel.JD),
        events,
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    context = runtime.capture_context_provider(
        _task(channel=WebsiteChannel.JD),
        object(),
        _state(canonical_url="https://example.test/jd/item-1"),
    )
    runtime.evidence_capture()._pipeline = _OrderedRawCapturePipeline(
        events
    )  # type: ignore[assignment]

    runtime.evidence_capture().capture(_capture_request(context))

    assert events == ["prepare", "reader", "capture", "restore"]


def test_runtime_restores_view_when_capture_fails() -> None:
    calls: list[str] = []
    adapter = _CapturePreparedAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    page = object()
    state = _state(canonical_url="https://example.test/jd/item-1")
    context = runtime.capture_context_provider(task, page, state)
    runtime.evidence_capture()._pipeline = _RawCapturePipeline(
        error=OSError("capture failed")
    )  # type: ignore[assignment]

    with pytest.raises(OSError, match="capture failed"):
        runtime.evidence_capture().capture(_capture_request(context))

    assert adapter.restore_calls == [(task, page, state)]


def test_runtime_final_capture_failure_restores_once_after_capture() -> None:
    events: list[str] = []
    adapter = _OrderedCaptureAdapter(
        _site_spec(WebsiteChannel.JD),
        events,
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    context = runtime.capture_context_provider(
        _task(channel=WebsiteChannel.JD),
        object(),
        _state(canonical_url="https://example.test/jd/item-1"),
    )
    runtime.evidence_capture()._pipeline = _OrderedRawCapturePipeline(
        events,
        error=OSError("capture failed"),
    )  # type: ignore[assignment]

    with pytest.raises(OSError, match="capture failed"):
        runtime.evidence_capture().capture(_capture_request(context))

    assert events == ["prepare", "reader", "capture", "restore"]
    assert events.count("restore") == 1


def test_runtime_restores_view_when_context_build_fails_after_prepare() -> None:
    calls: list[str] = []
    adapter = _FailingCaptureGeometryAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    page = object()
    state = _state(canonical_url="https://example.test/jd/item-1")

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(task, page, state)
        )
        == "CAPTURE_GEOMETRY"
    )
    assert adapter.restore_calls == [(task, page, state)]


@pytest.mark.parametrize(
    ("layout_message", "safe_stage"),
    [
        (
            "capture scale 0.8 did not become visually stable",
            "缩放验证",
        ),
        (
            "JD no-model search input is not visible for capture",
            "搜索框定位",
        ),
        (
            "JD no-model product card name is not visible for capture",
            "结果区域定位",
        ),
    ],
)
def test_capture_preparation_layout_failure_preserves_retryable_safe_stage(
    layout_message: str,
    safe_stage: str,
) -> None:
    adapter = _FailingCapturePreparationAdapter(
        _site_spec(WebsiteChannel.JD),
        CaptureViewGeometryError(
            layout_message,
            safe_stage=safe_stage,
        ),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    state = _state(canonical_url="https://example.test/jd/item-1")
    if "no-model" in layout_message:
        state = replace(
            state,
            current_sku="not-applicable",
            region="not-applicable",
            stock_state="not-applicable",
            price=None,
            outcome=BusinessOutcome.NO_MODEL,
            css_rectangles=(
                CssRect(30, 120, 720, 340, "result_region"),
            ),
        )

    with pytest.raises(RetryableEvidenceCaptureError) as captured:
        runtime.capture_context_provider(
            _task(channel=WebsiteChannel.JD),
            object(),
            state,
        )

    assert captured.value.code == "CAPTURE_GEOMETRY"
    assert safe_stage in captured.value.message


def test_macos_jd_price_capture_keeps_a_verified_offer_when_dom_viewport_geometry_disagrees() -> None:
    """A verified JD offer remains capturable when only the DOM viewport gate fails."""

    adapter = _FailingCapturePreparationAdapter(
        _site_spec(WebsiteChannel.JD),
        CaptureViewGeometryError(
            "JD detail capture requires title, price, capacity and color "
            "in the same viewport",
            safe_stage="结果区域定位",
        ),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    state = _state(canonical_url="https://item.jd.com/100012345678.html")

    context = runtime.capture_context_provider(task, object(), state)

    assert context.css_rectangles is None
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0] == task
    assert adapter.calls[0][2] == state


def test_darwin_beta_jd_no_model_capture_keeps_verified_search_result_when_only_framing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified JD no-model page is still captured when only card framing fails."""

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    adapter = _FailingCapturePreparationAdapter(
        _site_spec(WebsiteChannel.JD),
        CaptureViewGeometryError(
            "JD no-model product card name is not visible for capture",
            safe_stage="结果区域定位",
        ),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )
    task = _task(channel=WebsiteChannel.JD)
    state = replace(
        _state(canonical_url="https://mall.jd.com/search?keyword=missing"),
        current_sku="not-applicable",
        region="not-applicable",
        stock_state="not-applicable",
        price=None,
        outcome=BusinessOutcome.NO_MODEL,
        css_rectangles=(CssRect(30, 120, 720, 340, "result_region"),),
    )

    context = runtime.capture_context_provider(task, object(), state)

    assert context.css_rectangles is None
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0] == task
    assert adapter.calls[0][2] == state


def test_darwin_beta_jd_capture_does_not_reread_unused_dom_rectangles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mac JD uses the already prepared page and captures the full display."""

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    adapter = _FailingCaptureGeometryAdapter(
        _site_spec(WebsiteChannel.JD)
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    context = runtime.capture_context_provider(
        _task(channel=WebsiteChannel.JD),
        object(),
        _state(canonical_url="https://item.jd.com/100012345678.html"),
    )

    assert context.css_rectangles is None


def test_capture_rectangle_layout_failure_preserves_result_region_stage() -> None:
    adapter = _FailingCaptureGeometryAdapter(
        _site_spec(WebsiteChannel.JD)
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    with pytest.raises(RetryableEvidenceCaptureError) as captured:
        runtime.capture_context_provider(
            _task(channel=WebsiteChannel.JD),
            object(),
            _state(canonical_url="https://example.test/jd/item-1"),
        )

    assert captured.value.code == "CAPTURE_GEOMETRY"
    assert "结果区域定位" in captured.value.message


@pytest.mark.parametrize(
    ("layout_message", "safe_stage"),
    [
        ("JD no-model search URL changed before capture", "URL复核"),
        ("JD approved store identity is missing", "店铺复核"),
        (
            "JD exact product appeared before no-model capture",
            "商品结果复核",
        ),
        ("fixture semantic layout failed", "页面语义复核"),
    ],
)
def test_semantic_capture_preparation_failure_remains_precise_environment_error(
    layout_message: str,
    safe_stage: str,
) -> None:
    adapter = _FailingCapturePreparationAdapter(
        _site_spec(WebsiteChannel.JD),
        LayoutRecognitionError(layout_message),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.capture_context_provider(
            _task(channel=WebsiteChannel.JD),
            object(),
            _state(canonical_url="https://example.test/jd/item-1"),
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert safe_stage in captured.value.message
    assert "结果区域定位" not in captured.value.message


def test_semantic_capture_rectangle_failure_remains_product_review_environment_error() -> None:
    adapter = _FailingSemanticCaptureRectangleAdapter(
        _site_spec(WebsiteChannel.JD)
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.capture_context_provider(
            _task(channel=WebsiteChannel.JD),
            object(),
            _state(canonical_url="https://example.test/jd/item-1"),
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"
    assert "商品结果复核" in captured.value.message
    assert "结果区域定位" not in captured.value.message


@pytest.mark.parametrize(
    "error",
    [PermissionError("screen permission denied"), OSError("filesystem failed")],
)
def test_non_layout_capture_preparation_failure_remains_environment_error(
    error: BaseException,
) -> None:
    adapter = _FailingCapturePreparationAdapter(
        _site_spec(WebsiteChannel.JD),
        error,
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    with pytest.raises(NonRetryableEvidenceCaptureError) as captured:
        runtime.capture_context_provider(
            _task(channel=WebsiteChannel.JD),
            object(),
            _state(canonical_url="https://example.test/jd/item-1"),
        )

    assert captured.value.code == "CAPTURE_ENVIRONMENT"


def test_capture_context_uses_final_marketplace_rectangles_after_view_preparation() -> None:
    calls: list[str] = []
    adapter = _CaptureGeometryAdapter(_site_spec(WebsiteChannel.TMALL))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.TMALL)
    state = _state(canonical_url="https://example.test/tmall/search")

    context = runtime.capture_context_provider(task, object(), state)

    assert context.css_rectangles == (
        CssRect(30, 40, 120, 32, "search_keyword"),
        CssRect(30, 120, 720, 340, "result_region"),
    )


def test_capture_context_preserves_jd_security_pause_from_final_view_preparation() -> None:
    """A JD challenge appearing immediately before capture must pause, not fail."""

    calls: list[str] = []
    adapter = _SecurityBlockedCaptureAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    with pytest.raises(SecurityVerificationRequired) as caught:
        runtime.capture_context_provider(
            _task(channel=WebsiteChannel.JD),
            object(),
            _state(canonical_url="https://example.test/jd/item-1"),
        )

    assert caught.value.site == "jd"


def test_honor_reader_rejects_mismatched_injected_adapter_config() -> None:
    calls: list[str] = []
    registry = _CustomAdapterRegistry(_official_adapter("小米"))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                _HonorPage(),
                _state(),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert registry.calls == [("HONOR", WebsiteChannel.OFFICIAL)]


def test_honor_reader_requires_explicit_registry_or_factory() -> None:
    calls: list[str] = []
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                _HonorPage(),
                _state(),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )


def test_default_honor_reader_hashes_only_final_controlled_state() -> None:
    calls: list[str] = []
    page = _HonorPage()
    registry = _CustomAdapterRegistry(_official_adapter("HONOR"))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )
    context = runtime.capture_context_provider(_task(), page, _state())

    first_hash = context.stability_probe.semantic_hash()
    page.ad_text = "rotating-ad-b"
    second_hash = context.stability_probe.semantic_hash()

    assert first_hash == second_hash
    address = page.children["#pro-predict.product-address"]
    address.children["a.product-pulldown-btn"].text = "福建省 厦门市 思明区"
    with pytest.raises(LayoutRecognitionError):
        context.stability_probe.semantic_hash()


def test_default_honor_reader_rejects_state_outside_live_price_slice() -> None:
    calls: list[str] = []
    registry = _CustomAdapterRegistry(_official_adapter("HONOR"))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )
    unsupported = VerifiedSemanticState(
        canonical_url="https://www.honor.com/cn/shop/search.html",
        brand="HONOR",
        model_name="HONOR 400",
        capacity="12GB+256GB",
        color="幻夜黑",
        current_sku="not-applicable",
        region="not-applicable",
        stock_state="not-applicable",
        price=None,
        outcome=BusinessOutcome.NO_MODEL,
        css_rectangles=(
            CssRect(0, 0, 100, 20, "search_keyword"),
            CssRect(0, 30, 300, 200, "result_region"),
        ),
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _task(),
                _HonorPage(),
                unsupported,
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )


_LIVE_OFFICIAL_BRANDS = ("小米", "欧珀", "维沃", "华为", "苹果")


def _live_official_spec(brand: str) -> SiteSpec:
    return next(
        candidate
        for candidate in load_site_catalog()
        if (
            candidate.brand == brand
            and candidate.channel is WebsiteChannel.OFFICIAL
        )
    )


def _live_official_task(brand: str) -> WebsiteTask:
    return _task(
        brand=brand,
        model_name=f"{brand} Test Phone",
        channel=WebsiteChannel.OFFICIAL,
    )


def _live_official_state(brand: str) -> VerifiedSemanticState:
    return _state(
        canonical_url=f"https://example.test/{brand}/product-1",
        brand=brand,
        model_name=f"{brand} Test Phone",
        current_sku="official-detail:product-1",
        region="excluded-from-live-official-stability:region",
        stock_state="excluded-from-live-official-stability:stock",
    )


class _OrderedLiveOfficialAdapter(_VerifiedAdapter):
    def __init__(
        self,
        spec: SiteSpec,
        events: list[str],
        *,
        reader_error: BaseException | None = None,
        state_read_error: BaseException | None = None,
        rectangle_error: BaseException | None = None,
        restore_error: BaseException | None = None,
    ) -> None:
        super().__init__(spec)
        self.events = events
        self.reader_error = reader_error
        self.state_read_error = state_read_error
        self.rectangle_error = rectangle_error
        self.restore_error = restore_error
        self.received: list[
            tuple[WebsiteTask, object, VerifiedSemanticState]
        ] = []

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        self.events.append("prepare")
        self.received.append((task, page, expected))

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> Callable[[], VerifiedSemanticState]:
        self.events.append("reader-builder")
        self.received.append((task, page, expected))
        if self.reader_error is not None:
            raise self.reader_error

        def read() -> VerifiedSemanticState:
            if self.state_read_error is not None:
                raise self.state_read_error
            return expected

        return read

    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        self.events.append("rectangles")
        self.received.append((task, page, expected))
        if self.rectangle_error is not None:
            raise self.rectangle_error
        return (CssRect(10, 20, 300, 200, "result_region"),)

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        self.events.append("restore")
        self.received.append((task, page, expected))
        if self.restore_error is not None:
            raise self.restore_error


class _PrepareFailingLiveOfficialAdapter(_VerifiedAdapter):
    def __init__(self, spec: SiteSpec, events: list[str]) -> None:
        super().__init__(spec)
        self.events = events

    def prepare_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        del task, page, expected
        self.events.append("prepare")
        raise OSError("prepare failed")

    def restore_capture_view(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> None:
        del task, page, expected
        self.events.append("restore")


@pytest.mark.parametrize("brand", _LIVE_OFFICIAL_BRANDS)
def test_live_official_reader_uses_exact_adapter_contract_and_capture_order(
    brand: str,
) -> None:
    events: list[str] = []
    adapter = _OrderedLiveOfficialAdapter(
        _live_official_spec(brand),
        events,
    )
    registry = _CustomAdapterRegistry(adapter)
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )
    task = _live_official_task(brand)
    page = object()
    expected = _live_official_state(brand)

    context = runtime.capture_context_provider(task, page, expected)
    runtime.evidence_capture()._pipeline = _OrderedRawCapturePipeline(
        events
    )  # type: ignore[assignment]
    runtime.evidence_capture().capture(_capture_request(context))

    assert registry.calls == [(brand, WebsiteChannel.OFFICIAL)]
    assert events == [
        "prepare",
        "reader-builder",
        "rectangles",
        "capture",
        "restore",
    ]
    assert context.css_rectangles == (
        CssRect(10, 20, 300, 200, "result_region"),
    )
    assert adapter.received == [
        (task, page, expected),
        (task, page, expected),
        (task, page, expected),
        (task, page, expected),
    ]
    assert all(call[2] is expected for call in adapter.received)


@pytest.mark.parametrize(
    ("adapter", "brand"),
    [
        (_VerifiedAdapter(_live_official_spec("华为")), "小米"),
        (_VerifiedAdapter(_site_spec(WebsiteChannel.JD)), "小米"),
    ],
)
def test_live_official_reader_rejects_mismatched_adapter_spec(
    adapter: _VerifiedAdapter,
    brand: str,
) -> None:
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task(brand),
                object(),
                _live_official_state(brand),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert adapter.calls == []


def test_live_official_reader_rejects_mismatched_adapter_channel() -> None:
    adapter = _VerifiedAdapter(_live_official_spec("小米"))
    adapter.channel = WebsiteChannel.JD
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task("小米"),
                object(),
                _live_official_state("小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert adapter.calls == []


def test_live_official_reader_rejects_non_site_spec() -> None:
    class InvalidSpecAdapter(_VerifiedAdapter):
        def __init__(self) -> None:
            self.spec = object()  # type: ignore[assignment]
            self.channel = WebsiteChannel.OFFICIAL
            self.calls = []

    adapter = InvalidSpecAdapter()
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task("小米"),
                object(),
                _live_official_state("小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert adapter.calls == []


def test_live_official_reader_requires_verified_state_reader() -> None:
    spec = _live_official_spec("小米")

    class MissingReaderAdapter:
        channel = WebsiteChannel.OFFICIAL

        def __init__(self) -> None:
            self.spec = spec

    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(MissingReaderAdapter()),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task("小米"),
                object(),
                _live_official_state("小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )


def test_live_official_capture_hooks_are_optional() -> None:
    adapter = _VerifiedAdapter(_live_official_spec("小米"))
    registry = _CustomAdapterRegistry(adapter)
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=registry,
        monotonic_clock=lambda: _NOW,
    )
    task = _live_official_task("小米")
    page = object()
    expected = _live_official_state("小米")

    context = runtime.capture_context_provider(task, page, expected)

    assert registry.calls == [("小米", WebsiteChannel.OFFICIAL)]
    assert adapter.calls == [(task, page, expected)]
    assert context.css_rectangles is None
    assert isinstance(context.stability_probe.semantic_hash(), str)


def test_live_official_reader_state_change_fails_closed() -> None:
    events: list[str] = []
    adapter = _OrderedLiveOfficialAdapter(
        _live_official_spec("小米"),
        events,
        state_read_error=LayoutRecognitionError(
            "Live official verified semantic state changed"
        ),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    expected = _live_official_state("小米")
    context = runtime.capture_context_provider(
        _live_official_task("小米"),
        object(),
        expected,
    )
    runtime.evidence_capture()._pipeline = _ProbeCapturePipeline(
        events
    )  # type: ignore[assignment]

    with pytest.raises(LayoutRecognitionError, match="state changed"):
        runtime.evidence_capture().capture(_capture_request(context))
    assert events == [
        "prepare",
        "reader-builder",
        "rectangles",
        "capture",
        "restore",
    ]


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        (
            "reader",
            ["prepare", "reader-builder", "restore"],
        ),
        (
            "rectangles",
            ["prepare", "reader-builder", "rectangles", "restore"],
        ),
    ],
)
def test_live_official_context_failure_restores_prepared_view_once(
    failure_stage: str,
    expected_events: list[str],
) -> None:
    events: list[str] = []
    adapter = _OrderedLiveOfficialAdapter(
        _live_official_spec("小米"),
        events,
        reader_error=(
            OSError("reader failed") if failure_stage == "reader" else None
        ),
        rectangle_error=(
            OSError("rectangles failed")
            if failure_stage == "rectangles"
            else None
        ),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task("小米"),
                object(),
                _live_official_state("小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert events == expected_events
    assert events.count("restore") == 1


def test_live_official_prepare_failure_does_not_restore_unprepared_view() -> None:
    events: list[str] = []
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(
            _PrepareFailingLiveOfficialAdapter(
                _live_official_spec("小米"),
                events,
            )
        ),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task("小米"),
                object(),
                _live_official_state("小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert events == ["prepare"]


@pytest.mark.parametrize("capture_fails", (False, True))
def test_live_official_capture_completion_restores_once(
    capture_fails: bool,
) -> None:
    events: list[str] = []
    adapter = _OrderedLiveOfficialAdapter(
        _live_official_spec("小米"),
        events,
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    context = runtime.capture_context_provider(
        _live_official_task("小米"),
        object(),
        _live_official_state("小米"),
    )
    runtime.evidence_capture()._pipeline = _OrderedRawCapturePipeline(
        events,
        error=OSError("capture failed") if capture_fails else None,
    )  # type: ignore[assignment]

    if capture_fails:
        with pytest.raises(OSError, match="capture failed"):
            runtime.evidence_capture().capture(_capture_request(context))
    else:
        runtime.evidence_capture().capture(_capture_request(context))

    assert events.count("restore") == 1
    assert events[-1] == "restore"


def test_live_official_restore_failure_fails_closed_once() -> None:
    events: list[str] = []
    adapter = _OrderedLiveOfficialAdapter(
        _live_official_spec("小米"),
        events,
        restore_error=OSError("restore failed"),
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    context = runtime.capture_context_provider(
        _live_official_task("小米"),
        object(),
        _live_official_state("小米"),
    )
    runtime.evidence_capture()._pipeline = _RawCapturePipeline(
        result=object()
    )  # type: ignore[assignment]

    with pytest.raises(OSError, match="restore failed"):
        runtime.evidence_capture().capture(_capture_request(context))

    assert events.count("restore") == 1


@pytest.mark.parametrize("invalid_reader", (None, object()))
def test_live_official_reader_builder_rejects_non_callable_result(
    invalid_reader: object | None,
) -> None:
    events: list[str] = []

    class InvalidReaderAdapter(_OrderedLiveOfficialAdapter):
        def verified_state_reader(
            self,
            task: WebsiteTask,
            page: object,
            expected: VerifiedSemanticState,
        ) -> Any:
            self.events.append("reader-builder")
            self.received.append((task, page, expected))
            return invalid_reader

    adapter = InvalidReaderAdapter(
        _live_official_spec("小米"),
        events,
    )
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler([]),
        bridge=_Bridge([]),
        binder=_Binder([]),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(
                _live_official_task("小米"),
                object(),
                _live_official_state("小米"),
            )
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert events == ["prepare", "reader-builder", "restore"]
    assert runtime._prepared_adapters == {}

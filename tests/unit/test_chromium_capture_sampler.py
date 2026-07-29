from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import math
from typing import Any

import pytest

from quote_app.evidence.chromium import (
    ChromiumCaptureSampler,
    ChromiumWindowMode,
    ChromiumWindowSample,
)
from quote_app.evidence.geometry import DisplayBounds


def _window_response(*, state: str, bounds: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "windowId": 731,
        "bounds": bounds
        or {
            "left": 40,
            "top": 60,
            "width": 1280,
            "height": 860,
            "windowState": state,
        },
    }


def _layout_metrics() -> dict[str, Any]:
    return {
        "layoutViewport": {
            "pageX": 0,
            "pageY": 0,
            "clientWidth": 1440,
            "clientHeight": 900,
        },
        "visualViewport": {
            "offsetX": 0,
            "offsetY": 0,
            "pageX": 0,
            "pageY": 0,
            "clientWidth": 1440,
            "clientHeight": 900,
            "scale": 1,
            "zoom": 1,
        },
        "contentSize": {"x": 0, "y": 0, "width": 1440, "height": 2200},
        "cssLayoutViewport": {
            "pageX": 0,
            "pageY": 0,
            "clientWidth": 1440,
            "clientHeight": 900,
        },
        "cssVisualViewport": {
            "offsetX": 0,
            "offsetY": 0,
            "pageX": 0,
            "pageY": 0,
            "clientWidth": 1440,
            "clientHeight": 900,
            "scale": 1,
        },
        "cssContentSize": {"x": 0, "y": 0, "width": 1440, "height": 2200},
    }


def _process_info(*, browser_pids: tuple[int, ...] = (5172,)) -> dict[str, Any]:
    candidates = [
        {"type": "browser", "id": pid, "cpuTime": 9.25}
        for pid in browser_pids
    ]
    return {
        "processInfo": [
            {"type": "renderer", "id": 5178, "cpuTime": 2.5},
            *candidates,
            {"type": "gpu", "id": 5180, "cpuTime": 0.75},
        ]
    }


class _FakeCdpSession:
    def __init__(
        self,
        *,
        initial_window: dict[str, Any] | None = None,
        sampled_window: dict[str, Any] | None = None,
        layout_metrics: dict[str, Any] | None = None,
        layout_metrics_sequence: list[dict[str, Any]] | None = None,
        process_info: dict[str, Any] | None = None,
        detach_error: BaseException | None = None,
    ) -> None:
        metrics = layout_metrics_sequence or [
            layout_metrics or _layout_metrics()
        ] * 3
        self._responses: dict[str, list[Any]] = {
            "Browser.getWindowForTarget": [
                initial_window or _window_response(state="normal"),
                sampled_window
                or _window_response(
                    state="maximized",
                    bounds={
                        "left": 0,
                        "top": 25,
                        "width": 1512,
                        "height": 941,
                        "windowState": "maximized",
                    },
                ),
            ],
            "Browser.setWindowBounds": [{}],
            "Page.bringToFront": [{}],
            "Page.getLayoutMetrics": metrics,
            "SystemInfo.getProcessInfo": [process_info or _process_info()],
        }
        self.commands: list[tuple[str, dict[str, Any] | None]] = []
        self._detach_error = detach_error
        self.detach_calls = 0

    def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.commands.append((method, params))
        responses = self._responses.get(method)
        if not responses:
            raise AssertionError(f"unexpected or exhausted CDP command: {method}")
        return deepcopy(responses.pop(0))

    def detach(self) -> None:
        self.detach_calls += 1
        if self._detach_error is not None:
            raise self._detach_error


class _PageCdpSessionRejectsProcessInfo(_FakeCdpSession):
    def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "SystemInfo.getProcessInfo":
            raise RuntimeError(
                "Protocol error (SystemInfo.getProcessInfo): "
                "only supported on the browser target",
            )
        return super().send(method, params)


class _BrowserCdpSessionFailsProcessInfo(_FakeCdpSession):
    def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "SystemInfo.getProcessInfo":
            raise RuntimeError("browser process query failed")
        return super().send(method, params)


class _FakeCdpSessionWithoutSend(_FakeCdpSession):
    send = None


class _FakeBrowser:
    def __init__(
        self,
        session: _FakeCdpSession,
        *,
        session_error: BaseException | None = None,
    ) -> None:
        self._session = session
        self._session_error = session_error
        self.session_requests = 0

    def new_browser_cdp_session(self) -> _FakeCdpSession:
        self.session_requests += 1
        if self._session_error is not None:
            raise self._session_error
        return self._session


class _FakeContext:
    def __init__(
        self,
        session: _FakeCdpSession,
        browser_session: _FakeCdpSession | None = None,
        *,
        browser_session_error: BaseException | None = None,
    ) -> None:
        self._session = session
        self.session_requests = 0
        self.browser = _FakeBrowser(
            browser_session or _FakeCdpSession(),
            session_error=browser_session_error,
        )

    def new_cdp_session(self, page: Any) -> _FakeCdpSession:
        self.session_requests += 1
        return self._session


class _FakePage:
    def __init__(
        self,
        session: _FakeCdpSession,
        *,
        browser_session: _FakeCdpSession | None = None,
        browser_session_error: BaseException | None = None,
        evaluation: dict[str, Any] | None = None,
        evaluation_sequence: list[dict[str, Any]] | None = None,
        url: str = "https://store.example.test/product?account=acme&token=do-not-record",
    ) -> None:
        self.context = _FakeContext(
            session,
            browser_session,
            browser_session_error=browser_session_error,
        )
        self._evaluation = evaluation or {
            "innerWidth": 1440,
            "innerHeight": 900,
            "devicePixelRatio": 2,
            "clientOriginX": 20,
            "clientOriginY": 70,
            "outerWidth": 1480,
            "outerHeight": 971,
            "fullscreen": False,
        }
        self._evaluation_sequence = list(evaluation_sequence or ())
        self.url = url
        self.evaluations: list[str] = []

    def evaluate(self, expression: str) -> Any:
        self.evaluations.append(expression)
        if self._evaluation_sequence:
            return deepcopy(self._evaluation_sequence.pop(0))
        return deepcopy(self._evaluation)


def test_sampler_returns_post_maximize_window_and_viewport_sample() -> None:
    session = _FakeCdpSession()
    browser_session = _FakeCdpSession()
    page = _FakePage(session, browser_session=browser_session)

    sample = ChromiumCaptureSampler(
        monotonic_clock=lambda: 123.25,
    ).prepare_and_sample(page)

    assert sample == ChromiumWindowSample(
        cdp_window_id=731,
        browser_pid=5172,
        bounds_dip=DisplayBounds(0, 25, 1512, 941),
        viewport_width_css=1440.0,
        viewport_height_css=900.0,
        device_pixel_ratio=2.0,
        maximized=True,
        fullscreen=False,
        sample_id=sample.sample_id,
        sampled_at_monotonic=123.25,
    )
    assert page.context.session_requests == 1
    assert page.context.browser.session_requests == 1
    assert session.detach_calls == 1
    assert browser_session.detach_calls == 1
    assert session.commands == [
        ("Browser.getWindowForTarget", None),
        (
            "Browser.setWindowBounds",
            {"windowId": 731, "bounds": {"windowState": "maximized"}},
        ),
        ("Page.bringToFront", None),
        ("Browser.getWindowForTarget", None),
        ("Page.getLayoutMetrics", None),
    ]
    assert browser_session.commands == [("SystemInfo.getProcessInfo", None)]
    assert len(page.evaluations) == 1
    assert all(
        property_name in page.evaluations[0]
        for property_name in (
            "window.innerWidth",
            "window.innerHeight",
            "window.devicePixelRatio",
            "document.fullscreenElement",
        )
    )


def test_web_area_sampler_returns_only_typed_page_geometry() -> None:
    page_session = _FakeCdpSession()
    browser_session = _FakeCdpSession()
    page = _FakePage(page_session, browser_session=browser_session)

    sample = ChromiumCaptureSampler(
        monotonic_clock=lambda: 123.25,
    ).sample_web_area(page)

    assert sample.browser_pid == 5172
    assert sample.viewport_width_css == 1440.0
    assert sample.viewport_height_css == 900.0
    assert sample.device_pixel_ratio == 2.0
    assert sample.client_origin_x_dip == 20.0
    assert sample.client_origin_y_dip == 70.0
    assert sample.layout_page_x_css == 0.0
    assert sample.layout_page_y_css == 0.0
    assert sample.layout_viewport_width_css == 1440.0
    assert sample.layout_viewport_height_css == 900.0
    assert sample.sampled_at_monotonic == 123.25
    assert sample.outer_width_css == 1480.0
    assert sample.outer_height_css == 971.0
    assert set(asdict(sample)) == {
        "browser_pid",
        "viewport_width_css",
        "viewport_height_css",
        "device_pixel_ratio",
        "client_origin_x_dip",
        "client_origin_y_dip",
        "outer_width_css",
        "outer_height_css",
        "layout_page_x_css",
        "layout_page_y_css",
        "layout_viewport_width_css",
        "layout_viewport_height_css",
        "sample_id",
        "sampled_at_monotonic",
    }
    assert page_session.commands == [("Page.getLayoutMetrics", None)]
    assert browser_session.commands == [("SystemInfo.getProcessInfo", None)]
    assert page.context.session_requests == 1
    assert page.context.browser.session_requests == 1
    assert page_session.detach_calls == 1
    assert browser_session.detach_calls == 1
    assert len(page.evaluations) == 1
    assert all(
        property_name in page.evaluations[0]
        for property_name in (
            "window.innerWidth",
            "window.innerHeight",
            "window.devicePixelRatio",
            "window.screenX",
            "window.screenY",
            "window.outerWidth",
            "window.outerHeight",
        )
    )
    assert all(
        forbidden not in page.evaluations[0]
        for forbidden in (
            "location",
            "cookie",
            "localStorage",
            "sessionStorage",
            "textContent",
            "innerText",
        )
    )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("outerWidth", None),
        ("outerHeight", None),
        ("outerWidth", math.nan),
        ("outerHeight", math.inf),
        ("outerWidth", 0),
        ("outerHeight", -1),
        ("outerWidth", 1439),
        ("outerHeight", 899),
    ],
    ids=(
        "missing-width",
        "missing-height",
        "nonfinite-width",
        "nonfinite-height",
        "nonpositive-width",
        "nonpositive-height",
        "width-smaller-than-viewport",
        "height-smaller-than-viewport",
    ),
)
def test_web_area_sampler_rejects_invalid_outer_window_geometry(
    field: str,
    bad_value: float | None,
) -> None:
    evaluation = {
        "innerWidth": 1440,
        "innerHeight": 900,
        "devicePixelRatio": 2,
        "clientOriginX": 40,
        "clientOriginY": 60,
        "outerWidth": 1480,
        "outerHeight": 971,
    }
    evaluation[field] = bad_value

    with pytest.raises(ValueError, match="outer"):
        ChromiumCaptureSampler().sample_web_area(
            _FakePage(_FakeCdpSession(), evaluation=evaluation),
        )


def test_web_area_sampler_rejects_css_layout_and_js_viewport_disagreement() -> None:
    page = _FakePage(
        _FakeCdpSession(),
        evaluation={
            "innerWidth": 1439,
            "innerHeight": 900,
            "devicePixelRatio": 2,
            "clientOriginX": 20,
            "clientOriginY": 70,
            "outerWidth": 1480,
            "outerHeight": 971,
        },
    )

    with pytest.raises(ValueError, match="viewport"):
        ChromiumCaptureSampler().sample_web_area(page)


def test_window_sampler_rejects_css_layout_and_js_viewport_disagreement() -> None:
    page = _FakePage(
        _FakeCdpSession(),
        evaluation={
            "innerWidth": 1440,
            "innerHeight": 899,
            "devicePixelRatio": 2,
            "fullscreen": False,
        },
    )

    with pytest.raises(ValueError, match="viewport"):
        ChromiumCaptureSampler().prepare_and_sample(page)


def test_preserved_window_sampler_uses_cdp_layout_when_js_viewport_differs() -> None:
    arranged = _window_response(state="normal")
    page = _FakePage(
        _FakeCdpSession(initial_window=arranged, sampled_window=arranged),
        evaluation={
            "innerWidth": 1440,
            "innerHeight": 899,
            "devicePixelRatio": 2,
            "fullscreen": False,
        },
    )

    sample = ChromiumCaptureSampler(
        window_mode=ChromiumWindowMode.PRESERVE,
        sleeper=lambda _seconds: None,
    ).prepare_and_sample(page)

    assert sample.viewport_width_css == 1440.0
    assert sample.viewport_height_css == 900.0


def test_preserved_web_area_sampler_uses_cdp_layout_when_js_viewport_differs() -> None:
    page = _FakePage(
        _FakeCdpSession(),
        evaluation={
            "innerWidth": 1440,
            "innerHeight": 899,
            "devicePixelRatio": 2,
            "fullscreen": False,
            "clientOriginX": 0,
            "clientOriginY": 25,
            "outerWidth": 1480,
            "outerHeight": 971,
        },
    )

    sample = ChromiumCaptureSampler(
        window_mode=ChromiumWindowMode.PRESERVE,
        sleeper=lambda _seconds: None,
    ).sample_web_area(page)

    assert sample.viewport_width_css == 1440.0
    assert sample.viewport_height_css == 900.0


def test_window_sampler_retries_transient_viewport_layout_disagreement() -> None:
    session = _FakeCdpSession()
    page = _FakePage(
        session,
        evaluation_sequence=[
            {
                "innerWidth": 1440,
                "innerHeight": 899,
                "devicePixelRatio": 2,
                "fullscreen": False,
            },
            {
                "innerWidth": 1440,
                "innerHeight": 900,
                "devicePixelRatio": 2,
                "fullscreen": False,
            },
        ],
    )

    sample = ChromiumCaptureSampler(
        sleeper=lambda _seconds: None,
    ).prepare_and_sample(page)

    assert sample.viewport_width_css == 1440.0
    assert sample.viewport_height_css == 900.0
    assert session.commands.count(("Page.getLayoutMetrics", None)) == 2
    assert len(page.evaluations) == 2


def test_web_area_sampler_uses_css_layout_viewport_when_cdp_viewports_differ() -> None:
    metrics = _layout_metrics()
    metrics["layoutViewport"] = {
        "pageX": 3,
        "pageY": 5,
        "clientWidth": 1512,
        "clientHeight": 941,
    }
    metrics["cssLayoutViewport"] = {
        "pageX": 125,
        "pageY": 250,
        "clientWidth": 1440,
        "clientHeight": 900,
    }
    page = _FakePage(
        _FakeCdpSession(layout_metrics=metrics),
    )

    sample = ChromiumCaptureSampler().sample_web_area(page)

    assert sample.layout_page_x_css == 125.0
    assert sample.layout_page_y_css == 250.0
    assert sample.layout_viewport_width_css == 1440.0
    assert sample.layout_viewport_height_css == 900.0
    assert sample.viewport_width_css == 1440.0
    assert sample.viewport_height_css == 900.0


def test_window_sampler_uses_css_layout_offsets_when_cdp_viewports_differ() -> None:
    metrics = _layout_metrics()
    metrics["layoutViewport"] = {
        "pageX": 3,
        "pageY": 5,
        "clientWidth": 1512,
        "clientHeight": 941,
    }
    metrics["cssLayoutViewport"] = {
        "pageX": 125,
        "pageY": 250,
        "clientWidth": 1440,
        "clientHeight": 900,
    }
    page = _FakePage(
        _FakeCdpSession(layout_metrics=metrics),
    )

    sample = ChromiumCaptureSampler().prepare_and_sample(page)

    assert sample.layout_page_x_css == 125.0
    assert sample.layout_page_y_css == 250.0
    assert sample.viewport_width_css == 1440.0
    assert sample.viewport_height_css == 900.0


@pytest.mark.parametrize("viewport_name", ["layoutViewport", "cssLayoutViewport"])
def test_sampler_rejects_missing_cdp_viewport(viewport_name: str) -> None:
    metrics = _layout_metrics()
    del metrics[viewport_name]

    with pytest.raises(ValueError, match=viewport_name):
        ChromiumCaptureSampler().sample_web_area(
            _FakePage(_FakeCdpSession(layout_metrics=metrics)),
        )


@pytest.mark.parametrize("viewport_name", ["layoutViewport", "cssLayoutViewport"])
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("pageX", None),
        ("pageY", math.nan),
        ("clientWidth", 0),
        ("clientHeight", math.inf),
    ],
)
def test_sampler_rejects_invalid_cdp_viewport_fields(
    viewport_name: str,
    field: str,
    bad_value: float | None,
) -> None:
    metrics = _layout_metrics()
    metrics[viewport_name][field] = bad_value

    with pytest.raises(ValueError, match=viewport_name):
        ChromiumCaptureSampler().sample_web_area(
            _FakePage(_FakeCdpSession(layout_metrics=metrics)),
        )


def test_normal_sampler_requests_normal_window_without_maximizing() -> None:
    session = _FakeCdpSession(
        sampled_window=_window_response(state="normal"),
    )
    browser_session = _FakeCdpSession()
    page = _FakePage(session, browser_session=browser_session)

    sample = ChromiumCaptureSampler(
        window_mode=ChromiumWindowMode.NORMAL,
        monotonic_clock=lambda: 123.25,
    ).prepare_and_sample(page)

    assert sample.maximized is False
    assert sample.bounds_dip == DisplayBounds(40, 60, 1280, 860)
    assert session.commands == [
        ("Browser.getWindowForTarget", None),
        (
            "Browser.setWindowBounds",
            {"windowId": 731, "bounds": {"windowState": "normal"}},
        ),
        ("Page.bringToFront", None),
        ("Browser.getWindowForTarget", None),
        ("Page.getLayoutMetrics", None),
    ]


def test_preserve_sampler_does_not_change_manually_arranged_window() -> None:
    arranged = _window_response(
        state="normal",
        bounds={
            "left": 84,
            "top": 92,
            "width": 1180,
            "height": 740,
            "windowState": "normal",
        },
    )
    session = _FakeCdpSession(
        initial_window=arranged,
        sampled_window=arranged,
    )
    browser_session = _FakeCdpSession()

    sample = ChromiumCaptureSampler(
        window_mode=ChromiumWindowMode.PRESERVE,
        monotonic_clock=lambda: 123.25,
    ).prepare_and_sample(_FakePage(session, browser_session=browser_session))

    assert sample.maximized is False
    assert sample.bounds_dip == DisplayBounds(84, 92, 1180, 740)
    assert session.commands == [
        ("Browser.getWindowForTarget", None),
        ("Page.bringToFront", None),
        ("Browser.getWindowForTarget", None),
        ("Page.getLayoutMetrics", None),
    ]


def test_sampler_rejects_untyped_window_mode() -> None:
    with pytest.raises(ValueError, match="ChromiumWindowMode"):
        ChromiumCaptureSampler(window_mode="normal")  # type: ignore[arg-type]


def test_sampler_reads_browser_pid_from_browser_level_cdp_session() -> None:
    page_session = _PageCdpSessionRejectsProcessInfo()
    browser_session = _FakeCdpSession()
    page = _FakePage(page_session, browser_session=browser_session)

    sample = ChromiumCaptureSampler().prepare_and_sample(page)

    assert sample.browser_pid == 5172
    assert "SystemInfo.getProcessInfo" not in [
        method for method, _params in page_session.commands
    ]
    assert browser_session.commands == [("SystemInfo.getProcessInfo", None)]


def test_sampler_detaches_both_sessions_when_page_sampling_fails() -> None:
    page_session = _FakeCdpSession(layout_metrics={"layoutViewport": []})
    browser_session = _FakeCdpSession()

    with pytest.raises(ValueError, match="layoutViewport"):
        ChromiumCaptureSampler().prepare_and_sample(
            _FakePage(page_session, browser_session=browser_session),
        )

    assert page_session.detach_calls == 1
    assert browser_session.detach_calls == 1


def test_sampler_detaches_both_sessions_when_browser_process_query_fails() -> None:
    page_session = _FakeCdpSession()
    browser_session = _BrowserCdpSessionFailsProcessInfo()

    with pytest.raises(RuntimeError, match="browser process query failed"):
        ChromiumCaptureSampler().prepare_and_sample(
            _FakePage(page_session, browser_session=browser_session),
        )

    assert browser_session.detach_calls == 1
    assert page_session.detach_calls == 1


def test_sampler_preserves_browser_session_error_when_page_detach_fails() -> None:
    page_session = _FakeCdpSession(detach_error=RuntimeError("page detach failed"))
    browser_error = RuntimeError("browser session failed")

    with pytest.raises(RuntimeError, match="browser session failed"):
        ChromiumCaptureSampler().prepare_and_sample(
            _FakePage(page_session, browser_session_error=browser_error),
        )

    assert page_session.detach_calls == 1


def test_sampler_preserves_page_sampling_error_when_both_detaches_fail() -> None:
    page_session = _FakeCdpSession(
        layout_metrics={"layoutViewport": []},
        detach_error=RuntimeError("page detach failed"),
    )
    browser_session = _FakeCdpSession(
        detach_error=RuntimeError("browser detach failed"),
    )

    with pytest.raises(ValueError, match="layoutViewport"):
        ChromiumCaptureSampler().prepare_and_sample(
            _FakePage(page_session, browser_session=browser_session),
        )

    assert browser_session.detach_calls == 1
    assert page_session.detach_calls == 1


def test_sampler_detaches_page_session_when_browser_session_is_invalid() -> None:
    page_session = _FakeCdpSession()
    browser_session = _FakeCdpSessionWithoutSend(
        detach_error=RuntimeError("browser detach failed"),
    )

    with pytest.raises(ValueError, match="invalid Chromium CDP session"):
        ChromiumCaptureSampler().prepare_and_sample(
            _FakePage(page_session, browser_session=browser_session),
        )

    assert browser_session.detach_calls == 1
    assert page_session.detach_calls == 1


def test_sampler_propagates_first_detach_error_after_successful_sample() -> None:
    page_session = _FakeCdpSession(detach_error=RuntimeError("page detach failed"))
    browser_session = _FakeCdpSession(
        detach_error=RuntimeError("browser detach failed"),
    )

    with pytest.raises(RuntimeError, match="browser detach failed"):
        ChromiumCaptureSampler().prepare_and_sample(
            _FakePage(page_session, browser_session=browser_session),
        )

    assert browser_session.detach_calls == 1
    assert page_session.detach_calls == 1


def test_sampler_rejects_fullscreen_page() -> None:
    session = _FakeCdpSession()
    page = _FakePage(session, evaluation={
        "innerWidth": 1440,
        "innerHeight": 900,
        "devicePixelRatio": 2,
        "fullscreen": True,
    })

    with pytest.raises(ValueError, match="fullscreen"):
        ChromiumCaptureSampler().prepare_and_sample(page)
    assert session.detach_calls == 1


def test_sampler_preserves_sampling_error_when_detach_also_fails() -> None:
    session = _FakeCdpSession(
        layout_metrics={"layoutViewport": []},
        detach_error=RuntimeError("detach failed"),
    )

    with pytest.raises(ValueError, match="layoutViewport"):
        ChromiumCaptureSampler().prepare_and_sample(_FakePage(session))

    assert session.detach_calls == 1


def test_sampler_fails_when_successful_sample_cannot_detach() -> None:
    session = _FakeCdpSession(detach_error=RuntimeError("detach failed"))

    with pytest.raises(RuntimeError, match="detach failed"):
        ChromiumCaptureSampler().prepare_and_sample(_FakePage(session))

    assert session.detach_calls == 1


def test_sampler_detaches_when_session_send_is_invalid() -> None:
    session = _FakeCdpSessionWithoutSend(detach_error=RuntimeError("detach failed"))

    with pytest.raises(ValueError, match="invalid Chromium CDP session"):
        ChromiumCaptureSampler().prepare_and_sample(_FakePage(session))

    assert session.detach_calls == 1


@pytest.mark.parametrize("browser_pids", [(), (5172, 5190)])
def test_sampler_rejects_missing_or_ambiguous_browser_process(
    browser_pids: tuple[int, ...],
) -> None:
    page = _FakePage(
        _FakeCdpSession(),
        browser_session=_FakeCdpSession(
            process_info=_process_info(browser_pids=browser_pids),
        ),
    )

    with pytest.raises(ValueError, match="browser process"):
        ChromiumCaptureSampler().prepare_and_sample(page)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("devicePixelRatio", 0),
        ("devicePixelRatio", -1),
        ("innerWidth", 0),
        ("innerHeight", -1),
    ],
)
def test_sampler_rejects_nonpositive_viewport_measurements(
    field: str,
    bad_value: int,
) -> None:
    evaluation = {
        "innerWidth": 1440,
        "innerHeight": 900,
        "devicePixelRatio": 2,
        "fullscreen": False,
    }
    evaluation[field] = bad_value
    page = _FakePage(_FakeCdpSession(), evaluation=evaluation)

    with pytest.raises(ValueError, match="positive"):
        ChromiumCaptureSampler().prepare_and_sample(page)


def test_sampler_generates_opaque_sample_id_without_url_query_values() -> None:
    secret = "a1b2c3-sensitive-query-value"
    page = _FakePage(
        _FakeCdpSession(),
        url=f"https://store.example.test/product?account=acme&token={secret}",
    )

    sample = ChromiumCaptureSampler().prepare_and_sample(page)

    assert sample.sample_id
    assert secret not in sample.sample_id
    assert "account" not in sample.sample_id
    assert page.url not in sample.sample_id


def test_sampler_uses_injected_monotonic_clock() -> None:
    sample = ChromiumCaptureSampler(
        monotonic_clock=lambda: 456.75,
    ).prepare_and_sample(_FakePage(_FakeCdpSession()))

    assert sample.sampled_at_monotonic == 456.75


@pytest.mark.parametrize(
    ("sampled_window", "layout_metrics", "process_info"),
    [
        (
            {"windowId": 731, "bounds": {"left": 0, "top": 0, "height": 941}},
            None,
            None,
        ),
        (_window_response(state="minimized"), None, None),
        (_window_response(state="fullscreen"), None, None),
        (
            _window_response(
                state="maximized",
                bounds={
                    "left": math.nan,
                    "top": 25,
                    "width": 1512,
                    "height": 941,
                    "windowState": "maximized",
                },
            ),
            None,
            None,
        ),
        (None, {"layoutViewport": []}, None),
        (None, None, {"processInfo": [{"type": "browser", "id": 5172.5, "cpuTime": 1}]}),
    ],
)
def test_sampler_fails_closed_for_malformed_or_unsafe_cdp_data(
    sampled_window: dict[str, Any] | None,
    layout_metrics: dict[str, Any] | None,
    process_info: dict[str, Any] | None,
) -> None:
    browser_session = _FakeCdpSession(process_info=process_info)
    page = _FakePage(
        _FakeCdpSession(
            sampled_window=sampled_window,
            layout_metrics=layout_metrics,
        ),
        browser_session=browser_session,
    )

    with pytest.raises(ValueError):
        ChromiumCaptureSampler().prepare_and_sample(page)

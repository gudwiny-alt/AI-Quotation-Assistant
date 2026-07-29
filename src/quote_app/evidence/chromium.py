"""Strict, single-session Chromium CDP window samples for native capture binding."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import uuid4

from quote_app.evidence.geometry import DisplayBounds

_VIEWPORT_SYNC_MAX_ATTEMPTS = 3
_VIEWPORT_SYNC_INTERVAL_SECONDS = 0.1


class ChromiumWindowMode(str, Enum):
    NORMAL = "normal"
    MAXIMIZED = "maximized"
    PRESERVE = "preserve"


@dataclass(frozen=True, slots=True)
class CdpWebAreaSample:
    """Geometry-only CDP proof sampled from one controlled page."""

    browser_pid: int
    viewport_width_css: float
    viewport_height_css: float
    device_pixel_ratio: float
    client_origin_x_dip: float
    client_origin_y_dip: float
    outer_width_css: float
    outer_height_css: float
    layout_page_x_css: float
    layout_page_y_css: float
    layout_viewport_width_css: float
    layout_viewport_height_css: float
    sample_id: str
    sampled_at_monotonic: float = 0.0

    def __post_init__(self) -> None:
        _positive_int(self.browser_pid, "Chromium process ID")
        for name, value in (
            ("viewport width", self.viewport_width_css),
            ("viewport height", self.viewport_height_css),
            ("device pixel ratio", self.device_pixel_ratio),
            ("outer window width", self.outer_width_css),
            ("outer window height", self.outer_height_css),
            ("layout viewport width", self.layout_viewport_width_css),
            ("layout viewport height", self.layout_viewport_height_css),
        ):
            _positive_number(value, name)
        if (
            self.outer_width_css < self.viewport_width_css
            or self.outer_height_css < self.viewport_height_css
        ):
            raise ValueError(
                "outer window dimensions must not be smaller than viewport"
            )
        for name, value in (
            ("client origin X", self.client_origin_x_dip),
            ("client origin Y", self.client_origin_y_dip),
            ("layout page X", self.layout_page_x_css),
            ("layout page Y", self.layout_page_y_css),
        ):
            _nonnegative_finite(value, name)
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("sample_id must be a nonblank string")
        _nonnegative_finite(
            self.sampled_at_monotonic,
            "sampled_at_monotonic",
        )


@dataclass(frozen=True, slots=True)
class ChromiumWindowSample:
    cdp_window_id: int
    browser_pid: int
    bounds_dip: DisplayBounds
    viewport_width_css: float
    viewport_height_css: float
    device_pixel_ratio: float
    maximized: bool
    fullscreen: bool
    sample_id: str
    sampled_at_monotonic: float = 0.0
    layout_page_x_css: float = 0.0
    layout_page_y_css: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("sample_id must be a nonblank string")
        _nonnegative_finite(
            self.sampled_at_monotonic,
            "sampled_at_monotonic",
        )
        _nonnegative_finite(self.layout_page_x_css, "layout page X")
        _nonnegative_finite(self.layout_page_y_css, "layout page Y")


class ChromiumCaptureSampler:
    """Prepare one Chromium window and return only its validated capture facts."""

    def __init__(
        self,
        monotonic_clock: Callable[[], float] | None = None,
        *,
        window_mode: ChromiumWindowMode = ChromiumWindowMode.MAXIMIZED,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(window_mode, ChromiumWindowMode):
            raise ValueError("window_mode must be ChromiumWindowMode")
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._window_mode = window_mode
        self._sleeper = sleeper or time.sleep

    def prepare_and_sample(self, page: Any) -> ChromiumWindowSample:
        context = getattr(page, "context", None)
        new_page_session = getattr(context, "new_cdp_session", None)
        browser = getattr(context, "browser", None)
        new_browser_session = getattr(browser, "new_browser_cdp_session", None)
        if not callable(new_page_session) or not callable(new_browser_session):
            raise ValueError("invalid Chromium capture page")
        page_session: Any = None
        page_detach: Any = None
        browser_session: Any = None
        browser_detach: Any = None
        sampling_error: BaseException | None = None
        try:
            page_session = new_page_session(page)
            page_detach = getattr(page_session, "detach", None)
            if not callable(page_detach):
                raise ValueError("Chromium CDP session must support detach")
            if page_session is None or not callable(getattr(page_session, "send", None)):
                raise ValueError("invalid Chromium CDP session")
            browser_session = new_browser_session()
            browser_detach = getattr(browser_session, "detach", None)
            if not callable(browser_detach):
                raise ValueError("Chromium CDP session must support detach")
            if browser_session is None or not callable(
                getattr(browser_session, "send", None),
            ):
                raise ValueError("invalid Chromium CDP session")

            window_id, _, current_window_state = self._read_window(page_session)
            expected_window_state = (
                current_window_state
                if self._window_mode is ChromiumWindowMode.PRESERVE
                else self._window_mode.value
            )
            if self._window_mode is not ChromiumWindowMode.PRESERVE:
                self._send(page_session, "Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {"windowState": expected_window_state},
                })
            self._send(page_session, "Page.bringToFront")

            sampled_window_id, bounds, window_state = self._read_window(page_session)
            if sampled_window_id != window_id:
                raise ValueError("Chromium window changed during sampling")
            if window_state != expected_window_state:
                raise ValueError(
                    "Chromium window did not reach the requested state"
                )

            for attempt in range(_VIEWPORT_SYNC_MAX_ATTEMPTS):
                (
                    layout_page_x,
                    layout_page_y,
                    layout_width,
                    layout_height,
                ) = self._read_layout_metrics(page_session)
                (
                    viewport_width,
                    viewport_height,
                    dpr,
                    fullscreen,
                ) = self._read_page_viewport(page)
                if (
                    viewport_width == layout_width
                    and viewport_height == layout_height
                ) or self._window_mode is ChromiumWindowMode.PRESERVE:
                    if self._window_mode is ChromiumWindowMode.PRESERVE:
                        viewport_width = layout_width
                        viewport_height = layout_height
                    break
                if attempt + 1 < _VIEWPORT_SYNC_MAX_ATTEMPTS:
                    self._sleeper(_VIEWPORT_SYNC_INTERVAL_SECONDS)
            else:
                raise ValueError("page viewport and CDP layout viewport differ")
            if fullscreen:
                raise ValueError("fullscreen Chromium page cannot be captured")
            browser_pid = self._read_browser_pid(browser_session)

            return ChromiumWindowSample(
                cdp_window_id=window_id,
                browser_pid=browser_pid,
                bounds_dip=bounds,
                viewport_width_css=viewport_width,
                viewport_height_css=viewport_height,
                device_pixel_ratio=dpr,
                maximized=window_state == ChromiumWindowMode.MAXIMIZED.value,
                fullscreen=False,
                sample_id=uuid4().hex,
                sampled_at_monotonic=_nonnegative_finite(
                    self._monotonic_clock(),
                    "sampled_at_monotonic",
                ),
                layout_page_x_css=layout_page_x,
                layout_page_y_css=layout_page_y,
            )
        except BaseException as error:
            sampling_error = error
            raise
        finally:
            detach_error: BaseException | None = None
            for detach in (browser_detach, page_detach):
                if not callable(detach):
                    continue
                try:
                    detach()
                except BaseException as error:
                    if detach_error is None:
                        detach_error = error
            if sampling_error is None and detach_error is not None:
                raise detach_error

    def sample_web_area(self, page: Any) -> CdpWebAreaSample:
        """Read only page geometry needed to prove the controlled web area."""
        context = getattr(page, "context", None)
        new_page_session = getattr(context, "new_cdp_session", None)
        browser = getattr(context, "browser", None)
        new_browser_session = getattr(browser, "new_browser_cdp_session", None)
        if not callable(new_page_session) or not callable(new_browser_session):
            raise ValueError("invalid Chromium capture page")
        page_session: Any = None
        page_detach: Any = None
        browser_session: Any = None
        browser_detach: Any = None
        sampling_error: BaseException | None = None
        try:
            page_session = new_page_session(page)
            page_detach = getattr(page_session, "detach", None)
            if not callable(page_detach):
                raise ValueError("Chromium CDP session must support detach")
            if not callable(getattr(page_session, "send", None)):
                raise ValueError("invalid Chromium CDP session")
            browser_session = new_browser_session()
            browser_detach = getattr(browser_session, "detach", None)
            if not callable(browser_detach):
                raise ValueError("Chromium CDP session must support detach")
            if not callable(getattr(browser_session, "send", None)):
                raise ValueError("invalid Chromium CDP session")

            for attempt in range(_VIEWPORT_SYNC_MAX_ATTEMPTS):
                (
                    layout_page_x,
                    layout_page_y,
                    layout_width,
                    layout_height,
                ) = self._read_layout_metrics(page_session)
                (
                    viewport_width,
                    viewport_height,
                    dpr,
                    client_origin_x,
                    client_origin_y,
                    outer_width,
                    outer_height,
                ) = self._read_web_area_viewport(page)
                if (
                    viewport_width == layout_width
                    and viewport_height == layout_height
                ) or self._window_mode is ChromiumWindowMode.PRESERVE:
                    if self._window_mode is ChromiumWindowMode.PRESERVE:
                        viewport_width = layout_width
                        viewport_height = layout_height
                    break
                if attempt + 1 < _VIEWPORT_SYNC_MAX_ATTEMPTS:
                    self._sleeper(_VIEWPORT_SYNC_INTERVAL_SECONDS)
            else:
                raise ValueError("page viewport and CDP layout viewport differ")

            return CdpWebAreaSample(
                browser_pid=self._read_browser_pid(browser_session),
                viewport_width_css=viewport_width,
                viewport_height_css=viewport_height,
                device_pixel_ratio=dpr,
                client_origin_x_dip=client_origin_x,
                client_origin_y_dip=client_origin_y,
                outer_width_css=outer_width,
                outer_height_css=outer_height,
                layout_page_x_css=layout_page_x,
                layout_page_y_css=layout_page_y,
                layout_viewport_width_css=layout_width,
                layout_viewport_height_css=layout_height,
                sample_id=uuid4().hex,
                sampled_at_monotonic=_nonnegative_finite(
                    self._monotonic_clock(),
                    "sampled_at_monotonic",
                ),
            )
        except BaseException as error:
            sampling_error = error
            raise
        finally:
            detach_error: BaseException | None = None
            for detach in (browser_detach, page_detach):
                if not callable(detach):
                    continue
                try:
                    detach()
                except BaseException as error:
                    if detach_error is None:
                        detach_error = error
            if sampling_error is None and detach_error is not None:
                raise detach_error

    def _read_window(self, session: Any) -> tuple[int, DisplayBounds, str]:
        response = self._send(session, "Browser.getWindowForTarget")
        window_id = _positive_int(response.get("windowId"), "CDP window ID")
        bounds = _dict(response.get("bounds"), "CDP window bounds")
        state = bounds.get("windowState")
        if type(state) is not str:
            raise ValueError("invalid Chromium window state")
        if state == "minimized":
            raise ValueError("minimized Chromium window cannot be captured")
        if state == "fullscreen":
            raise ValueError("fullscreen Chromium window cannot be captured")
        if state not in {"normal", "maximized"}:
            raise ValueError("invalid Chromium window state")

        return (
            window_id,
            DisplayBounds(
                _display_coordinate(bounds.get("left"), "window left"),
                _display_coordinate(bounds.get("top"), "window top"),
                _positive_int(bounds.get("width"), "window width"),
                _positive_int(bounds.get("height"), "window height"),
            ),
            state,
        )

    def _read_layout_metrics(
        self,
        session: Any,
    ) -> tuple[float, float, float, float]:
        response = self._send(session, "Page.getLayoutMetrics")
        parsed: dict[str, tuple[float, float, float, float]] = {}
        for name in ("layoutViewport", "cssLayoutViewport"):
            viewport = _dict(response.get(name), f"{name} metrics")
            parsed[name] = (
                _nonnegative_finite(viewport.get("pageX"), f"{name} page X"),
                _nonnegative_finite(viewport.get("pageY"), f"{name} page Y"),
                _positive_number(
                    viewport.get("clientWidth"),
                    f"{name} width",
                ),
                _positive_number(
                    viewport.get("clientHeight"),
                    f"{name} height",
                ),
            )
        return parsed["cssLayoutViewport"]

    def _read_page_viewport(self, page: Any) -> tuple[float, float, float, bool]:
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            raise ValueError("invalid Chromium capture page")
        response = evaluate(
            """() => ({
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                devicePixelRatio: window.devicePixelRatio,
                fullscreen: document.fullscreenElement !== null,
            })"""
        )
        values = _dict(response, "page viewport evaluation")
        fullscreen = values.get("fullscreen")
        if type(fullscreen) is not bool:
            raise ValueError("invalid fullscreen state")
        return (
            _positive_number(values.get("innerWidth"), "viewport width"),
            _positive_number(values.get("innerHeight"), "viewport height"),
            _positive_number(values.get("devicePixelRatio"), "device pixel ratio"),
            fullscreen,
        )

    def _read_web_area_viewport(
        self,
        page: Any,
    ) -> tuple[float, float, float, float, float, float, float]:
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            raise ValueError("invalid Chromium capture page")
        response = evaluate(
            """() => ({
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                devicePixelRatio: window.devicePixelRatio,
                clientOriginX: window.screenX,
                clientOriginY: window.screenY,
                outerWidth: window.outerWidth,
                outerHeight: window.outerHeight,
            })"""
        )
        values = _dict(response, "page web-area geometry evaluation")
        viewport_width = _positive_number(
            values.get("innerWidth"), "viewport width"
        )
        viewport_height = _positive_number(
            values.get("innerHeight"), "viewport height"
        )
        outer_width = _positive_number(
            values.get("outerWidth"), "outer window width"
        )
        outer_height = _positive_number(
            values.get("outerHeight"), "outer window height"
        )
        if outer_width < viewport_width or outer_height < viewport_height:
            raise ValueError(
                "outer window dimensions must not be smaller than viewport"
            )
        return (
            viewport_width,
            viewport_height,
            _positive_number(
                values.get("devicePixelRatio"),
                "device pixel ratio",
            ),
            _nonnegative_finite(
                values.get("clientOriginX"),
                "client origin X",
            ),
            _nonnegative_finite(
                values.get("clientOriginY"),
                "client origin Y",
            ),
            outer_width,
            outer_height,
        )

    def _read_browser_pid(self, session: Any) -> int:
        response = self._send(session, "SystemInfo.getProcessInfo")
        processes = response.get("processInfo")
        if type(processes) is not list:
            raise ValueError("invalid Chromium process information")

        browser_pids: list[int] = []
        for process in processes:
            item = _dict(process, "Chromium process")
            process_type = item.get("type")
            if type(process_type) is not str:
                raise ValueError("invalid Chromium process type")
            pid = _positive_int(item.get("id"), "Chromium process ID")
            _finite_number(item.get("cpuTime"), "Chromium process CPU time")
            if process_type == "browser":
                browser_pids.append(pid)

        if len(browser_pids) != 1:
            raise ValueError("exactly one Chromium browser process is required")
        return browser_pids[0]

    @staticmethod
    def _send(session: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = session.send(method) if params is None else session.send(method, params)
        return _dict(response, f"{method} response")


def _dict(value: Any, description: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"invalid {description}")
    return value


def _finite_number(value: Any, description: str) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{description} must be finite")
    return float(value)


def _positive_number(value: Any, description: str) -> float:
    number = _finite_number(value, description)
    if number <= 0:
        raise ValueError(f"{description} must be positive")
    return number


def _nonnegative_finite(value: Any, description: str) -> float:
    number = _finite_number(value, description)
    if number < 0:
        raise ValueError(f"{description} must be non-negative")
    return number


def _positive_int(value: Any, description: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{description} must be a positive integer")
    return value


def _display_coordinate(value: Any, description: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{description} must be a finite integer")
    return value

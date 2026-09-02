"""Strict production composition for formal macOS evidence capture."""

from __future__ import annotations

import math
import threading
import time
import platform as host_platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from quote_app.evidence.chromium import (
    CdpWebAreaSample,
    ChromiumCaptureSampler,
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
    browser_overlaps_system_ui,
    make_geometry_snapshot,
    make_system_ui_proof,
)
from quote_app.evidence.macos_native import (
    MacNativeBridge,
    MacPermissionState,
    NativeSystemUISample,
    NativeWindowSample,
    safe_capture_window_has_required_coverage,
)
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureContext,
    CaptureGeometrySnapshot,
    CaptureRequest,
    EvidenceCaptureError,
    SystemUIProof,
    make_cdp_web_area_geometry_snapshot,
    make_capture_error,
    make_macos_full_display_geometry_snapshot,
    revalidate_capture_error,
)
from quote_app.evidence.models import (
    EvidenceRecord,
    MacCapturePolicy,
    validate_mac_capture_policy,
)
from quote_app.evidence.semantic_state import (
    SemanticStateReader,
    VerifiedPageStateProbe,
    VerifiedSemanticState,
)
from quote_app.sites.catalog import SiteSpec
from quote_app.sites.detail_capture_view import CaptureViewGeometryError
from quote_app.tasks.models import BusinessOutcome, WebsiteChannel, WebsiteTask
from quote_app.tasks.retry import LayoutRecognitionError, LoginRequired

ReaderFactory = Callable[
    [WebsiteTask, Any, VerifiedSemanticState],
    SemanticStateReader,
]
EnvironmentFactory = Callable[..., MacOSCaptureEnvironment]
_ReaderKey = tuple[str, WebsiteChannel]
_T = TypeVar("_T")
_LAYOUT_CONVERGENCE_MAX_SAMPLES = 3
_LAYOUT_CONVERGENCE_INTERVAL_SECONDS = 0.1
_LAYOUT_CONVERGENCE_TIMEOUT_SECONDS = 0.2
_LAYOUT_CONVERGENCE_CLOCK_EPSILON = 1e-9
_MAC_VISUAL_REVIEW_WINDOW_ARGS = (
    "--window-position=24,49",
    "--window-size=1464,893",
)
_NO_MODEL_PROOF_FRAME_ID = "quotation-no-model-proof-frame"
_INSTALL_NO_MODEL_PROOF_FRAME = f"""
(bounds) => {{
  const frameId = "{_NO_MODEL_PROOF_FRAME_ID}";
  document.getElementById(frameId)?.remove();
  const frame = document.createElement("div");
  frame.id = frameId;
  frame.setAttribute("aria-hidden", "true");
  const properties = {{
    position: "fixed",
    left: `${{bounds.left}}px`,
    top: `${{bounds.top}}px`,
    width: `${{bounds.width}}px`,
    height: `${{bounds.height}}px`,
    "box-sizing": "border-box",
    border: "4px solid rgb(255, 0, 0)",
    "border-radius": "0",
    background: "transparent",
    "pointer-events": "none",
    "z-index": "2147483647",
  }};
  for (const [name, value] of Object.entries(properties)) {{
    frame.style.setProperty(name, value, "important");
  }}
  document.documentElement.appendChild(frame);
}}
"""
_REMOVE_NO_MODEL_PROOF_FRAME = f"""
() => document.getElementById("{_NO_MODEL_PROOF_FRAME_ID}")?.remove()
"""
_SIX_CAPTURE_BRANDS = ("HONOR", "小米", "欧珀", "维沃", "华为", "苹果")
_LIVE_OFFICIAL_CAPTURE_BRANDS = _SIX_CAPTURE_BRANDS
_MARKETPLACE_CAPTURE_BRANDS = _SIX_CAPTURE_BRANDS
_CAPTURE_VIEW_GEOMETRY_MESSAGES = {
    "缩放验证": "正式截图缩放验证失败",
    "搜索框定位": "正式截图搜索框定位失败",
    "结果区域定位": "正式截图结果区域定位失败",
}
_CAPTURE_VIEW_SEMANTIC_MESSAGES = {
    "URL复核": "正式截图URL复核失败",
    "店铺复核": "正式截图店铺复核失败",
    "商品结果复核": "正式截图商品结果复核失败",
    "页面语义复核": "正式截图页面语义复核失败",
}
_SAFE_CAPTURE_VIEW_MESSAGES = frozenset(
    (
        *_CAPTURE_VIEW_GEOMETRY_MESSAGES.values(),
        *_CAPTURE_VIEW_SEMANTIC_MESSAGES.values(),
    )
)


class _Sampler(Protocol):
    def prepare_and_sample(self, page: Any) -> ChromiumWindowSample: ...

    def sample_web_area(self, page: Any) -> CdpWebAreaSample: ...


class _Binder(Protocol):
    def bind(
        self,
        chromium: ChromiumWindowSample,
        native_windows: tuple[NativeWindowSample, ...],
        *,
        now_monotonic: float | None = None,
    ) -> BoundMacWindow: ...


class _Bridge(Protocol):
    def permissions(self) -> MacPermissionState: ...

    def windows(self) -> tuple[NativeWindowSample, ...]: ...

    def activate_window(self, identity: BrowserWindowIdentity) -> None: ...

    def web_area(self, identity: BrowserWindowIdentity) -> Any: ...

    def primary_display_bounds(self) -> Any: ...

    def system_ui(
        self,
        identity: BrowserWindowIdentity,
        *,
        policy: MacCapturePolicy,
    ) -> Any: ...

    def prepare_safe_capture_window(
        self,
        identity: BrowserWindowIdentity,
        *,
        menu_bar: Any,
        dock: Any,
        display: Any,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> None: ...

    def window_bounds(
        self,
        identity: BrowserWindowIdentity,
    ) -> NativeWindowSample: ...

    def focused_window(self) -> NativeWindowSample: ...


class _AdapterRegistry(Protocol):
    def adapter_for(
        self,
        brand: str,
        channel: WebsiteChannel,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _CurrentState:
    permissions: MacPermissionState
    bound: BoundMacWindow
    page: Any
    generation: object
    probe: VerifiedPageStateProbe | None
    restore_capture_view: Callable[[], None] | None = None


class _LeaseBoundMacOSEvidenceCapture(MacOSEvidenceCapture):
    def __init__(
        self,
        runtime: MacFormalCaptureRuntime,
        environment: MacOSCaptureEnvironment,
        *,
        policy: MacCapturePolicy,
    ) -> None:
        self._runtime = runtime
        super().__init__(environment, policy=policy)

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        return self._runtime._capture_once(self, request)

    def _capture_unchecked(
        self,
        request: CaptureRequest,
    ) -> EvidenceRecord:
        return super().capture(request)


class MacFormalCaptureRuntime:
    """Compose one fail-closed, exact-window macOS capture bundle at a time."""

    def __init__(
        self,
        *,
        sampler: _Sampler | None = None,
        bridge: _Bridge | None = None,
        binder: _Binder | None = None,
        adapter_registry: _AdapterRegistry | None = None,
        reader_factories: Mapping[_ReaderKey, ReaderFactory] | None = None,
        environment_factory: EnvironmentFactory = MacOSCaptureEnvironment,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    ) -> None:
        validate_mac_capture_policy(
            policy,
            platform_name=host_platform.system(),
        )
        self._policy = policy
        uses_darwin_beta_visual_review = (
            policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
            and host_platform.system() == "Darwin"
        )
        self._sampler = sampler or ChromiumCaptureSampler(
            window_mode=(
                ChromiumWindowMode.PRESERVE
                if uses_darwin_beta_visual_review
                else ChromiumWindowMode.NORMAL
            ),
        )
        self._bridge = bridge or MacNativeBridge()
        self._binder = binder or MacWindowBinder(
            window_coordinate_scale=(
                1.0 if uses_darwin_beta_visual_review else None
            )
        )
        self._environment_factory = environment_factory
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._adapter_registry = adapter_registry
        if reader_factories is None:
            self._reader_factories = (
                {
                    **{
                        (brand, channel):
                            self._injected_marketplace_reader_factory
                        for brand in _MARKETPLACE_CAPTURE_BRANDS
                        for channel in (
                            WebsiteChannel.JD,
                            WebsiteChannel.TMALL,
                        )
                    },
                    **{
                        (brand, WebsiteChannel.OFFICIAL):
                            self._injected_live_official_reader_factory
                        for brand in _LIVE_OFFICIAL_CAPTURE_BRANDS
                    },
                }
                if adapter_registry is not None
                else {}
            )
        else:
            self._reader_factories = dict(reader_factories)
        self._prepared_adapters: dict[_ReaderKey, object] = {}
        self._lock = threading.RLock()
        self._current: _CurrentState | None = None
        environment_options: dict[str, Any] = {
            "geometry_snapshot_provider": self._geometry_snapshot,
            "foreground_provider": self._foreground_window,
            "prepare_callback": self._prepare_browser,
            "permission_provider": self._screen_permission,
            "system_ui_proof_provider": self._system_ui_proof,
        }
        if self._uses_darwin_beta_visual_review():
            environment_options["validate_capture_scale_dpr"] = False
        self._environment = self._environment_factory(
            **environment_options,
        )
        self._capture = _LeaseBoundMacOSEvidenceCapture(
            self,
            self._environment,
            policy=self._policy,
        )

    def capture_context_provider(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> CaptureContext:
        with self._lock:
            return self._build_capture_context(task, page, state)

    def browser_launch_args(self) -> tuple[str, ...] | None:
        """Start the Mac pilot in one stable, tall normal window."""
        if self._uses_darwin_beta_visual_review():
            return _MAC_VISUAL_REVIEW_WINDOW_ARGS
        return None

    def browser_startup_preflight(
        self,
    ) -> Callable[[Any], None] | None:
        """Fixed launch arguments establish the Mac pilot window contract."""
        return None

    def _build_capture_context(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> CaptureContext:
        self._current = None
        restore_capture_view: Callable[[], None] | None = None
        context_built = False
        try:
            if not isinstance(task, WebsiteTask):
                raise make_capture_error(
                    "CAPTURE_ENVIRONMENT",
                    "截图任务上下文无效",
                )
            if not isinstance(state, VerifiedSemanticState):
                raise make_capture_error(
                    "CAPTURE_ENVIRONMENT",
                    "已验证页面状态无效",
                )
            permissions = self._permissions()
            first_sample = self._stage(
                "CAPTURE_ENVIRONMENT",
                "Chromium 截图采样不可用",
                lambda: self._sampler.prepare_and_sample(page),
            )
            first_bound = self._bind(first_sample)
            self._stage(
                "CAPTURE_FOREGROUND",
                "macOS 浏览器窗口无法激活",
                lambda: self._activate_bound_window(first_bound.identity),
            )
            second_sample = self._stage(
                "CAPTURE_ENVIRONMENT",
                "Chromium 激活后采样不可用",
                lambda: self._sampler.prepare_and_sample(page),
            )
            current_bound = self._bind(second_sample)
            if current_bound.identity != first_bound.identity:
                raise make_capture_error(
                    "CAPTURE_WINDOW_IDENTITY",
                    "激活后浏览器窗口身份发生变化",
                )

            generation = object()
            self._current = _CurrentState(
                permissions=permissions,
                bound=current_bound,
                page=page,
                generation=generation,
                probe=None,
            )
            current_bound = self._prepare_safe_window(
                page,
                current_bound,
            )

            reader_factory = self._reader_factories.get(
                (task.brand, task.channel)
            )
            if reader_factory is None:
                raise make_capture_error(
                    "CAPTURE_ENVIRONMENT",
                    "当前品牌渠道没有受控页面状态读取器",
                )
            reader = self._stage(
                "CAPTURE_ENVIRONMENT",
                "受控页面状态读取器无法创建",
                lambda: reader_factory(task, page, state),
            )
            restore_capture_view = self._prepared_capture_view_restorer(
                task,
                page,
                state,
            )
            current = self._require_current()
            self._current = _CurrentState(
                permissions=current.permissions,
                bound=current.bound,
                page=current.page,
                generation=current.generation,
                probe=current.probe,
                restore_capture_view=restore_capture_view,
            )
            capture_rectangles = self._capture_view_stage(
                lambda: self._capture_rectangles_for_current_view(
                    task,
                    page,
                    state,
                ),
                environment_message="正式截图证据区域无法读取",
            )
            if (
                self._uses_darwin_beta_visual_review()
                and state.outcome is BusinessOutcome.NO_MODEL
                and capture_rectangles
            ):
                overlay_restorer = self._stage(
                    "CAPTURE_ENVIRONMENT",
                    "未找到机型红框无法绘制",
                    lambda: self._install_no_model_proof_frame(
                        page,
                        capture_rectangles,
                    ),
                )
                restore_capture_view = self._compose_capture_view_restorers(
                    overlay_restorer,
                    restore_capture_view,
                )
                current = self._require_current()
                self._current = _CurrentState(
                    permissions=current.permissions,
                    bound=current.bound,
                    page=current.page,
                    generation=current.generation,
                    probe=current.probe,
                    restore_capture_view=restore_capture_view,
                )

            probe: VerifiedPageStateProbe | None = None

            def read_current_state() -> VerifiedSemanticState:
                with self._lock:
                    current = self._current
                    if (
                        current is None
                        or current.generation is not generation
                        or current.probe is not probe
                    ):
                        self._current = None
                        raise make_capture_error(
                            "CAPTURE_WINDOW_IDENTITY",
                            "页面状态探针租约已失效",
                        )
                    try:
                        current_state = reader()
                        if not isinstance(
                            current_state,
                            VerifiedSemanticState,
                        ):
                            raise ValueError(
                                "reader returned invalid semantic state"
                            )
                        return current_state
                    except EvidenceCaptureError as error:
                        self._current = None
                        raise revalidate_capture_error(
                            error,
                            fallback_code="CAPTURE_ENVIRONMENT",
                            safe_message="受控页面状态读取失败",
                        ) from None
                    except BaseException:
                        self._current = None
                        raise

            probe = VerifiedPageStateProbe(read_current_state)
            self._current = _CurrentState(
                permissions=permissions,
                bound=current_bound,
                page=page,
                generation=generation,
                probe=probe,
                restore_capture_view=restore_capture_view,
            )
            context = CaptureContext(
                expected_window=current_bound.identity,
                stability_probe=probe,
                css_rectangles=capture_rectangles,
            )
            context_built = True
            return context
        except (KeyboardInterrupt, SystemExit):
            self._current = None
            raise
        except LoginRequired:
            # A marketplace can display a login or risk-control page after its
            # offer was observed but just before evidence capture.  Preserve
            # that control signal so the scheduler can park the task for the
            # user instead of misreporting it as a capture-environment fault.
            self._current = None
            raise
        except EvidenceCaptureError as error:
            self._current = None
            if (
                error.code in {"CAPTURE_GEOMETRY", "CAPTURE_ENVIRONMENT"}
                and error.message in _SAFE_CAPTURE_VIEW_MESSAGES
            ):
                raise
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="macOS 正式截图上下文组合失败",
            ) from None
        except Exception:
            self._current = None
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "macOS 正式截图运行时组合失败",
            ) from None
        finally:
            if not context_built and restore_capture_view is not None:
                self._current = None
                try:
                    restore_capture_view()
                except Exception:
                    pass

    def evidence_capture(self) -> MacOSEvidenceCapture:
        return self._capture

    def _capture_once(
        self,
        capture: _LeaseBoundMacOSEvidenceCapture,
        request: CaptureRequest,
    ) -> EvidenceRecord:
        with self._lock:
            capture_error: BaseException | None = None
            restore_capture_view: Callable[[], None] | None = None
            try:
                if not isinstance(request, CaptureRequest):
                    raise ValueError(
                        "request must be a CaptureRequest"
                    )
                current = self._require_current()
                restore_capture_view = current.restore_capture_view
                if (
                    current.probe is None
                    or request.stability_probe is not current.probe
                ):
                    raise make_capture_error(
                        "CAPTURE_WINDOW_IDENTITY",
                        "截图请求不属于当前页面状态租约",
                    )
                if request.expected_window != current.bound.identity:
                    raise make_capture_error(
                        "CAPTURE_WINDOW_IDENTITY",
                        "截图请求窗口不属于当前页面状态租约",
                    )
                try:
                    return capture._capture_unchecked(request)
                except EvidenceCaptureError as error:
                    raise revalidate_capture_error(
                        error,
                        fallback_code="CAPTURE_ENVIRONMENT",
                        safe_message="macOS 正式截图失败",
                    ) from None
            except BaseException as error:
                capture_error = error
                raise
            finally:
                self._current = None
                if restore_capture_view is not None:
                    try:
                        restore_capture_view()
                    except Exception:
                        if capture_error is None:
                            raise

    def _permissions(self) -> MacPermissionState:
        permissions = self._stage(
            "CAPTURE_ENVIRONMENT",
            "macOS 权限预检失败",
            self._bridge.permissions,
        )
        if not isinstance(permissions, MacPermissionState):
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "macOS 权限预检返回值无效",
            )
        if not permissions.screen_recording:
            raise make_capture_error(
                "CAPTURE_PERMISSION",
                "macOS 未授予屏幕录制权限",
            )
        if not permissions.accessibility:
            raise make_capture_error(
                "CAPTURE_ACCESSIBILITY",
                "macOS 未授予辅助功能权限",
            )
        return permissions

    def _bind(
        self,
        sample: ChromiumWindowSample,
    ) -> BoundMacWindow:
        windows = self._stage(
            "CAPTURE_WINDOW_IDENTITY",
            "macOS 原生窗口列表不可用",
            self._bridge.windows,
        )
        bound = self._stage(
            "CAPTURE_WINDOW_IDENTITY",
            "Chromium 与 macOS 窗口无法唯一绑定",
            lambda: self._binder.bind(
                sample,
                windows,
                now_monotonic=self._monotonic_clock(),
            ),
        )
        return bound

    def _require_current(self) -> _CurrentState:
        current = self._current
        if current is None:
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "macOS 正式截图上下文尚未就绪",
            )
        return current

    def _activate_bound_window(
        self,
        identity: BrowserWindowIdentity,
    ) -> None:
        for attempt in range(3):
            try:
                self._bridge.activate_window(identity)
                return
            except EvidenceCaptureError as error:
                if error.code != "CAPTURE_FOREGROUND" or attempt == 2:
                    raise
                self._sleeper(0.15)

    def _require_identity(
        self,
        expected: BrowserWindowIdentity,
    ) -> _CurrentState:
        current = self._require_current()
        if expected != current.bound.identity:
            self._current = None
            raise make_capture_error(
                "CAPTURE_WINDOW_IDENTITY",
                "截图提供器拒绝非当前浏览器窗口身份",
            )
        return current

    def _screen_permission(self) -> bool:
        permissions = self._require_current().permissions
        return (
            permissions.screen_recording
            and permissions.accessibility
        )

    def _prepare_browser(
        self,
        expected: BrowserWindowIdentity,
    ) -> None:
        current = self._require_identity(expected)
        self._stage(
            "CAPTURE_FOREGROUND",
            "当前 macOS 浏览器窗口无法重新激活",
            lambda: self._activate_bound_window(current.bound.identity),
        )
        if self._uses_darwin_beta_visual_review():
            self._refresh_bound_for_capture(current)

    def _post_capture_validation(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> None:
        if not self._uses_darwin_beta_visual_review():
            return
        if not isinstance(snapshot, CaptureGeometrySnapshot):
            raise ValueError("snapshot must be CaptureGeometrySnapshot")
        current = self._require_identity(snapshot.expected_window)
        self._refresh_bound_for_capture(current)
        self._system_ui_proof(snapshot)

    def _uses_darwin_beta_visual_review(self) -> bool:
        return (
            self._policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
            and host_platform.system() == "Darwin"
        )

    def _refresh_bound_for_capture(
        self,
        current: _CurrentState,
    ) -> BoundMacWindow:
        identity = current.bound.identity
        native = self._stage(
            "CAPTURE_SYSTEM_UI",
            "macOS 截图窗口边界不可用",
            lambda: self._bridge.window_bounds(identity),
        )
        if (
            not isinstance(native, NativeWindowSample)
            or native.process_id != identity.process_id
            or str(native.window_id) != identity.window_handle
            or not native.on_screen
            or native.minimized
            or native.layer != 0
        ):
            raise make_capture_error(
                "CAPTURE_SYSTEM_UI",
                "macOS 截图窗口边界身份发生变化",
            )
        try:
            bound = BoundMacWindow(
                identity=identity,
                chromium=current.bound.chromium,
                native=native,
            )
        except ValueError:
            raise make_capture_error(
                "CAPTURE_SYSTEM_UI",
                "macOS 截图窗口边界无效",
            ) from None
        self._current = _CurrentState(
            permissions=current.permissions,
            bound=bound,
            page=current.page,
            generation=current.generation,
            probe=current.probe,
            restore_capture_view=current.restore_capture_view,
        )
        return bound

    def _geometry_snapshot(
        self,
        expected: BrowserWindowIdentity,
    ) -> CaptureGeometrySnapshot:
        current = self._require_identity(expected)
        identity = current.bound.identity
        if self._uses_darwin_beta_visual_review():
            display = self._stage(
                "CAPTURE_GEOMETRY",
                "macOS 主显示器边界不可用",
                self._bridge.primary_display_bounds,
            )
            return self._stage(
                "CAPTURE_GEOMETRY",
                "macOS 整屏视觉复核几何证明失败",
                lambda: make_macos_full_display_geometry_snapshot(
                    current.bound,
                    display,
                    now_monotonic=self._monotonic_clock(),
                ),
            )
        web_area = self._web_area_or_cdp(current)
        display = self._stage(
            "CAPTURE_GEOMETRY",
            "macOS 主显示器边界不可用",
            self._bridge.primary_display_bounds,
        )
        if isinstance(web_area, CdpWebAreaSample):
            return self._stage(
                "CAPTURE_GEOMETRY",
                "macOS CDP 网页区域几何证明失败",
                lambda: make_cdp_web_area_geometry_snapshot(
                    expected_window=identity,
                    chromium=current.bound.chromium,
                    browser_physical_bounds=current.bound.native.bounds_px,
                    display_physical_bounds=display,
                    sample=web_area,
                    now_monotonic=self._monotonic_clock(),
                ),
            )
        return self._stage(
            "CAPTURE_GEOMETRY",
            "macOS 截图几何证明失败",
            lambda: make_geometry_snapshot(
                current.bound,
                web_area,
                display,
                now_monotonic=self._monotonic_clock(),
            ),
        )

    def _web_area_or_cdp(
        self,
        current: _CurrentState,
    ) -> Any:
        identity = current.bound.identity
        try:
            return self._bridge.web_area(identity)
        except (KeyboardInterrupt, SystemExit):
            self._current = None
            raise
        except LoginRequired:
            self._current = None
            raise
        except EvidenceCaptureError as error:
            if (
                self._policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
                and host_platform.system() == "Darwin"
                and error.code == "CAPTURE_ACCESSIBILITY"
                and error.message == "WEB_AREA_TIMEOUT"
            ):
                return self._stage(
                    "CAPTURE_GEOMETRY",
                    "当前受控页面 CDP 网页区域采样不可用",
                    lambda: self._sampler.sample_web_area(current.page),
                )
            self._current = None
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message="当前 macOS 浏览器网页区域不可用",
            ) from None
        except Exception:
            self._current = None
            raise make_capture_error(
                "CAPTURE_GEOMETRY",
                "当前 macOS 浏览器网页区域不可用",
            ) from None

    def _system_ui_proof(
        self,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof:
        if not isinstance(snapshot, CaptureGeometrySnapshot):
            self._current = None
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                "macOS 系统栏证明收到无效几何",
            )
        current = self._require_identity(snapshot.expected_window)
        identity = current.bound.identity
        sample = self._sample_system_ui(identity)
        return self._make_system_ui_proof(
            current.bound,
            sample,
            snapshot,
        )

    def _sample_system_ui(
        self,
        identity: BrowserWindowIdentity,
    ) -> Any:
        return self._stage(
            "CAPTURE_SYSTEM_UI",
            "macOS 系统栏采样失败",
            lambda: self._bridge.system_ui(identity, policy=self._policy),
        )

    def _make_system_ui_proof(
        self,
        bound: BoundMacWindow,
        sample: Any,
        snapshot: CaptureGeometrySnapshot,
    ) -> SystemUIProof:
        return self._stage(
            "CAPTURE_SYSTEM_UI",
            "macOS 系统栏证明失败",
            lambda: make_system_ui_proof(
                bound,
                sample,
                snapshot.display_physical_bounds,
                now_monotonic=self._monotonic_clock(),
                policy=self._policy,
            ),
        )

    def _prepare_safe_window(
        self,
        page: Any,
        current_bound: BoundMacWindow,
    ) -> BoundMacWindow:
        if self._uses_darwin_beta_visual_review():
            return current_bound
        identity = current_bound.identity
        snapshot = self._environment.geometry_snapshot(identity)
        sample = self._sample_system_ui(identity)
        self._stage(
            "CAPTURE_SYSTEM_UI",
            "macOS 系统栏证明失败",
            lambda: browser_overlaps_system_ui(
                current_bound,
                sample,
                snapshot.display_physical_bounds,
                now_monotonic=self._monotonic_clock(),
                policy=self._policy,
            ),
        )

        self._stage(
            "CAPTURE_SYSTEM_UI",
            "macOS 浏览器安全窗口准备失败",
            lambda: self._bridge.prepare_safe_capture_window(
                identity,
                menu_bar=sample.menu_bar_bounds_px,
                dock=sample.dock_bounds_px,
                display=snapshot.display_physical_bounds,
                policy=self._policy,
            ),
        )
        self._stage(
            "CAPTURE_SYSTEM_UI",
            "macOS 浏览器窗口布局未收敛",
            lambda: self._wait_for_layout_convergence(
                identity,
                sample,
                snapshot.display_physical_bounds,
            ),
        )
        safe_sample = self._stage(
            "CAPTURE_ENVIRONMENT",
            "Chromium 安全窗口准备后采样不可用",
            lambda: self._sampler.prepare_and_sample(page),
        )
        safe_bound = self._bind(safe_sample)
        if safe_bound.identity != identity:
            raise make_capture_error(
                "CAPTURE_WINDOW_IDENTITY",
                "安全窗口准备后浏览器窗口身份发生变化",
            )
        current = self._require_identity(identity)
        self._current = _CurrentState(
            permissions=current.permissions,
            bound=safe_bound,
            page=current.page,
            generation=current.generation,
            probe=current.probe,
            restore_capture_view=current.restore_capture_view,
        )
        self._foreground_window()
        safe_snapshot = self._environment.geometry_snapshot(identity)
        self._environment.system_ui_proof(safe_snapshot)
        return safe_bound

    def _wait_for_layout_convergence(
        self,
        identity: BrowserWindowIdentity,
        system_ui: NativeSystemUISample,
        display: DisplayBounds,
    ) -> None:
        if not isinstance(system_ui, NativeSystemUISample):
            raise ValueError("system_ui must be NativeSystemUISample")
        started_at = self._layout_convergence_time()
        deadline = started_at + _LAYOUT_CONVERGENCE_TIMEOUT_SECONDS

        for attempt in range(_LAYOUT_CONVERGENCE_MAX_SAMPLES):
            if attempt:
                before_sleep = self._layout_convergence_time()
                if (
                    before_sleep + _LAYOUT_CONVERGENCE_INTERVAL_SECONDS
                    > deadline + _LAYOUT_CONVERGENCE_CLOCK_EPSILON
                ):
                    raise make_capture_error(
                        "CAPTURE_SYSTEM_UI",
                        "macOS 浏览器窗口布局收敛超时",
                    )
                self._sleeper(_LAYOUT_CONVERGENCE_INTERVAL_SECONDS)
                if (
                    self._layout_convergence_time()
                    > deadline + _LAYOUT_CONVERGENCE_CLOCK_EPSILON
                ):
                    raise make_capture_error(
                        "CAPTURE_SYSTEM_UI",
                        "macOS 浏览器窗口布局收敛超时",
                    )

            bounds = self._bridge.window_bounds(identity)
            if (
                not isinstance(bounds, NativeWindowSample)
                or bounds.process_id != identity.process_id
                or str(bounds.window_id) != identity.window_handle
                or not bounds.on_screen
                or bounds.minimized
                or bounds.layer != 0
            ):
                raise make_capture_error(
                    "CAPTURE_SYSTEM_UI",
                    "macOS 浏览器窗口布局收敛身份发生变化",
                )
            if (
                self._layout_convergence_time()
                > deadline + _LAYOUT_CONVERGENCE_CLOCK_EPSILON
            ):
                raise make_capture_error(
                    "CAPTURE_SYSTEM_UI",
                    "macOS 浏览器窗口布局收敛超时",
                )
            if self._has_converged_capture_frame(
                bounds.bounds_px,
                system_ui=system_ui,
                display=display,
            ):
                return

        raise make_capture_error(
            "CAPTURE_SYSTEM_UI",
            "macOS 浏览器窗口布局在有限重试内未收敛",
        )

    def _has_converged_capture_frame(
        self,
        bounds: DisplayBounds,
        *,
        system_ui: NativeSystemUISample,
        display: DisplayBounds,
    ) -> bool:
        return safe_capture_window_has_required_coverage(
            bounds,
            menu_bar=system_ui.menu_bar_bounds_px,
            dock=system_ui.dock_bounds_px,
            display=display,
        )

    def _layout_convergence_time(self) -> float:
        value = self._monotonic_clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("monotonic clock returned invalid data")
        return float(value)

    def _foreground_window(self) -> BrowserWindowIdentity:
        identity = self._require_current().bound.identity
        focused = self._stage(
            "CAPTURE_FOREGROUND",
            "macOS 前台窗口身份不可用",
            self._bridge.focused_window,
        )
        if (
            not isinstance(focused, NativeWindowSample)
            or focused.process_id != identity.process_id
            or str(focused.window_id) != identity.window_handle
        ):
            self._current = None
            raise make_capture_error(
                "CAPTURE_FOREGROUND",
                "macOS 前台窗口不是当前绑定窗口",
            )
        return identity

    def _injected_honor_reader_factory(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> SemanticStateReader:
        """Backward-compatible entry point for the original HONOR bundle."""

        if task.channel is WebsiteChannel.OFFICIAL:
            return self._injected_live_official_reader_factory(
                task,
                page,
                state,
            )
        return self._injected_marketplace_reader_factory(task, page, state)

    def _injected_marketplace_reader_factory(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> SemanticStateReader:
        registry = self._adapter_registry
        if registry is None:
            raise ValueError(
                "Marketplace verified-state reader registry is required"
            )
        adapter = registry.adapter_for(task.brand, task.channel)
        self._prepared_adapters[(task.brand, task.channel)] = adapter
        spec = getattr(adapter, "spec", None)
        if (
            not isinstance(spec, SiteSpec)
            or task.brand not in _MARKETPLACE_CAPTURE_BRANDS
            or task.channel not in (WebsiteChannel.JD, WebsiteChannel.TMALL)
            or spec.brand != task.brand
            or spec.channel is not task.channel
            or getattr(adapter, "channel", None) is not task.channel
        ):
            raise ValueError(
                "Marketplace verified-state reader adapter does not match task"
            )
        try:
            spec.validate_approved()
        except ValueError:
            raise ValueError(
                "Marketplace verified-state reader adapter is not approved"
            ) from None
        capture_view_preparer = getattr(adapter, "prepare_capture_view", None)
        prepared = False
        try:
            if callable(capture_view_preparer):
                try:
                    capture_view_preparer(task, page, state)
                except CaptureViewGeometryError as error:
                    if not (
                        task.channel is WebsiteChannel.JD
                        and error.safe_stage == "结果区域定位"
                        and (
                            state.outcome is BusinessOutcome.PRICE_FOUND
                            or (
                                state.outcome is BusinessOutcome.NO_MODEL
                                and self._uses_darwin_beta_visual_review()
                            )
                        )
                    ):
                        raise self._capture_view_layout_error(
                            error,
                        ) from None
                except LayoutRecognitionError as error:
                    raise self._capture_view_semantic_error(
                        error,
                    ) from None
                prepared = True
            reader_builder = getattr(adapter, "verified_state_reader", None)
            if not callable(reader_builder):
                raise ValueError(
                    "Marketplace verified-state reader is unavailable for channel"
                )
            return reader_builder(task, page, state)
        except BaseException:
            self._prepared_adapters.pop((task.brand, task.channel), None)
            restorer = getattr(adapter, "restore_capture_view", None)
            if prepared and callable(restorer):
                try:
                    restorer(task, page, state)
                except Exception:
                    pass
            raise

    def _injected_live_official_reader_factory(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> SemanticStateReader:
        registry = self._adapter_registry
        if registry is None:
            raise ValueError(
                "Live official verified-state reader registry is required"
            )
        adapter = registry.adapter_for(task.brand, task.channel)
        spec = getattr(adapter, "spec", None)
        if not isinstance(spec, SiteSpec):
            raise ValueError(
                "Live official verified-state reader adapter has invalid "
                "spec"
            )
        try:
            spec.validate_approved()
        except ValueError:
            raise ValueError(
                "Live official verified-state reader adapter has invalid "
                "spec"
            ) from None
        if (
            task.brand not in _LIVE_OFFICIAL_CAPTURE_BRANDS
            or task.channel is not WebsiteChannel.OFFICIAL
            or spec.brand != task.brand
            or spec.channel is not WebsiteChannel.OFFICIAL
            or getattr(adapter, "channel", None)
            is not WebsiteChannel.OFFICIAL
        ):
            raise ValueError(
                "Live official verified-state reader adapter does not "
                "match task"
            )

        self._prepared_adapters[(task.brand, task.channel)] = adapter
        capture_view_preparer = getattr(adapter, "prepare_capture_view", None)
        prepared = False
        try:
            if callable(capture_view_preparer):
                try:
                    capture_view_preparer(task, page, state)
                except CaptureViewGeometryError as error:
                    raise self._capture_view_layout_error(error) from None
                except LayoutRecognitionError as error:
                    raise self._capture_view_semantic_error(error) from None
                prepared = True
            reader_builder = getattr(adapter, "verified_state_reader", None)
            if not callable(reader_builder):
                raise ValueError(
                    "Live official verified-state reader is unavailable"
                )
            reader = reader_builder(task, page, state)
            if not callable(reader):
                raise ValueError(
                    "Live official verified-state reader must be callable"
                )
            return reader
        except BaseException:
            self._prepared_adapters.pop((task.brand, task.channel), None)
            restorer = getattr(adapter, "restore_capture_view", None)
            if prepared and callable(restorer):
                try:
                    restorer(task, page, state)
                except Exception:
                    pass
            raise

    def _prepared_capture_view_restorer(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> Callable[[], None] | None:
        adapter = self._prepared_adapters.get((task.brand, task.channel))
        restorer = getattr(adapter, "restore_capture_view", None)
        if not callable(restorer):
            return None
        return lambda: restorer(task, page, state)

    def _capture_rectangles_for_current_view(
        self,
        task: WebsiteTask,
        page: Any,
        state: VerifiedSemanticState,
    ) -> tuple[CssRect, ...] | None:
        registry = self._adapter_registry
        if registry is None:
            return None
        adapter = self._prepared_adapters.pop(
            (task.brand, task.channel),
            None,
        )
        if adapter is None:
            adapter = registry.adapter_for(task.brand, task.channel)
        if (
            self._uses_darwin_beta_visual_review()
            and task.channel is WebsiteChannel.JD
            and state.outcome is not BusinessOutcome.NO_MODEL
        ):
            return None
        reader = getattr(adapter, "capture_rectangles_for_capture", None)
        if not callable(reader):
            return None
        try:
            rectangles = reader(task, page, state)
        except CaptureViewGeometryError:
            if not (
                self._uses_darwin_beta_visual_review()
                and task.channel is WebsiteChannel.JD
                and state.outcome is BusinessOutcome.NO_MODEL
                and state.css_rectangles
            ):
                raise
            # JD occasionally finishes the verified search but cannot perform
            # its optional final card alignment.  Prefer live final geometry;
            # retain the already verified observation geometry only as the
            # visual-review fallback so a decorative frame never suppresses
            # the underlying evidence screenshot.
            rectangles = state.css_rectangles
        if not isinstance(rectangles, tuple | list) or not all(
            isinstance(rectangle, CssRect)
            for rectangle in rectangles
        ):
            raise ValueError(
                "capture rectangles reader must return CssRect values"
            )
        roles = tuple(rectangle.role for rectangle in rectangles)
        if state.outcome is BusinessOutcome.NO_MODEL and roles not in {
            ("search_keyword", "result_region"),
            ("result_region",),
        }:
            raise ValueError("no-model capture rectangles have invalid roles")
        return tuple(rectangles)

    @staticmethod
    def _install_no_model_proof_frame(
        page: Any,
        rectangles: tuple[CssRect, ...],
    ) -> Callable[[], None]:
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            raise ValueError("page does not support DOM proof framing")
        left = min(rectangle.x for rectangle in rectangles)
        top = min(rectangle.y for rectangle in rectangles)
        right = max(
            rectangle.x + rectangle.width for rectangle in rectangles
        )
        bottom = max(
            rectangle.y + rectangle.height for rectangle in rectangles
        )
        evaluate(
            _INSTALL_NO_MODEL_PROOF_FRAME,
            {
                "left": float(left),
                "top": float(top),
                "width": float(right - left),
                "height": float(bottom - top),
            },
        )
        return lambda: evaluate(_REMOVE_NO_MODEL_PROOF_FRAME)

    @staticmethod
    def _compose_capture_view_restorers(
        first: Callable[[], None],
        second: Callable[[], None] | None,
    ) -> Callable[[], None]:
        def restore() -> None:
            try:
                first()
            finally:
                if second is not None:
                    second()

        return restore

    def _capture_view_stage(
        self,
        operation: Callable[[], _T],
        *,
        environment_message: str,
    ) -> _T:
        """Classify only capture-view layout failures as retryable geometry."""

        try:
            return operation()
        except (KeyboardInterrupt, SystemExit):
            self._current = None
            raise
        except LoginRequired:
            self._current = None
            raise
        except CaptureViewGeometryError as error:
            self._current = None
            raise self._capture_view_layout_error(
                error,
            ) from None
        except LayoutRecognitionError as error:
            self._current = None
            raise self._capture_view_semantic_error(
                error,
            ) from None
        except EvidenceCaptureError as error:
            self._current = None
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message=environment_message,
            ) from None
        except Exception:
            self._current = None
            raise make_capture_error(
                "CAPTURE_ENVIRONMENT",
                environment_message,
            ) from None

    @staticmethod
    def _capture_view_layout_error(
        error: CaptureViewGeometryError,
    ) -> EvidenceCaptureError:
        return make_capture_error(
            "CAPTURE_GEOMETRY",
            _CAPTURE_VIEW_GEOMETRY_MESSAGES[error.safe_stage],
        )

    @staticmethod
    def _capture_view_semantic_error(
        error: LayoutRecognitionError,
    ) -> EvidenceCaptureError:
        message = str(error).lower()
        if "url" in message:
            semantic_stage = "URL复核"
        elif "store" in message or "seller" in message:
            semantic_stage = "店铺复核"
        elif (
            "exact product" in message
            or "no-model evidence" in message
            or "no-model result" in message
        ):
            semantic_stage = "商品结果复核"
        else:
            semantic_stage = "页面语义复核"
        return make_capture_error(
            "CAPTURE_ENVIRONMENT",
            _CAPTURE_VIEW_SEMANTIC_MESSAGES[semantic_stage],
        )

    def _stage(
        self,
        code: str,
        message: str,
        operation: Callable[[], _T],
    ) -> _T:
        try:
            return operation()
        except (KeyboardInterrupt, SystemExit):
            self._current = None
            raise
        except LoginRequired:
            self._current = None
            raise
        except EvidenceCaptureError as error:
            self._current = None
            if (
                error.code in {"CAPTURE_GEOMETRY", "CAPTURE_ENVIRONMENT"}
                and error.message in _SAFE_CAPTURE_VIEW_MESSAGES
            ):
                raise
            raise revalidate_capture_error(
                error,
                fallback_code="CAPTURE_ENVIRONMENT",
                safe_message=message,
            ) from None
        except Exception:
            self._current = None
            raise make_capture_error(code, message) from None

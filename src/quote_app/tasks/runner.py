from __future__ import annotations

import hashlib
import math
import platform as host_platform
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceState,
    MacCapturePolicy,
    validate_mac_capture_policy,
)
from quote_app.evidence.validation import read_validated_evidence
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureContext,
    CaptureRequest,
    PlatformEvidenceCapture,
)
from quote_app.evidence.quality import SemanticHashProbe
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.protocol import (
    AdapterObservation,
    ResumableSiteObservationAdapter,
    SiteObservationAdapter,
)
from quote_app.sites.catalog import site_session_family
from quote_app.sites.registry import AdapterRegistry
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.repository import (
    AttemptToken,
    RepositoryError,
    SQLiteTaskRepository,
)
from quote_app.tasks.retry import (
    LoginRequired,
    NonRetryableTechnicalError,
    RetryPolicy,
    SchedulerControl,
    TechnicalError,
    classify_attempt_error,
)
from quote_app.tasks.scheduler import (
    BrowserTaskScheduler,
    EventDeliveryError,
    EventSink,
    ManualLoginCallback,
)

_OUTCOME_STATES = {
    BusinessOutcome.PRICE_FOUND: EvidenceState.NORMAL,
    BusinessOutcome.NO_MODEL: EvidenceState.NO_MODEL,
    BusinessOutcome.CAPACITY_UNAVAILABLE: EvidenceState.CAPACITY_UNAVAILABLE,
    BusinessOutcome.COLOR_UNAVAILABLE: EvidenceState.COLOR_UNAVAILABLE,
    BusinessOutcome.SOLD_OUT: EvidenceState.SOLD_OUT,
}
_OUTCOME_ROLES = {
    BusinessOutcome.PRICE_FOUND: frozenset({()}),
    BusinessOutcome.NO_MODEL: frozenset({("search_keyword", "result_region")}),
    BusinessOutcome.CAPACITY_UNAVAILABLE: frozenset(
        {("capacity",), ("title", "capacity_group")}
    ),
    BusinessOutcome.COLOR_UNAVAILABLE: frozenset(
        {("color",), ("title", "color_group")}
    ),
    BusinessOutcome.SOLD_OUT: frozenset({("stock_status",)}),
}
_AUTOMATION_PAGE_KEY = "quotation-automation"
_CHANNEL_EXECUTION_PRIORITY = {
    WebsiteChannel.OFFICIAL: 0,
    WebsiteChannel.TMALL: 1,
    WebsiteChannel.JD: 2,
}
_CAPTURE_RETRY_CODES = frozenset(
    {
        "CAPTURE_FOREGROUND",
        "CAPTURE_UNSTABLE",
        "CAPTURE_OBSCURED",
        "CAPTURE_GEOMETRY",
        "CAPTURE_FAILED",
    }
)


class BrowserPageSession(Protocol):
    def page_for(self, site_family: str) -> Any: ...


class FixtureAdapter(Protocol):
    def __call__(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> FixtureObservation: ...


DiagnosticCapture = Callable[[WebsiteTask, BaseException, Path], Path | None]
SiteFamilyResolver = Callable[[WebsiteTask], str]
CaptureContextProvider = Callable[
    [WebsiteTask, Any, VerifiedSemanticState],
    CaptureContext,
]


@dataclass(frozen=True, slots=True)
class FixtureObservation:
    """One adapter result before the platform evidence capture boundary."""

    outcome: BusinessOutcome
    price: Decimal | None
    url: str
    css_rectangles: tuple[CssRect, ...]
    expected_roles: tuple[str, ...]
    expected_window: BrowserWindowIdentity
    stability_probe: SemanticHashProbe

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, BusinessOutcome):
            raise ValueError("outcome must be a BusinessOutcome")
        if self.price is not None and not isinstance(self.price, Decimal):
            raise ValueError("price must be a Decimal")
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must not be blank")
        if not isinstance(self.css_rectangles, tuple | list) or not all(
            isinstance(rectangle, CssRect)
            for rectangle in self.css_rectangles
        ):
            raise ValueError("css_rectangles must contain CssRect values")
        if not isinstance(self.expected_roles, tuple | list) or not all(
            isinstance(role, str) and role.strip()
            for role in self.expected_roles
        ):
            raise ValueError("expected_roles must contain nonblank strings")
        if not isinstance(self.expected_window, BrowserWindowIdentity):
            raise ValueError(
                "expected_window must be a BrowserWindowIdentity"
            )
        if not callable(getattr(self.stability_probe, "semantic_hash", None)):
            raise ValueError("stability_probe must provide semantic_hash")
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(
            self,
            "css_rectangles",
            tuple(self.css_rectangles),
        )
        object.__setattr__(
            self,
            "expected_roles",
            tuple(role.strip() for role in self.expected_roles),
        )
        actual_roles = tuple(
            rectangle.role for rectangle in self.css_rectangles
        )
        allowed_roles = _OUTCOME_ROLES[self.outcome]
        if (
            self.expected_roles != actual_roles
            or actual_roles not in allowed_roles
        ):
            raise ValueError(
                "fixture observation roles do not match the business outcome"
            )
        if self.outcome is BusinessOutcome.PRICE_FOUND:
            if (
                self.price is None
                or not self.price.is_finite()
                or self.price < 0
            ):
                raise ValueError("price-found observation requires a price")
        elif self.price is not None:
            raise ValueError("legal no observation cannot carry a price")


class WebsiteTaskRunner:
    """Serial, resumable boundary from adapter observation to persisted result."""

    def __init__(
        self,
        *,
        repository: SQLiteTaskRepository,
        run_id: str,
        browser_session: BrowserPageSession,
        evidence_capture: PlatformEvidenceCapture,
        evidence_dir: Path,
        adapter: FixtureAdapter | None = None,
        adapter_registry: AdapterRegistry | None = None,
        capture_context_provider: CaptureContextProvider | None = None,
        diagnostic_capture: DiagnosticCapture | None = None,
        retry_policy: RetryPolicy | None = None,
        control: SchedulerControl | None = None,
        event_sink: EventSink | None = None,
        manual_login_callback: ManualLoginCallback | None = None,
        site_family_resolver: SiteFamilyResolver | None = None,
        minimum_stability_interval_seconds: float = 0.15,
        capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT,
        stop_after_brand_issue: bool = False,
    ) -> None:
        if not isinstance(repository, SQLiteTaskRepository):
            raise ValueError("repository must be SQLiteTaskRepository")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must not be blank")
        if not callable(getattr(browser_session, "page_for", None)):
            raise ValueError("browser_session must provide page_for")
        if (adapter is None) == (adapter_registry is None):
            raise ValueError("exactly one of adapter or adapter_registry is required")
        if adapter is not None:
            if not callable(adapter):
                raise ValueError("adapter must be callable")
            if capture_context_provider is not None:
                raise ValueError(
                    "capture_context_provider is only valid with adapter_registry"
                )
        else:
            if not isinstance(adapter_registry, AdapterRegistry):
                raise ValueError("adapter_registry must be an AdapterRegistry")
            if not callable(capture_context_provider):
                raise ValueError(
                    "adapter_registry requires a callable capture_context_provider"
                )
        if not callable(getattr(evidence_capture, "capture", None)):
            raise ValueError("evidence_capture must provide capture")
        validate_mac_capture_policy(
            capture_acceptance_policy,
            platform_name=host_platform.system(),
        )
        if not isinstance(evidence_dir, Path):
            raise ValueError("evidence_dir must be a Path")
        if type(stop_after_brand_issue) is not bool:
            raise ValueError("stop_after_brand_issue must be a bool")
        if diagnostic_capture is not None and not callable(diagnostic_capture):
            raise ValueError("diagnostic_capture must be callable")
        if manual_login_callback is not None and not callable(
            manual_login_callback
        ):
            raise ValueError("manual_login_callback must be callable")
        if site_family_resolver is not None and not callable(
            site_family_resolver
        ):
            raise ValueError("site_family_resolver must be callable")
        if (
            not isinstance(minimum_stability_interval_seconds, int | float)
            or isinstance(minimum_stability_interval_seconds, bool)
            or not math.isfinite(minimum_stability_interval_seconds)
            or minimum_stability_interval_seconds <= 0
        ):
            raise ValueError(
                "minimum_stability_interval_seconds must be positive"
            )
        self.repository = repository
        self.run_id = run_id.strip()
        self.browser_session = browser_session
        self.adapter = adapter
        self.adapter_registry = adapter_registry
        self.capture_context_provider = capture_context_provider
        self.evidence_capture = evidence_capture
        self.capture_acceptance_policy = capture_acceptance_policy
        self.evidence_dir = evidence_dir.expanduser().resolve()
        self.diagnostic_capture = diagnostic_capture
        self.site_family_resolver = (
            site_family_resolver or _default_site_family
        )
        self.manual_login_callback = manual_login_callback
        self.minimum_stability_interval_seconds = float(
            minimum_stability_interval_seconds
        )
        self._page_lock = threading.Lock()
        self.scheduler = BrowserTaskScheduler(
            repository,
            self.run_id,
            self._attempt,
            retry_policy=retry_policy,
            control=control,
            event_sink=event_sink,
            manual_login_callback=self._prepare_manual_login,
            task_sort_key=task_sort_key,
            task_site_resolver=self.site_family_resolver,
            stop_after_brand_issue=stop_after_brand_issue,
        )

    @property
    def event_errors(self) -> tuple[EventDeliveryError, ...]:
        return tuple(self.scheduler.event_errors)

    def run(
        self,
        tasks: Sequence[WebsiteTask],
    ) -> tuple[WebsiteResult, ...]:
        ordered = _validated_ordered_tasks(tasks, run_id=self.run_id)
        for task in ordered:
            self._ensure_task(task)
        self.repository.audit_evidence(self.run_id)
        self.scheduler.run_until_idle()
        results: list[WebsiteResult] = []
        for task in ordered:
            result = self.repository.load_result(task.task_id)
            if result is not None:
                results.append(result)
        return tuple(results)

    def _ensure_task(self, task: WebsiteTask) -> None:
        try:
            existing = self.repository.load_task(task.task_id)
        except RepositoryError:
            self.repository.upsert_task(task)
            return
        if existing == task:
            return
        self.repository.upsert_task(
            task,
            generation=self.repository.task_generation(task.task_id) + 1,
        )

    def _attempt(
        self,
        task: WebsiteTask,
        token: AttemptToken,
        control: SchedulerControl,
    ) -> WebsiteResult:
        del control
        file_stem = safe_task_file_stem(task.task_id)
        destination = self.evidence_dir / (
            f"{file_stem}.g{token.generation}.a{token.attempt_number}.png"
        )
        with self._page_lock:
            site_family: str | None = None
            try:
                site_family = self.site_family_resolver(task)
                if not isinstance(site_family, str) or not site_family.strip():
                    raise ValueError("site family resolver returned invalid data")
                self._close_unassigned_pages()
                page = self._automation_page()
                observation: FixtureObservation | AdapterObservation
                saved_checkpoint = self.repository.load_observation(task.task_id)
                if self.adapter_registry is None:
                    if saved_checkpoint is not None:
                        observation = self._resume_fixture_observation(
                            task,
                            page,
                            saved_checkpoint,
                        )
                    else:
                        observation = self._fixture_observation(task, page)
                else:
                    observation = (
                        self._resume_site_observation(
                            task,
                            page,
                            saved_checkpoint,
                        )
                        if saved_checkpoint is not None
                        else self._site_observation(task, page)
                    )
                if saved_checkpoint is None:
                    checkpoint = WebsiteObservationCheckpoint(
                        task_id=task.task_id,
                        outcome=observation.outcome,
                        price=observation.price,
                        url=observation.url,
                        observed_at=datetime.now(timezone.utc),
                    )
                    self.repository.save_observation(checkpoint, token=token)
                    self.scheduler.publish_observation(task, checkpoint)
                evidence = self._capture_current_observation(
                    task,
                    page,
                    observation,
                    destination,
                )
                return WebsiteResult(
                    task_id=task.task_id,
                    state=TaskState.SUCCEEDED,
                    outcome=observation.outcome,
                    price=observation.price,
                    url=observation.url,
                    evidence=evidence,
                    diagnostic_path=None,
                    error_code=None,
                    error_message=None,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except LoginRequired as error:
                _remove_partial_formal(destination)
                if site_family is None or error.site != site_family:
                    classified = classify_attempt_error(
                        ValueError("login site does not match task site family")
                    )
                    if not isinstance(classified, TechnicalError):
                        raise AssertionError(
                            "login site mismatch was not technical"
                        )
                    diagnostic_path = self._capture_diagnostic(
                        task,
                        error,
                        token,
                    )
                    return WebsiteResult(
                        task_id=task.task_id,
                        state=TaskState.TECHNICAL_FAILURE,
                        outcome=None,
                        price=None,
                        url=None,
                        evidence=None,
                        diagnostic_path=diagnostic_path,
                        error_code=classified.code,
                        error_message=classified.message,
                    )
                raise
            except Exception as error:
                _remove_partial_formal(destination)
                classified = classify_attempt_error(error)
                if isinstance(classified, LoginRequired):
                    raise classified
                if not isinstance(classified, TechnicalError):
                    raise AssertionError(
                        "attempt classifier returned an invalid error"
                    )
                diagnostic_path = self._capture_diagnostic(
                    task,
                    error,
                    token,
                )
                return WebsiteResult(
                    task_id=task.task_id,
                    state=TaskState.TECHNICAL_FAILURE,
                    outcome=None,
                    price=None,
                    url=None,
                    evidence=None,
                    diagnostic_path=diagnostic_path,
                    error_code=classified.code,
                    error_message=classified.message,
                )

    def _fixture_observation(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> FixtureObservation:
        if self.adapter is None:
            raise AssertionError("legacy adapter is unavailable")
        observation = self.adapter(task, page)
        if not isinstance(observation, FixtureObservation):
            raise ValueError("adapter returned an invalid observation")
        return observation

    def _resume_fixture_observation(
        self,
        task: WebsiteTask,
        page: Any,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> FixtureObservation:
        resume = getattr(self.adapter, "resume", None)
        if not callable(resume):
            raise NonRetryableTechnicalError(
                "RECOVERY_UNSUPPORTED",
                "已保存价格，但当前适配器无法安全恢复截图；旧价格已保留",
            )
        observation = resume(task, page, checkpoint)
        if not isinstance(observation, FixtureObservation):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "恢复页面未返回可验证的产品状态；旧价格已保留",
            )
        if (
            observation.outcome is not checkpoint.outcome
            or observation.price != checkpoint.price
            or observation.url != checkpoint.url
        ):
            raise NonRetryableTechnicalError(
                "RECOVERY_CHECKPOINT_MISMATCH",
                "恢复页面与已保存价格或产品链接不一致；旧价格已保留",
            )
        return observation

    def _resume_site_observation(
        self,
        task: WebsiteTask,
        page: Any,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        if self.adapter_registry is None:
            raise AssertionError("registry observation dependencies are unavailable")
        adapter = self.adapter_registry.adapter_for(task.brand, task.channel)
        if not isinstance(adapter, ResumableSiteObservationAdapter):
            raise NonRetryableTechnicalError(
                "RECOVERY_UNSUPPORTED",
                "已保存价格，但当前网站无法安全恢复截图；旧价格已保留",
            )
        try:
            observation = adapter.resume(task, page, checkpoint)
        except LoginRequired:
            raise
        except NonRetryableTechnicalError:
            raise
        except Exception as error:
            raise NonRetryableTechnicalError(
                "RECOVERY_REVALIDATION_FAILED",
                "恢复页面未通过产品、配置、店铺和价格复核；旧价格已保留",
            ) from error
        if not isinstance(observation, AdapterObservation):
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "恢复页面未返回可验证的产品状态；旧价格已保留",
            )
        if (
            observation.outcome is not checkpoint.outcome
            or observation.price != checkpoint.price
            or observation.url != checkpoint.url
        ):
            raise NonRetryableTechnicalError(
                "RECOVERY_CHECKPOINT_MISMATCH",
                "恢复页面与已保存价格或产品链接不一致；旧价格已保留",
            )
        return observation

    def _site_observation(
        self,
        task: WebsiteTask,
        page: Any,
    ) -> AdapterObservation:
        if self.adapter_registry is None:
            raise AssertionError("registry observation dependencies are unavailable")
        adapter = self.adapter_registry.adapter_for(task.brand, task.channel)
        if not isinstance(adapter, SiteObservationAdapter):
            raise ValueError("registry adapter must provide observe")
        observation = adapter.observe(task, page)
        if not isinstance(observation, AdapterObservation):
            raise ValueError("registry adapter returned an invalid observation")
        observation = AdapterObservation(
            outcome=observation.outcome,
            price=observation.price,
            url=observation.url,
            css_rectangles=observation.css_rectangles,
            semantic_state=observation.semantic_state,
        )
        return observation

    def _capture_current_observation(
        self,
        task: WebsiteTask,
        page: Any,
        observation: FixtureObservation | AdapterObservation,
        destination: Path,
    ) -> EvidenceRecord:
        for capture_attempt in range(3):
            try:
                request = self._capture_request(
                    task,
                    page,
                    observation,
                    destination,
                )
                evidence = self.evidence_capture.capture(request)
                _require_validated_formal_evidence(
                    evidence,
                    request,
                    capture_acceptance_policy=self.capture_acceptance_policy,
                )
                return evidence
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                classified = classify_attempt_error(error)
                if (
                    capture_attempt == 2
                    or not isinstance(classified, TechnicalError)
                    or classified.code not in _CAPTURE_RETRY_CODES
                ):
                    raise
                wait_for_timeout = getattr(page, "wait_for_timeout", None)
                if not callable(wait_for_timeout):
                    raise ValueError("browser page must provide wait_for_timeout")
                wait_for_timeout(500)
        raise AssertionError("capture retry loop exhausted without a result")

    def _capture_request(
        self,
        task: WebsiteTask,
        page: Any,
        observation: FixtureObservation | AdapterObservation,
        destination: Path,
    ) -> CaptureRequest:
        if isinstance(observation, FixtureObservation):
            return CaptureRequest(
                destination=destination,
                state=_OUTCOME_STATES[observation.outcome],
                css_rectangles=observation.css_rectangles,
                expected_roles=observation.expected_roles,
                expected_window=observation.expected_window,
                stability_probe=observation.stability_probe,
                minimum_stability_interval_seconds=(
                    self.minimum_stability_interval_seconds
                ),
            )
        provider = self.capture_context_provider
        if provider is None:
            raise AssertionError("capture context provider is unavailable")
        context = provider(task, page, observation.semantic_state)
        if not isinstance(context, CaptureContext):
            raise ValueError("capture context provider returned invalid data")
        rectangles = (
            observation.css_rectangles
            if context.css_rectangles is None
            else context.css_rectangles
        )
        return CaptureRequest(
            destination=destination,
            state=_OUTCOME_STATES[observation.outcome],
            css_rectangles=rectangles,
            expected_roles=tuple(
                rectangle.role for rectangle in rectangles
            ),
            expected_window=context.expected_window,
            stability_probe=context.stability_probe,
            minimum_stability_interval_seconds=(
                self.minimum_stability_interval_seconds
            ),
        )

    def _capture_diagnostic(
        self,
        task: WebsiteTask,
        error: BaseException,
        token: AttemptToken,
    ) -> Path | None:
        if self.diagnostic_capture is None:
            return None
        diagnostics_root = self._safe_diagnostics_root()
        if diagnostics_root is None:
            return None
        path = (
            diagnostics_root
            / (
                f"{safe_task_file_stem(task.task_id)}"
                f".g{token.generation}.a{token.attempt_number}.png"
            )
        )
        try:
            captured = self.diagnostic_capture(task, error, path)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return None
        if not isinstance(captured, Path):
            return None
        verified_root = self._safe_diagnostics_root()
        if verified_root is None or verified_root != diagnostics_root:
            return None
        normalized = captured.expanduser().resolve()
        requested = path.resolve()
        if normalized != requested or not normalized.is_file():
            return None
        try:
            normalized.relative_to(diagnostics_root)
        except ValueError:
            return None
        return normalized

    def _safe_diagnostics_root(self) -> Path | None:
        evidence_root = self.evidence_dir.resolve()
        unresolved = evidence_root / "diagnostics"
        if unresolved.is_symlink():
            return None
        resolved = unresolved.resolve()
        try:
            resolved.relative_to(evidence_root)
        except ValueError:
            return None
        return resolved

    def _prepare_manual_login(self, site: str) -> None:
        with self._page_lock:
            page = self._automation_page()
            bring_to_front = getattr(page, "bring_to_front", None)
            if callable(bring_to_front):
                bring_to_front()
            if self.manual_login_callback is not None:
                self.manual_login_callback(site)

    def _automation_page(self) -> Any:
        getter = getattr(self.browser_session, "automation_page", None)
        if callable(getter):
            return getter()
        return self.browser_session.page_for(_AUTOMATION_PAGE_KEY)

    def _close_unassigned_pages(self) -> None:
        closer = getattr(self.browser_session, "close_unassigned_pages", None)
        if callable(closer):
            closer()


def task_sort_key(
    task: WebsiteTask,
) -> tuple[int, str, int]:
    return (
        _CHANNEL_EXECUTION_PRIORITY[task.channel],
        task.brand,
        task.output_row_number,
    )


def _default_site_family(task: WebsiteTask) -> str:
    return site_session_family(task.brand, task.channel)


def safe_task_file_stem(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must not be blank")
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return f"task-{digest}"


def _validated_ordered_tasks(
    tasks: Sequence[WebsiteTask],
    *,
    run_id: str,
) -> tuple[WebsiteTask, ...]:
    if not isinstance(tasks, Sequence) or isinstance(tasks, str | bytes):
        raise ValueError("tasks must be a sequence")
    if not all(isinstance(task, WebsiteTask) for task in tasks):
        raise ValueError("tasks must contain WebsiteTask values")
    if any(task.run_id != run_id for task in tasks):
        raise ValueError("all tasks must belong to the runner run")
    task_ids = tuple(task.task_id for task in tasks)
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task ids must be unique")
    return tuple(sorted(tasks, key=task_sort_key))


def _require_validated_formal_evidence(
    evidence: object,
    request: CaptureRequest,
    *,
    capture_acceptance_policy: MacCapturePolicy,
) -> None:
    if not isinstance(evidence, EvidenceRecord):
        raise ValueError("capture returned invalid evidence")
    if (
        evidence.state is not request.state
        or evidence.path != request.destination
    ):
        raise ValueError("capture did not publish validated formal evidence")
    audit = read_validated_evidence(
        evidence,
        capture_acceptance_policy=capture_acceptance_policy,
    )
    if not audit.is_valid:
        raise ValueError("capture did not publish validated formal evidence")


def _remove_partial_formal(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

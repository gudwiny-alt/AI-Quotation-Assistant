from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.models import EvidenceRecord, EvidenceState
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureRequest,
    PlatformEvidenceCapture,
)
from quote_app.evidence.quality import SemanticHashProbe
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.repository import (
    AttemptToken,
    RepositoryError,
    SQLiteTaskRepository,
)
from quote_app.tasks.retry import (
    LoginRequired,
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
    BusinessOutcome.PRICE_FOUND: (),
    BusinessOutcome.NO_MODEL: ("search_keyword", "result_region"),
    BusinessOutcome.CAPACITY_UNAVAILABLE: ("capacity",),
    BusinessOutcome.COLOR_UNAVAILABLE: ("color",),
    BusinessOutcome.SOLD_OUT: ("stock_status",),
}


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
        expected_roles = _OUTCOME_ROLES[self.outcome]
        actual_roles = tuple(
            rectangle.role for rectangle in self.css_rectangles
        )
        if self.expected_roles != expected_roles or actual_roles != expected_roles:
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
        adapter: FixtureAdapter,
        evidence_capture: PlatformEvidenceCapture,
        evidence_dir: Path,
        diagnostic_capture: DiagnosticCapture | None = None,
        retry_policy: RetryPolicy | None = None,
        control: SchedulerControl | None = None,
        event_sink: EventSink | None = None,
        manual_login_callback: ManualLoginCallback | None = None,
        site_family_resolver: SiteFamilyResolver | None = None,
        minimum_stability_interval_seconds: float = 0.15,
    ) -> None:
        if not isinstance(repository, SQLiteTaskRepository):
            raise ValueError("repository must be SQLiteTaskRepository")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must not be blank")
        if not callable(getattr(browser_session, "page_for", None)):
            raise ValueError("browser_session must provide page_for")
        if not callable(adapter):
            raise ValueError("adapter must be callable")
        if not callable(getattr(evidence_capture, "capture", None)):
            raise ValueError("evidence_capture must provide capture")
        if not isinstance(evidence_dir, Path):
            raise ValueError("evidence_dir must be a Path")
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
        self.evidence_capture = evidence_capture
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
                page = self.browser_session.page_for(site_family.strip())
                observation = self.adapter(task, page)
                if not isinstance(observation, FixtureObservation):
                    raise ValueError(
                        "adapter returned an invalid observation"
                    )
                request = CaptureRequest(
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
                evidence = self.evidence_capture.capture(request)
                _require_validated_formal_evidence(evidence, request)
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
            page = self.browser_session.page_for(site)
            bring_to_front = getattr(page, "bring_to_front", None)
            if callable(bring_to_front):
                bring_to_front()
            if self.manual_login_callback is not None:
                self.manual_login_callback(site)


def task_sort_key(
    task: WebsiteTask,
) -> tuple[str, str, str, str, str, str, int]:
    return (
        task.brand,
        task.model_name,
        task.ram,
        task.storage,
        task.color,
        task.channel.value,
        task.output_row_number,
    )


def _default_site_family(task: WebsiteTask) -> str:
    if task.channel.value == "official":
        return f"official:{task.brand}"
    return task.channel.value


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
) -> None:
    if not isinstance(evidence, EvidenceRecord):
        raise ValueError("capture returned invalid evidence")
    if (
        not evidence.is_validated
        or evidence.state is not request.state
        or evidence.path != request.destination
        or not evidence.path.is_file()
    ):
        raise ValueError("capture did not publish validated formal evidence")
    digest = hashlib.sha256(evidence.path.read_bytes()).hexdigest()
    if digest != evidence.sha256:
        raise ValueError("formal evidence hash does not match its record")


def _remove_partial_formal(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

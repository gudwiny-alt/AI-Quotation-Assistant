"""Production composition for one local, serial website-task run."""

from __future__ import annotations

import os
import platform as host_platform
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quote_app.browser.session import PersistentBrowserSession
from quote_app.evidence.macos_runtime import MacFormalCaptureRuntime
from quote_app.evidence.models import MacCapturePolicy, validate_mac_capture_policy
from quote_app.evidence.validation import read_validated_evidence
from quote_app.sites.catalog import site_session_family
from quote_app.sites.registry import AdapterRegistry
from quote_app.browser.worker import WorkerEvent
from quote_app.tasks.models import (
    TaskState,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.repository import SQLiteTaskRepository
from quote_app.tasks.runner import WebsiteTaskRunner
from quote_app.tasks.scheduler import EventSink, ManualActionEvent

RuntimeFactory = Callable[[AdapterRegistry], MacFormalCaptureRuntime]
CheckpointSink = Callable[["WebsiteRunSnapshot"], None]


class WebsiteRunController:
    """Thread-safe handoff between a paused browser worker and the Tk UI."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiting_action: ManualActionEvent | None = None
        self._resolution: bool | None = None
        self._shutdown_requested = False

    @property
    def waiting_action(self) -> ManualActionEvent | None:
        with self._condition:
            return self._waiting_action

    def publish_manual_action(self, action: ManualActionEvent) -> None:
        if not isinstance(action, ManualActionEvent):
            raise TypeError("action must be ManualActionEvent")
        with self._condition:
            if self._waiting_action is not None:
                raise RuntimeError("已有网站任务等待人工处理")
            self._waiting_action = action
            self._resolution = False if self._shutdown_requested else None
            self._condition.notify_all()

    def request_shutdown(self) -> None:
        """Resolve current or future manual waits as cancellation."""
        with self._condition:
            self._shutdown_requested = True
            if self._waiting_action is not None:
                self._resolution = False
            self._condition.notify_all()

    def continue_current_task(self) -> None:
        self._resolve(True)

    def cancel_manual_action(self) -> bool:
        with self._condition:
            if self._waiting_action is None:
                return False
            self._resolution = False
            self._condition.notify_all()
            return True

    def wait_for_resolution(self) -> bool:
        with self._condition:
            if self._waiting_action is None:
                raise RuntimeError("当前没有等待人工处理的网站任务")
            self._condition.wait_for(lambda: self._resolution is not None)
            resolved = self._resolution
            if resolved is None:
                raise AssertionError("manual action resolution was not retained")
            self._waiting_action = None
            self._resolution = None
            return resolved

    def _resolve(self, resolution: bool) -> None:
        with self._condition:
            if self._waiting_action is None:
                raise RuntimeError("当前没有等待人工处理的网站任务")
            self._resolution = (
                False if self._shutdown_requested else resolution
            )
            self._condition.notify_all()


@dataclass(frozen=True, slots=True)
class WebsiteRunRequest:
    run_id: str
    tasks: tuple[WebsiteTask, ...]
    profile_dir: Path
    evidence_dir: Path
    database_path: Path
    controller: WebsiteRunController | None = None
    event_sink: EventSink | None = None
    checkpoint_sink: CheckpointSink | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must not be blank")
        if not isinstance(self.tasks, tuple | list) or not all(
            isinstance(task, WebsiteTask) for task in self.tasks
        ):
            raise ValueError("tasks must contain WebsiteTask values")
        tasks = tuple(self.tasks)
        if any(task.run_id != self.run_id.strip() for task in tasks):
            raise ValueError("every task must belong to the requested run")
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("tasks must not contain duplicate task IDs")
        for field_name in ("profile_dir", "evidence_dir", "database_path"):
            if not isinstance(getattr(self, field_name), Path):
                raise ValueError(f"{field_name} must be a Path")
        if self.controller is not None and not isinstance(
            self.controller, WebsiteRunController
        ):
            raise ValueError("controller must be WebsiteRunController")
        if self.event_sink is not None and not callable(self.event_sink):
            raise ValueError("event_sink must be callable")
        if self.checkpoint_sink is not None and not callable(
            self.checkpoint_sink
        ):
            raise ValueError("checkpoint_sink must be callable")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "profile_dir", self.profile_dir.expanduser().resolve())
        object.__setattr__(self, "evidence_dir", self.evidence_dir.expanduser().resolve())
        object.__setattr__(
            self,
            "database_path",
            self.database_path.expanduser().resolve(),
        )


@dataclass(frozen=True, slots=True)
class WebsiteRunSummary:
    succeeded: int
    waiting_for_login: int
    technical_failure: int
    evidence_paths: tuple[Path, ...]
    waiting_sites: tuple[str, ...] = ()
    technical_failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebsiteRunSnapshot:
    observations: tuple[WebsiteObservationCheckpoint, ...]
    results: tuple[WebsiteResult, ...]
    waiting_task_ids: frozenset[str]


def default_registry() -> AdapterRegistry:
    """Construct the one adapter registry owned by a service invocation."""
    return AdapterRegistry()


def _default_runtime_factory(registry: AdapterRegistry) -> MacFormalCaptureRuntime:
    return MacFormalCaptureRuntime(adapter_registry=registry)


def mac_visual_review_runtime_factory(
    registry: AdapterRegistry,
) -> MacFormalCaptureRuntime:
    """The only service factory that opts into Mac visual-review capture."""
    return MacFormalCaptureRuntime(
        adapter_registry=registry,
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )


def run_website_tasks(
    request: WebsiteRunRequest,
    *,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
) -> WebsiteRunSummary:
    """Run one resumable website workload in one persistent browser session."""
    if not isinstance(request, WebsiteRunRequest):
        raise ValueError("request must be a WebsiteRunRequest")
    if not callable(runtime_factory):
        raise ValueError("runtime_factory must be callable")

    repository = SQLiteTaskRepository(request.database_path)
    try:
        registry = default_registry()
        runtime = runtime_factory(registry)
        browser_session: PersistentBrowserSession
        if isinstance(runtime, MacFormalCaptureRuntime):
            launch_args = runtime.browser_launch_args()
            startup_preflight = runtime.browser_startup_preflight()
            if launch_args is not None:
                if startup_preflight is not None:
                    browser_session = PersistentBrowserSession(
                        request.profile_dir,
                        launch_args=launch_args,
                        startup_preflight=startup_preflight,
                    )
                else:
                    browser_session = PersistentBrowserSession(
                        request.profile_dir,
                        launch_args=launch_args,
                    )
            elif startup_preflight is not None:
                browser_session = PersistentBrowserSession(
                    request.profile_dir,
                    startup_preflight=startup_preflight,
                )
            else:
                browser_session = PersistentBrowserSession(request.profile_dir)
        else:
            browser_session = PersistentBrowserSession(request.profile_dir)
        with browser_session as browser:
            evidence_capture = runtime.evidence_capture()
            capture_acceptance_policy = _capture_acceptance_policy(
                evidence_capture
            )
            checkpoint_errors: list[Exception] = []
            event_sink = _event_sink_for_request(
                request,
                repository,
                checkpoint_errors=checkpoint_errors,
            )
            runner = WebsiteTaskRunner(
                repository=repository,
                run_id=request.run_id,
                browser_session=browser,
                evidence_capture=evidence_capture,
                evidence_dir=request.evidence_dir,
                adapter_registry=registry,
                capture_context_provider=runtime.capture_context_provider,
                capture_acceptance_policy=capture_acceptance_policy,
                event_sink=event_sink,
                # A technical failure is recorded per task; it must not prevent
                # the next brand from being processed in the same run.
                stop_after_brand_issue=False,
            )
            while True:
                results = runner.run(request.tasks)
                _raise_checkpoint_error(checkpoint_errors)
                controller = request.controller
                if controller is None:
                    break
                action = runner.scheduler.waiting_action
                if action is None:
                    break
                controller.publish_manual_action(action)
                if controller.wait_for_resolution():
                    runner.scheduler.continue_current_task()
                    continue
                runner.scheduler.cancel_manual_action()
                break
        return _summarize(
            request,
            repository,
            results,
            capture_acceptance_policy=capture_acceptance_policy,
        )
    finally:
        repository.close()

def _summarize(
    request: WebsiteRunRequest,
    repository: SQLiteTaskRepository,
    results: tuple[WebsiteResult, ...],
    *,
    capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT,
) -> WebsiteRunSummary:
    results_by_task = {result.task_id: result for result in results}
    states = tuple(repository.task_state(task.task_id) for task in request.tasks)
    waiting_sites = tuple(
        dict.fromkeys(
            site_session_family(task.brand, task.channel)
            for task, state in zip(request.tasks, states, strict=True)
            if state is TaskState.WAITING_FOR_LOGIN
        )
    )
    technical_failure_codes = tuple(
        result.error_code
        for task in request.tasks
        if (
            (result := results_by_task.get(task.task_id)) is not None
            and result.state is TaskState.TECHNICAL_FAILURE
            and result.error_code is not None
        )
    )
    evidence_paths: list[Path] = []
    for task in request.tasks:
        result = results_by_task.get(task.task_id)
        if result is None:
            continue
        evidence_path = _validated_evidence_path(
            result,
            capture_acceptance_policy=capture_acceptance_policy,
        )
        if evidence_path is not None:
            evidence_paths.append(evidence_path)
    return WebsiteRunSummary(
        succeeded=sum(state is TaskState.SUCCEEDED for state in states),
        waiting_for_login=sum(
            state is TaskState.WAITING_FOR_LOGIN for state in states
        ),
        technical_failure=sum(
            state is TaskState.TECHNICAL_FAILURE for state in states
        ),
        evidence_paths=tuple(evidence_paths),
        waiting_sites=waiting_sites,
        technical_failure_codes=technical_failure_codes,
    )


def _event_sink_for_request(
    request: WebsiteRunRequest,
    repository: SQLiteTaskRepository,
    *,
    checkpoint_errors: list[Exception] | None = None,
) -> EventSink | None:
    if request.event_sink is None and request.checkpoint_sink is None:
        return None
    snapshot_index = (
        _WebsiteRunSnapshotIndex(request.tasks, repository)
        if request.checkpoint_sink is not None
        else None
    )

    def sink(event: WorkerEvent) -> None:
        if request.checkpoint_sink is not None and _is_checkpoint_event(event):
            try:
                if snapshot_index is None:
                    raise AssertionError("checkpoint snapshot index is unavailable")
                request.checkpoint_sink(snapshot_index.update(event.task_id))
            except Exception as error:
                if checkpoint_errors is not None and not checkpoint_errors:
                    checkpoint_errors.append(error)
                raise
            finally:
                if request.event_sink is not None:
                    request.event_sink(event)
            return
        if request.event_sink is not None:
            request.event_sink(event)

    return sink


def _raise_checkpoint_error(checkpoint_errors: list[Exception]) -> None:
    if checkpoint_errors:
        raise checkpoint_errors[0]


class _WebsiteRunSnapshotIndex:
    """Read the workload once, then refresh only the task named by an event."""

    def __init__(
        self,
        tasks: tuple[WebsiteTask, ...],
        repository: SQLiteTaskRepository,
    ) -> None:
        self._tasks = tuple(tasks)
        self._repository = repository
        self._task_ids = frozenset(task.task_id for task in self._tasks)
        self._observations: dict[str, WebsiteObservationCheckpoint] = {}
        self._results: dict[str, WebsiteResult] = {}
        self._waiting: set[str] = set()
        self._initialized = False

    def update(self, task_id: str) -> WebsiteRunSnapshot:
        if task_id not in self._task_ids:
            raise ValueError("checkpoint event does not belong to the requested run")
        if self._initialized:
            self._refresh(task_id)
        else:
            for task in self._tasks:
                self._refresh(task.task_id)
            self._initialized = True
        return WebsiteRunSnapshot(
            observations=tuple(
                self._observations[task.task_id]
                for task in self._tasks
                if task.task_id in self._observations
            ),
            results=tuple(
                self._results[task.task_id]
                for task in self._tasks
                if task.task_id in self._results
            ),
            waiting_task_ids=frozenset(self._waiting),
        )

    def _refresh(self, task_id: str) -> None:
        observation = self._repository.load_observation(task_id)
        if observation is None:
            self._observations.pop(task_id, None)
        else:
            self._observations[task_id] = observation
        result = self._repository.load_result(task_id)
        if result is None:
            self._results.pop(task_id, None)
        else:
            self._results[task_id] = result
        if self._repository.task_state(task_id) is TaskState.WAITING_FOR_LOGIN:
            self._waiting.add(task_id)
        else:
            self._waiting.discard(task_id)


def _is_checkpoint_event(event: WorkerEvent) -> bool:
    if event.event in {"observation", "result", "waiting_for_login"}:
        return True
    if event.event != "technical_failure":
        return False
    retryable = event.data.get("retryable")
    retry_remaining = event.data.get("retry_remaining")
    return retryable is False or retry_remaining == 0


def _validated_evidence_path(
    result: WebsiteResult,
    *,
    capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT,
) -> Path | None:
    evidence = result.evidence
    if (
        result.state is not TaskState.SUCCEEDED
        or evidence is None
    ):
        return None
    audit = read_validated_evidence(
        evidence,
        open_fd=_open_evidence_fd,
        lstat_path=_lstat_evidence_path,
        capture_acceptance_policy=capture_acceptance_policy,
    )
    if not audit.is_valid:
        return None
    return evidence.path


def _open_evidence_fd(path: Path, flags: int) -> int:
    return os.open(path, flags)


def _lstat_evidence_path(path: Path) -> os.stat_result:
    return os.lstat(path)


def _capture_acceptance_policy(evidence_capture: object) -> MacCapturePolicy:
    policy = getattr(evidence_capture, "policy", MacCapturePolicy.STRICT)
    validate_mac_capture_policy(
        policy,
        platform_name=host_platform.system(),
    )
    return policy

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from quote_app.browser.worker import JsonValue, WorkerEvent
from quote_app.tasks.models import (
    TaskState,
    WebsiteObservationCheckpoint,
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
    NonRetryableTechnicalError,
    RETRYABLE_ERROR_CODES,
    RetryPolicy,
    RetryableTechnicalError,
    SchedulerControl,
    SecurityVerificationRequired,
    TechnicalError,
    classify_attempt_error,
    consumes_technical_budget,
    credential_free_error_message,
)


class AttemptCallback(Protocol):
    def __call__(
        self,
        task: WebsiteTask,
        token: AttemptToken,
        control: SchedulerControl,
    ) -> WebsiteResult: ...


EventSink = Callable[[WorkerEvent], None]
ManualLoginCallback = Callable[[str], None]
TaskSortKey = Callable[
    [WebsiteTask],
    tuple[str | int, ...],
]
TaskSiteResolver = Callable[[WebsiteTask], str]


@dataclass(frozen=True, slots=True)
class EventDeliveryError:
    event: str
    task_id: str
    error_code: str = "EVENT_SINK_ERROR"


@dataclass(frozen=True, slots=True)
class ManualActionEvent:
    """One browser task paused on its visible login or verification page."""

    task: WebsiteTask
    token: AttemptToken
    site: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.task, WebsiteTask):
            raise ValueError("manual action task must be WebsiteTask")
        if (
            not isinstance(self.token, AttemptToken)
            or self.token.task_id != self.task.task_id
        ):
            raise ValueError("manual action token must belong to task")
        for field_name in ("site", "reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"manual action {field_name} must be nonblank")
            object.__setattr__(self, field_name, value.strip())


class ManualLoginError(RuntimeError):
    """A stable public error for manual-login foreground preparation."""


class BrowserTaskScheduler:
    """Serial scheduler around the crash-safe repository attempt boundary."""

    _MANUAL_VERIFICATION_EXHAUSTED_CODE = (
        "MANUAL_VERIFICATION_RETRIES_EXHAUSTED"
    )

    def __init__(
        self,
        repository: SQLiteTaskRepository,
        run_id: str,
        attempt_callback: AttemptCallback,
        *,
        retry_policy: RetryPolicy | None = None,
        control: SchedulerControl | None = None,
        event_sink: EventSink | None = None,
        manual_login_callback: ManualLoginCallback | None = None,
        task_sort_key: TaskSortKey | None = None,
        task_site_resolver: TaskSiteResolver | None = None,
        stop_after_brand_issue: bool = False,
        max_manual_verification_retries: int = 2,
    ) -> None:
        if not isinstance(repository, SQLiteTaskRepository):
            raise ValueError("repository must be a SQLiteTaskRepository")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must not be blank")
        if not callable(attempt_callback):
            raise ValueError("attempt_callback must be callable")
        if task_sort_key is not None and not callable(task_sort_key):
            raise ValueError("task_sort_key must be callable")
        if task_site_resolver is not None and not callable(
            task_site_resolver
        ):
            raise ValueError("task_site_resolver must be callable")
        if type(stop_after_brand_issue) is not bool:
            raise ValueError("stop_after_brand_issue must be a bool")
        if (
            type(max_manual_verification_retries) is not int
            or max_manual_verification_retries < 0
        ):
            raise ValueError(
                "max_manual_verification_retries must be a non-negative integer"
            )
        self.repository = repository
        self.run_id = run_id.strip()
        self.attempt_callback = attempt_callback
        self.retry_policy = retry_policy or RetryPolicy()
        self.control = control or SchedulerControl()
        self.event_sink = event_sink
        self.event_errors: list[EventDeliveryError] = []
        self.manual_login_callback = manual_login_callback
        self.task_sort_key = task_sort_key
        self.task_site_resolver = task_site_resolver
        self.stop_after_brand_issue = stop_after_brand_issue
        self.max_manual_verification_retries = max_manual_verification_retries
        self._run_lock = threading.Lock()
        self._manual_lock = threading.Lock()
        self._waiting_action: ManualActionEvent | None = None

    @property
    def waiting_action(self) -> ManualActionEvent | None:
        with self._manual_lock:
            return self._waiting_action

    def run_until_idle(self) -> None:
        self._restore_waiting_action()
        with self._run_lock:
            while self.control.automated_work_allowed:
                tasks = self._eligible_tasks()
                if not tasks:
                    return
                selected_tasks = self._select_brand_batch(tasks)
                made_progress = False
                for task in selected_tasks:
                    if not self.control.automated_work_allowed:
                        return
                    if self._site_is_waiting(task):
                        continue
                    if self._run_task(task):
                        made_progress = True
                    if self.waiting_action is not None:
                        return
                if not made_progress:
                    return
                if self.stop_after_brand_issue and self._brand_has_issue(
                    selected_tasks
                ):
                    return

    def waiting_for_site(self, site: str) -> tuple[WebsiteTask, ...]:
        return self.repository.select_waiting(self.run_id, site=site)

    def enter_manual_login(self, site: str) -> None:
        with self._manual_lock:
            self.control.enter_manual_login(site)
            active_site = self.control.manual_site
            if active_site is None:
                raise AssertionError("manual login site was not retained")
        try:
            with self._run_lock:
                if self.manual_login_callback is not None:
                    self.manual_login_callback(active_site)
        except Exception:
            with self._manual_lock:
                self.control.cancel_manual_login(active_site)
            raise ManualLoginError("无法进入人工登录模式，请重试") from None

    def confirm_manual_login(self, site: str) -> tuple[WebsiteTask, ...]:
        normalized_site = _normalized_site(site)
        with self._run_lock:
            with self._manual_lock:
                if self.control.manual_site != normalized_site:
                    raise ValueError("确认的站点与当前人工登录站点不一致")
                requeued = self.repository.requeue_waiting_site(
                    self.run_id,
                    normalized_site,
                )
                self.control.confirm_manual_login(normalized_site)
                return requeued

    def continue_current_task(self) -> tuple[WebsiteTask, ...]:
        """Requeue the exact site paused by the automatic browser attempt."""
        with self._run_lock:
            with self._manual_lock:
                action = self._waiting_action
                if action is None:
                    raise ValueError("当前没有等待人工处理的网站任务")
                if self.control.manual_site != action.site:
                    raise AssertionError("manual action site does not match scheduler control")
                requeued_task = self.repository.requeue_exact_waiting_task(
                    self.run_id,
                    action.task,
                    action.site,
                    expected_token=action.token,
                )
                self.control.confirm_manual_login(action.site)
                self._waiting_action = None
                return (requeued_task,)

    def cancel_manual_action(self) -> bool:
        """Stop this scheduler while preserving the durable waiting task."""
        with self._manual_lock:
            action = self._waiting_action
            if action is None:
                return False
            if not self.control.cancel_manual_login(action.site):
                raise AssertionError("manual action site does not match scheduler control")
            self._waiting_action = None
            self.control.request_stop()
            return True

    def _eligible_tasks(self) -> tuple[WebsiteTask, ...]:
        if self.repository.select_waiting(self.run_id):
            return ()
        pending = self.repository.select_pending(self.run_id)
        failed = tuple(
            task
            for task in self.repository.select_failed(self.run_id)
            if self._can_retry_persisted_failure(task.task_id)
        )
        tasks = pending + failed
        if self.task_site_resolver is not None:
            waiting_sites = {
                self._task_site(task)
                for task in self.repository.select_waiting(self.run_id)
            }
            tasks = tuple(
                task
                for task in tasks
                if self._task_site(task) not in waiting_sites
            )
        if self.task_sort_key is not None:
            return tuple(sorted(tasks, key=self.task_sort_key))
        return tasks

    def _restore_waiting_action(self) -> None:
        with self._manual_lock:
            if self._waiting_action is not None or self.control.manual_site is not None:
                return
            waiting = self.repository.select_waiting(self.run_id)
            if not waiting:
                return
            if len(waiting) != 1:
                raise RepositoryError(
                    "等待人工处理的任务状态已变化，请重新开始报价"
                )
            task = waiting[0]
            site = self.repository.waiting_site(task.task_id)
            if site is None:
                raise RepositoryError(
                    "等待人工处理的任务状态已变化，请重新开始报价"
                )
            token = self.repository.waiting_attempt_token(task.task_id)
            if token is None:
                raise RepositoryError(
                    "等待人工处理的任务状态已变化，请重新开始报价"
                )
            self.control.enter_manual_login(site)
            self._waiting_action = ManualActionEvent(
                task=task,
                token=token,
                site=site,
                reason="等待人工登录或安全验证",
            )

    def _site_is_waiting(self, task: WebsiteTask) -> bool:
        if self.task_site_resolver is None:
            return False
        site = self._task_site(task)
        return any(
            self._task_site(waiting) == site
            for waiting in self.repository.select_waiting(self.run_id)
        )

    def _select_brand_batch(
        self, tasks: tuple[WebsiteTask, ...]
    ) -> tuple[WebsiteTask, ...]:
        if not self.stop_after_brand_issue:
            return tasks
        current_brand = tasks[0].brand
        return tuple(task for task in tasks if task.brand == current_brand)

    def _brand_has_issue(self, tasks: tuple[WebsiteTask, ...]) -> bool:
        blocking_states = {TaskState.TECHNICAL_FAILURE, TaskState.WAITING_FOR_LOGIN}
        return any(
            self.repository.task_state(task.task_id) in blocking_states
            for task in tasks
        )

    def _task_site(self, task: WebsiteTask) -> str:
        resolver = self.task_site_resolver
        if resolver is None:
            raise AssertionError("task site resolver is not configured")
        site = resolver(task)
        if not isinstance(site, str) or not site.strip():
            raise ValueError("task site resolver returned invalid data")
        return site.strip()

    def _can_retry_persisted_failure(self, task_id: str) -> bool:
        result = self.repository.load_result(task_id)
        error_code = result.error_code if result is not None else None
        if error_code is None:
            latest = self.repository.latest_attempt(task_id)
            error_code = latest.error_code if latest is not None else None
        if error_code not in RETRYABLE_ERROR_CODES:
            return False
        return self.retry_policy.can_start_technical_attempt(
            self._technical_attempts(task_id)
        )

    def _run_task(self, task: WebsiteTask) -> bool:
        completed_technical_attempts = self._technical_attempts(task.task_id)
        if not self.retry_policy.can_start_technical_attempt(
            completed_technical_attempts
        ):
            return False

        while self.control.automated_work_allowed:
            token = self.repository.start_attempt(task.task_id)
            self._emit(
                "progress",
                task.task_id,
                {
                    "attempt_number": token.attempt_number,
                    "channel": task.channel.value,
                },
            )
            try:
                result = self.attempt_callback(task, token, self.control)
            except Exception as error:
                classified = classify_attempt_error(error)
                if isinstance(classified, LoginRequired):
                    if self._manual_verification_retries_exhausted(
                        task,
                        classified,
                    ):
                        self._save_technical_failure(
                            task,
                            token,
                            self._MANUAL_VERIFICATION_EXHAUSTED_CODE,
                            self._manual_verification_exhausted_message(
                                classified.site
                            ),
                            retryable=False,
                            completed_technical_attempts=completed_technical_attempts,
                        )
                        return True
                    self._park_for_login(task, token, classified)
                    return True
                if not isinstance(classified, TechnicalError):
                    raise AssertionError("attempt classifier returned an unknown outcome")
                completed_technical_attempts += classified.retry_cost
                self._save_technical_failure(
                    task,
                    token,
                    classified.code,
                    classified.message,
                    retryable=isinstance(classified, RetryableTechnicalError),
                    completed_technical_attempts=completed_technical_attempts,
                )
                if isinstance(classified, NonRetryableTechnicalError):
                    return True
                if not self.retry_policy.can_start_technical_attempt(
                    completed_technical_attempts
                ):
                    return True
                if not self.retry_policy.wait_before_retry(self.control):
                    return True
                continue

            if not isinstance(result, WebsiteResult):
                completed_technical_attempts += 1
                classified = NonRetryableTechnicalError(
                    "INVALID_ATTEMPT_RESULT",
                    "网站适配器未返回有效结果",
                )
                self._save_technical_failure(
                    task,
                    token,
                    classified.code,
                    classified.message,
                    retryable=False,
                    completed_technical_attempts=completed_technical_attempts,
                )
                return True
            if result.task_id != task.task_id:
                classified = NonRetryableTechnicalError(
                    "RESULT_TASK_MISMATCH",
                    "网站结果与当前任务不匹配",
                )
                completed_technical_attempts += 1
                self._save_technical_failure(
                    task,
                    token,
                    classified.code,
                    classified.message,
                    retryable=False,
                    completed_technical_attempts=completed_technical_attempts,
                )
                return True
            if result.state is TaskState.SUCCEEDED:
                self.repository.save_result(result, token=token)
                self._emit(
                    "result",
                    task.task_id,
                    {
                        "outcome": result.outcome.value if result.outcome else None,
                        "price": str(result.price) if result.price is not None else None,
                    },
                )
                return True

            self.repository.save_result(result, token=token)
            retryable = result.error_code in RETRYABLE_ERROR_CODES
            completed_technical_attempts += (
                1 if consumes_technical_budget(result.error_code) else 0
            )
            self._emit_technical_failure(
                task.task_id,
                result.error_code or "UNKNOWN_TECHNICAL_FAILURE",
                retryable=retryable,
                completed_technical_attempts=completed_technical_attempts,
            )
            if (
                not retryable
                or not self.retry_policy.can_start_technical_attempt(
                    completed_technical_attempts
                )
                or not self.retry_policy.wait_before_retry(self.control)
            ):
                return True
        return True

    def _park_for_login(
        self,
        task: WebsiteTask,
        token: AttemptToken,
        error: LoginRequired,
    ) -> None:
        self.repository.mark_waiting_for_login(
            task.task_id,
            token=token,
            site=error.site,
            security_verification=isinstance(
                error,
                SecurityVerificationRequired,
            ),
        )
        self.control.enter_manual_login(error.site)
        with self._manual_lock:
            if self._waiting_action is not None:
                raise AssertionError("only one manual action may be active")
            self._waiting_action = ManualActionEvent(
                task=task,
                token=token,
                site=error.site,
                reason=error.reason,
            )
        condition = (
            "security_verification"
            if isinstance(error, SecurityVerificationRequired)
            else "login_required"
        )
        self._emit(
            "waiting_for_login",
            task.task_id,
            {
                "site": error.site,
                "condition": condition,
            },
        )

    def _manual_verification_retries_exhausted(
        self,
        task: WebsiteTask,
        error: LoginRequired,
    ) -> bool:
        if not isinstance(error, SecurityVerificationRequired):
            return False
        retries_already_used = sum(
            attempt.error_code == "SECURITY_VERIFICATION_REQUIRED"
            for attempt in self.repository.list_attempts(task.task_id)
        )
        return retries_already_used >= self.max_manual_verification_retries

    def _manual_verification_exhausted_message(self, site: str) -> str:
        return (
            f"{site}人工安全验证连续重试"
            f"{self.max_manual_verification_retries}次仍未通过，"
            "已跳过该站并继续后续网站；请在执行报告中人工补充该渠道"
        )

    def _save_technical_failure(
        self,
        task: WebsiteTask,
        token: AttemptToken,
        code: str,
        message: str,
        *,
        retryable: bool,
        completed_technical_attempts: int,
    ) -> None:
        result = WebsiteResult(
            task_id=task.task_id,
            state=TaskState.TECHNICAL_FAILURE,
            outcome=None,
            price=None,
            url=None,
            evidence=None,
            diagnostic_path=None,
            error_code=code,
            error_message=credential_free_error_message(code, message),
        )
        self.repository.save_result(result, token=token)
        self._emit_technical_failure(
            task.task_id,
            code,
            retryable=retryable,
            completed_technical_attempts=completed_technical_attempts,
        )

    def _emit_technical_failure(
        self,
        task_id: str,
        code: str,
        *,
        retryable: bool,
        completed_technical_attempts: int,
    ) -> None:
        retry_remaining = max(
            0,
            self.retry_policy.maximum_technical_attempts
            - completed_technical_attempts,
        )
        self._emit(
            "technical_failure",
            task_id,
            {
                "error_code": code,
                "retryable": retryable,
                "retry_remaining": retry_remaining,
            },
        )

    def publish_observation(
        self,
        task: WebsiteTask,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> None:
        self._emit(
            "observation",
            task.task_id,
            {
                "outcome": checkpoint.outcome.value,
                "price": (
                    str(checkpoint.price)
                    if checkpoint.price is not None
                    else None
                ),
            },
        )

    def _technical_attempts(self, task_id: str) -> int:
        generation = self.repository.task_generation(task_id)
        return sum(
            consumes_technical_budget(attempt.error_code)
            for attempt in self.repository.list_attempts(task_id)
            if attempt.generation == generation
        )

    def _emit(
        self,
        event: str,
        task_id: str,
        data: dict[str, JsonValue],
    ) -> None:
        if self.event_sink is None:
            return
        worker_event = WorkerEvent(
            event=event,
            run_id=self.run_id,
            task_id=task_id,
            data=data,
        )
        try:
            self.event_sink(worker_event)
        except Exception:
            self.event_errors.append(
                EventDeliveryError(
                    event=event,
                    task_id=task_id,
                )
            )


def _normalized_site(site: object) -> str:
    if not isinstance(site, str) or not site.strip():
        raise ValueError("site must not be blank")
    return site.strip()

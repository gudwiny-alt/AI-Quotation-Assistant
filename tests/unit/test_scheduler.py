from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.browser.worker import WorkerEvent
from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceRectangle,
    EvidenceState,
)
from quote_app.tasks.models import (
    SCHEMA_VERSION,
    BusinessOutcome,
    InputFingerprint,
    RunRecord,
    RunState,
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.repository import SQLiteTaskRepository
from quote_app.tasks.retry import (
    LoginRequired,
    NonRetryableTechnicalError,
    RetryPolicy,
    RetryableTechnicalError,
    SchedulerControl,
    SecurityVerificationRequired,
)
from quote_app.tasks.scheduler import BrowserTaskScheduler

NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _fingerprint(tmp_path: Path, role: str) -> InputFingerprint:
    path = tmp_path / f"{role}.xlsx"
    path.write_bytes(role.encode())
    stat = path.stat()
    return InputFingerprint(
        source_role=role,
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
    )


def _run(tmp_path: Path) -> RunRecord:
    snapshot = "[]"
    return RunRecord(
        run_id="run-1",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=tuple(
            _fingerprint(tmp_path, role)
            for role in ("base", "marketing", "bop")
        ),
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot=snapshot,
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256=hashlib.sha256(
            snapshot.encode()
        ).hexdigest(),
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )


def _task(
    task_id: str,
    *,
    channel: WebsiteChannel,
    output_row: int,
) -> WebsiteTask:
    return WebsiteTask(
        task_id=task_id,
        run_id="run-1",
        source_row_number=output_row,
        output_row_number=output_row,
        material_code=f"CODE-{output_row}",
        brand="小米",
        model_name="小米 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=channel,
    )


def _success(tmp_path: Path, task: WebsiteTask) -> WebsiteResult:
    evidence_path = tmp_path / f"{task.task_id}.png"
    evidence_path.write_bytes(task.task_id.encode())
    return WebsiteResult(
        task_id=task.task_id,
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("3999"),
        url=f"https://example.test/{task.task_id}",
        evidence=EvidenceRecord(
            path=evidence_path,
            sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            state=EvidenceState.NORMAL,
            pixel_width=1920,
            pixel_height=1080,
            captured_at=NOW,
            validation_code="CAPTURE_OK",
            annotations=(EvidenceRectangle("price", 10, 20, 100, 40),),
        ),
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )


def _technical_failure(
    task_id: str,
    *,
    error_code: str = "NETWORK_ERROR",
) -> WebsiteResult:
    return WebsiteResult(
        task_id=task_id,
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url=None,
        evidence=None,
        diagnostic_path=None,
        error_code=error_code,
        error_message="网站技术处理失败",
    )


@pytest.fixture
def repository(tmp_path: Path):
    value = SQLiteTaskRepository(tmp_path / "state.sqlite3")
    value.create_run(_run(tmp_path))
    try:
        yield value
    finally:
        value.close()


def _seed(
    repository: SQLiteTaskRepository,
    *tasks: WebsiteTask,
) -> None:
    for task in tasks:
        repository.upsert_task(task)


def test_scheduler_rejects_noncallable_task_sort_key(
    repository: SQLiteTaskRepository,
) -> None:
    with pytest.raises(ValueError, match="task_sort_key"):
        BrowserTaskScheduler(
            repository,
            "run-1",
            lambda _task, _token, _control: _technical_failure("unused"),
            task_sort_key=object(),  # type: ignore[arg-type]
        )


def test_initial_attempt_and_two_technical_retries_are_persisted(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("tmall-1", channel=WebsiteChannel.TMALL, output_row=2)
    _seed(repository, task)
    attempted: list[int] = []

    def fail(_task, token, _control):
        attempted.append(token.attempt_number)
        raise RetryableTechnicalError("LAYOUT_CHANGED", "页面结构发生变化")

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        fail,
        retry_policy=RetryPolicy(retry_delay_seconds=0),
    )

    scheduler.run_until_idle()

    assert attempted == [1, 2, 3]
    assert repository.attempt_count(task.task_id) == 3
    assert repository.task_state(task.task_id) is TaskState.TECHNICAL_FAILURE
    assert [
        attempt.error_code for attempt in repository.list_attempts(task.task_id)
    ] == ["LAYOUT_CHANGED", "LAYOUT_CHANGED", "LAYOUT_CHANGED"]


@pytest.mark.parametrize(
    "login_error",
    [
        LoginRequired("天猫", "登录后才能查看价格"),
        SecurityVerificationRequired("天猫", "需要人工完成安全验证"),
    ],
)
def test_login_attempt_is_parked_without_using_three_attempt_technical_budget(
    repository: SQLiteTaskRepository,
    login_error: BaseException,
) -> None:
    task = _task("tmall-1", channel=WebsiteChannel.TMALL, output_row=2)
    _seed(repository, task)
    mode = "login"

    def attempt(_task, _token, _control):
        nonlocal mode
        if mode == "login":
            raise login_error
        raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        attempt,
        retry_policy=RetryPolicy(retry_delay_seconds=0),
    )
    scheduler.run_until_idle()
    mode = "technical"
    scheduler.enter_manual_login("天猫")
    scheduler.confirm_manual_login("天猫")
    scheduler.run_until_idle()

    attempts = repository.list_attempts(task.task_id)
    assert len(attempts) == 4
    assert attempts[0].error_code == "LOGIN_REQUIRED"
    assert [attempt.error_code for attempt in attempts[1:]] == [
        "NETWORK_ERROR",
        "NETWORK_ERROR",
        "NETWORK_ERROR",
    ]


def test_login_site_is_parked_while_other_sites_continue(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    tasks = (
        _task("tmall", channel=WebsiteChannel.TMALL, output_row=2),
        _task("jd", channel=WebsiteChannel.JD, output_row=3),
        _task("official", channel=WebsiteChannel.OFFICIAL, output_row=4),
    )
    _seed(repository, *tasks)
    visited: list[str] = []

    def attempt(task, _token, _control):
        visited.append(task.task_id)
        if task.channel is WebsiteChannel.TMALL:
            raise LoginRequired("天猫", "需要登录")
        return _success(tmp_path, task)

    scheduler = BrowserTaskScheduler(repository, "run-1", attempt)

    scheduler.run_until_idle()

    assert visited == ["tmall", "jd", "official"]
    assert repository.task_state("tmall") is TaskState.WAITING_FOR_LOGIN
    assert repository.task_state("jd") is TaskState.SUCCEEDED
    assert repository.task_state("official") is TaskState.SUCCEEDED
    assert scheduler.waiting_for_site("天猫") == (tasks[0],)


def test_all_tasks_waiting_for_the_same_site_are_discoverable(
    repository: SQLiteTaskRepository,
) -> None:
    tasks = (
        _task("tmall-1", channel=WebsiteChannel.TMALL, output_row=2),
        _task("tmall-2", channel=WebsiteChannel.TMALL, output_row=3),
    )
    _seed(repository, *tasks)

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        lambda _task, _token, _control: (
            (_ for _ in ()).throw(LoginRequired("天猫", "需要登录"))
        ),
    )
    scheduler.run_until_idle()

    assert scheduler.waiting_for_site("天猫") == tasks


def test_manual_login_mode_globally_pauses_attempts_and_requeues_only_its_site(
    repository: SQLiteTaskRepository,
) -> None:
    jd = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    tmall = _task("tmall", channel=WebsiteChannel.TMALL, output_row=3)
    pending = _task("official", channel=WebsiteChannel.OFFICIAL, output_row=4)
    _seed(repository, jd, tmall, pending)
    for task, site in ((jd, "京东"), (tmall, "天猫")):
        token = repository.start_attempt(task.task_id)
        repository.mark_waiting_for_login(task.task_id, token=token, site=site)
    visited: list[str] = []
    foregrounded_sites: list[str] = []

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        lambda task, _token, _control: visited.append(task.task_id),
        manual_login_callback=foregrounded_sites.append,
    )
    scheduler.enter_manual_login("天猫")

    scheduler.run_until_idle()
    requeued = scheduler.confirm_manual_login("天猫")

    assert visited == []
    assert foregrounded_sites == ["天猫"]
    assert requeued == (tmall,)
    assert repository.task_state("tmall") is TaskState.PENDING
    assert repository.task_state("jd") is TaskState.WAITING_FOR_LOGIN
    assert repository.task_state("official") is TaskState.PENDING


def test_manual_login_foreground_failure_rolls_back_mode_without_requeueing(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    tmall = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    jd = _task("jd", channel=WebsiteChannel.JD, output_row=3)
    _seed(repository, tmall, jd)
    token = repository.start_attempt(tmall.task_id)
    repository.mark_waiting_for_login(
        tmall.task_id,
        token=token,
        site="天猫",
    )
    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        lambda task, _token, _control: _success(tmp_path, task),
        manual_login_callback=lambda _site: (
            (_ for _ in ()).throw(RuntimeError("password=hunter2"))
        ),
    )

    with pytest.raises(RuntimeError, match="无法进入人工登录模式") as captured:
        scheduler.enter_manual_login("天猫")

    assert "hunter2" not in str(captured.value)
    assert scheduler.control.manual_site is None
    assert repository.task_state(tmall.task_id) is TaskState.WAITING_FOR_LOGIN
    scheduler.run_until_idle()
    assert repository.task_state(jd.task_id) is TaskState.SUCCEEDED


def test_manual_login_pauses_active_attempt_before_waiting_for_foreground_lock(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    _seed(repository, task)
    attempt_started = threading.Event()
    abort_attempt = threading.Event()
    manual_seen_by_attempt = threading.Event()
    attempt_finished = threading.Event()
    foregrounded: list[tuple[str, bool]] = []

    def attempt(task, _token, control):
        attempt_started.set()
        while control.manual_site is None and not abort_attempt.wait(0.01):
            pass
        if control.manual_site == "天猫":
            manual_seen_by_attempt.set()
        attempt_finished.set()
        return _success(tmp_path, task)

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        attempt,
        manual_login_callback=lambda site: foregrounded.append(
            (site, attempt_finished.is_set())
        ),
    )
    runner = threading.Thread(target=scheduler.run_until_idle)
    manual = threading.Thread(
        target=lambda: scheduler.enter_manual_login("天猫")
    )
    runner.start()
    assert attempt_started.wait(1)
    manual.start()
    manual_became_visible = manual_seen_by_attempt.wait(1)
    try:
        abort_attempt.set()
        runner.join(2)
        manual.join(2)
    finally:
        abort_attempt.set()
        runner.join(2)
        manual.join(2)

    assert manual_became_visible
    assert not runner.is_alive()
    assert not manual.is_alive()
    assert foregrounded == [("天猫", True)]
    assert repository.task_state(task.task_id) is TaskState.SUCCEEDED
    scheduler.control.confirm_manual_login("天猫")


def test_confirm_login_database_failure_keeps_manual_and_waiting_state(
    repository: SQLiteTaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    _seed(repository, task)
    token = repository.start_attempt(task.task_id)
    repository.mark_waiting_for_login(
        task.task_id,
        token=token,
        site="天猫",
    )
    scheduler = BrowserTaskScheduler(repository, "run-1", lambda *_args: None)
    scheduler.enter_manual_login("天猫")

    def fail_requeue(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "requeue_waiting_site", fail_requeue)

    with pytest.raises(RuntimeError, match="database unavailable"):
        scheduler.confirm_manual_login("天猫")

    assert scheduler.control.manual_site == "天猫"
    assert repository.task_state(task.task_id) is TaskState.WAITING_FOR_LOGIN


def test_confirm_login_rejects_other_site_without_requeueing(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    _seed(repository, task)
    token = repository.start_attempt(task.task_id)
    repository.mark_waiting_for_login(
        task.task_id,
        token=token,
        site="天猫",
    )
    scheduler = BrowserTaskScheduler(repository, "run-1", lambda *_args: None)
    scheduler.enter_manual_login("天猫")

    with pytest.raises(ValueError, match="不一致"):
        scheduler.confirm_manual_login("京东")

    assert scheduler.control.manual_site == "天猫"
    assert repository.task_state(task.task_id) is TaskState.WAITING_FOR_LOGIN


def test_confirming_login_while_still_logged_out_parks_once_without_busy_loop(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    _seed(repository, task)
    first = repository.start_attempt(task.task_id)
    repository.mark_waiting_for_login(task.task_id, token=first, site="天猫")
    calls = 0

    def still_logged_out(_task, _token, _control):
        nonlocal calls
        calls += 1
        raise LoginRequired("天猫", "仍需登录")

    scheduler = BrowserTaskScheduler(repository, "run-1", still_logged_out)
    scheduler.enter_manual_login("天猫")
    scheduler.confirm_manual_login("天猫")

    scheduler.run_until_idle()

    assert calls == 1
    assert repository.task_state(task.task_id) is TaskState.WAITING_FOR_LOGIN
    assert len(repository.list_attempts(task.task_id)) == 2


def test_restart_keeps_waiting_tasks_and_runs_unrelated_pending_work(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    tmall = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    jd = _task("jd", channel=WebsiteChannel.JD, output_row=3)
    with SQLiteTaskRepository(database) as first:
        first.create_run(_run(tmp_path))
        _seed(first, tmall, jd)
        token = first.start_attempt(tmall.task_id)
        first.mark_waiting_for_login(tmall.task_id, token=token, site="天猫")

    visited: list[str] = []
    with SQLiteTaskRepository(database) as reopened:
        scheduler = BrowserTaskScheduler(
            reopened,
            "run-1",
            lambda task, _token, _control: (
                visited.append(task.task_id) or _success(tmp_path, task)
            ),
        )
        scheduler.run_until_idle()

        assert visited == ["jd"]
        assert reopened.task_state(tmall.task_id) is TaskState.WAITING_FOR_LOGIN
        assert reopened.task_state(jd.task_id) is TaskState.SUCCEEDED


def test_pause_during_retry_delay_leaves_retryable_failure_resumable(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    _seed(repository, task)
    control = SchedulerControl()
    calls = 0

    def fail(_task, _token, _control):
        nonlocal calls
        calls += 1
        raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        fail,
        retry_policy=RetryPolicy(retry_delay_seconds=5),
        control=control,
    )
    timer = threading.Timer(0.05, control.request_pause)
    timer.start()
    try:
        scheduler.run_until_idle()
    finally:
        timer.cancel()

    assert calls == 1
    assert repository.task_state(task.task_id) is TaskState.TECHNICAL_FAILURE

    control.resume()
    scheduler.run_until_idle()
    assert calls == 3


def test_higher_task_generation_receives_a_fresh_technical_retry_budget(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    _seed(repository, task)
    first_generation_calls = 0

    def fail(_task, _token, _control):
        nonlocal first_generation_calls
        first_generation_calls += 1
        raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")

    BrowserTaskScheduler(
        repository,
        "run-1",
        fail,
        retry_policy=RetryPolicy(retry_delay_seconds=0),
    ).run_until_idle()
    assert first_generation_calls == 3

    repository.upsert_task(task, generation=1)
    second_generation_calls = 0

    def succeed(task, _token, _control):
        nonlocal second_generation_calls
        second_generation_calls += 1
        return _success(tmp_path, task)

    BrowserTaskScheduler(repository, "run-1", succeed).run_until_idle()

    assert second_generation_calls == 1
    assert repository.task_generation(task.task_id) == 1
    assert repository.task_state(task.task_id) is TaskState.SUCCEEDED


def test_reopen_after_one_technical_failure_has_only_two_attempts_remaining(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    with SQLiteTaskRepository(database) as first:
        first.create_run(_run(tmp_path))
        _seed(first, task)
        token = first.start_attempt(task.task_id)
        first.save_result(_technical_failure(task.task_id), token=token)

    calls = 0

    def fail(_task, _token, _control):
        nonlocal calls
        calls += 1
        raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")

    with SQLiteTaskRepository(database) as reopened:
        BrowserTaskScheduler(
            reopened,
            "run-1",
            fail,
            retry_policy=RetryPolicy(retry_delay_seconds=0),
        ).run_until_idle()

        assert calls == 2
        assert len(reopened.list_attempts(task.task_id)) == 3


def test_reopen_retries_persisted_transient_capture_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    with SQLiteTaskRepository(database) as first:
        first.create_run(_run(tmp_path))
        _seed(first, task)
        token = first.start_attempt(task.task_id)
        first.save_result(
            _technical_failure(
                task.task_id,
                error_code="CAPTURE_BLANK",
            ),
            token=token,
        )

    calls = 0

    def succeed(task, _token, _control):
        nonlocal calls
        calls += 1
        return _success(tmp_path, task)

    with SQLiteTaskRepository(database) as reopened:
        BrowserTaskScheduler(reopened, "run-1", succeed).run_until_idle()

        assert calls == 1
        assert reopened.task_state(task.task_id) is TaskState.SUCCEEDED


def test_reopen_does_not_retry_persisted_nonretryable_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    with SQLiteTaskRepository(database) as first:
        first.create_run(_run(tmp_path))
        _seed(first, task)
        token = first.start_attempt(task.task_id)
        first.save_result(
            _technical_failure(
                task.task_id,
                error_code="CAPTURE_ENVIRONMENT",
            ),
            token=token,
        )

    calls = 0

    def should_not_run(task, _token, _control):
        nonlocal calls
        calls += 1
        return _success(tmp_path, task)

    with SQLiteTaskRepository(database) as reopened:
        BrowserTaskScheduler(
            reopened,
            "run-1",
            should_not_run,
        ).run_until_idle()

        assert calls == 0
        assert (
            reopened.task_state(task.task_id)
            is TaskState.TECHNICAL_FAILURE
        )


def test_recovered_process_interruption_does_not_consume_retry_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    with SQLiteTaskRepository(database) as first:
        first.create_run(_run(tmp_path))
        _seed(first, task)
        first.start_attempt(task.task_id)

    calls = 0

    def fail(_task, _token, _control):
        nonlocal calls
        calls += 1
        raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")

    with SQLiteTaskRepository(database) as reopened:
        BrowserTaskScheduler(
            reopened,
            "run-1",
            fail,
            retry_policy=RetryPolicy(retry_delay_seconds=0),
        ).run_until_idle()

        attempts = reopened.list_attempts(task.task_id)
        assert calls == 3
        assert len(attempts) == 4
        assert attempts[0].error_code == "PROCESS_INTERRUPTED"


def test_reopen_after_login_wait_still_has_three_technical_attempts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    task = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    with SQLiteTaskRepository(database) as first:
        first.create_run(_run(tmp_path))
        _seed(first, task)
        token = first.start_attempt(task.task_id)
        first.mark_waiting_for_login(task.task_id, token=token, site="天猫")

    calls = 0

    def fail(_task, _token, _control):
        nonlocal calls
        calls += 1
        raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")

    with SQLiteTaskRepository(database) as reopened:
        scheduler = BrowserTaskScheduler(
            reopened,
            "run-1",
            fail,
            retry_policy=RetryPolicy(retry_delay_seconds=0),
        )
        scheduler.enter_manual_login("天猫")
        scheduler.confirm_manual_login("天猫")
        scheduler.run_until_idle()

        attempts = reopened.list_attempts(task.task_id)
        assert calls == 3
        assert len(attempts) == 4
        assert attempts[0].error_code == "LOGIN_REQUIRED"


def test_worker_events_are_versioned_json_safe_and_exclude_credentials(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    _seed(repository, task)
    events = []

    def requires_login(_task, _token, _control):
        raise LoginRequired(
            "天猫",
            "password=hunter2 token=secret Cookie: session=private",
        )

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        requires_login,
        event_sink=events.append,
    )

    scheduler.run_until_idle()

    payloads = [event.to_payload() for event in events]
    encoded = json.dumps(payloads, ensure_ascii=False)
    assert [payload["event"] for payload in payloads] == [
        "progress",
        "waiting_for_login",
    ]
    assert all(payload["schema_version"] == 1 for payload in payloads)
    assert "hunter2" not in encoded
    assert "secret" not in encoded
    assert "session=private" not in encoded


def test_progress_event_sink_failure_does_not_skip_callback_or_later_channels(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    tasks = (
        _task("tmall", channel=WebsiteChannel.TMALL, output_row=2),
        _task("jd", channel=WebsiteChannel.JD, output_row=3),
        _task("official", channel=WebsiteChannel.OFFICIAL, output_row=4),
    )
    _seed(repository, *tasks)
    visited: list[str] = []

    def sink(event: WorkerEvent) -> None:
        if event.event == "progress":
            raise RuntimeError("token=secret")

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        lambda task, _token, _control: (
            visited.append(task.task_id) or _success(tmp_path, task)
        ),
        event_sink=sink,
    )

    scheduler.run_until_idle()

    assert visited == ["tmall", "jd", "official"]
    assert all(
        repository.task_state(task.task_id) is TaskState.SUCCEEDED
        for task in tasks
    )
    assert len(scheduler.event_errors) == 3
    assert "secret" not in repr(scheduler.event_errors)


def test_waiting_event_sink_failure_keeps_site_parked_and_continues(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    tmall = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    jd = _task("jd", channel=WebsiteChannel.JD, output_row=3)
    _seed(repository, tmall, jd)

    def attempt(task, _token, _control):
        if task.task_id == tmall.task_id:
            raise LoginRequired("天猫", "需要登录")
        return _success(tmp_path, task)

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        attempt,
        event_sink=lambda event: (
            (_ for _ in ()).throw(RuntimeError("event queue closed"))
            if event.event == "waiting_for_login"
            else None
        ),
    )

    scheduler.run_until_idle()

    assert repository.task_state(tmall.task_id) is TaskState.WAITING_FOR_LOGIN
    assert repository.task_state(jd.task_id) is TaskState.SUCCEEDED
    assert [error.event for error in scheduler.event_errors] == [
        "waiting_for_login"
    ]


def test_result_event_sink_failure_keeps_success_and_continues(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    tasks = (
        _task("jd", channel=WebsiteChannel.JD, output_row=2),
        _task("official", channel=WebsiteChannel.OFFICIAL, output_row=3),
    )
    _seed(repository, *tasks)
    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        lambda task, _token, _control: _success(tmp_path, task),
        event_sink=lambda event: (
            (_ for _ in ()).throw(RuntimeError("result listener failed"))
            if event.event == "result"
            else None
        ),
    )

    scheduler.run_until_idle()

    assert all(
        repository.task_state(task.task_id) is TaskState.SUCCEEDED
        for task in tasks
    )
    assert [error.event for error in scheduler.event_errors] == [
        "result",
        "result",
    ]


def test_technical_failure_event_sink_failure_preserves_retries_and_next_task(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    failing = _task("tmall", channel=WebsiteChannel.TMALL, output_row=2)
    jd = _task("jd", channel=WebsiteChannel.JD, output_row=3)
    _seed(repository, failing, jd)
    attempts = 0

    def attempt(task, _token, _control):
        nonlocal attempts
        if task.task_id == failing.task_id:
            attempts += 1
            raise RetryableTechnicalError("NETWORK_ERROR", "网络连接失败")
        return _success(tmp_path, task)

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        attempt,
        retry_policy=RetryPolicy(retry_delay_seconds=0),
        event_sink=lambda event: (
            (_ for _ in ()).throw(RuntimeError("failure listener failed"))
            if event.event == "technical_failure"
            else None
        ),
    )

    scheduler.run_until_idle()

    assert attempts == 3
    assert repository.task_state(failing.task_id) is TaskState.TECHNICAL_FAILURE
    assert repository.task_state(jd.task_id) is TaskState.SUCCEEDED
    assert len(scheduler.event_errors) == 3


@pytest.mark.parametrize(
    "unsafe_data",
    [
        {"password": "hunter2"},
        {"message": "token=secret"},
        {"progress": float("nan")},
    ],
)
def test_worker_event_contract_rejects_credentials_and_non_json_numbers(
    unsafe_data: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="JSON-safe and credential-free"):
        WorkerEvent(
            event="progress",
            run_id="run-1",
            task_id="task-1",
            data=unsafe_data,  # type: ignore[arg-type]
        )


def test_worker_event_schema_version_rejects_bool() -> None:
    with pytest.raises(ValueError, match="schema version"):
        WorkerEvent(
            event="progress",
            run_id="run-1",
            task_id="task-1",
            data={},
            schema_version=True,
        )


def test_worker_event_deeply_freezes_input_and_returns_independent_payloads() -> None:
    original = {"nested": {"message": "safe"}}
    event = WorkerEvent(
        event="progress",
        run_id="run-1",
        task_id="task-1",
        data=original,
    )

    original["nested"]["message"] = "token=secret"
    first_payload = event.to_payload()
    first_payload["data"]["nested"]["message"] = "password=hunter2"
    second_payload = event.to_payload()

    assert second_payload["data"] == {"nested": {"message": "safe"}}
    with pytest.raises(TypeError):
        event.data["password"] = "hunter2"


def test_invalid_technical_code_finishes_attempt_with_safe_stable_failure(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    _seed(repository, task)

    def invalid_error(_task, _token, _control):
        raise RetryableTechnicalError("token=secret", "password=hunter2")

    BrowserTaskScheduler(repository, "run-1", invalid_error).run_until_idle()

    result = repository.load_result(task.task_id)
    assert result is not None
    assert result.error_code == "UNEXPECTED_BROWSER_ERROR"
    assert repository.task_state(task.task_id) is TaskState.TECHNICAL_FAILURE
    assert repository.latest_attempt(task.task_id).finished_at is not None


def test_sensitive_technical_message_is_not_persisted_or_emitted(
    repository: SQLiteTaskRepository,
) -> None:
    task = _task("jd", channel=WebsiteChannel.JD, output_row=2)
    _seed(repository, task)
    events = []

    def fail(_task, _token, _control):
        raise NonRetryableTechnicalError(
            "SITE_NOT_SUPPORTED",
            "token=secret Cookie: session=private",
        )

    scheduler = BrowserTaskScheduler(
        repository,
        "run-1",
        fail,
        event_sink=events.append,
    )
    scheduler.run_until_idle()

    result = repository.load_result(task.task_id)
    assert result is not None
    combined = json.dumps(
        {
            "message": result.error_message,
            "events": [event.to_payload() for event in events],
        },
        ensure_ascii=False,
    )
    assert "secret" not in combined
    assert "session=private" not in combined


def test_one_scheduler_serializes_foreground_attempts_across_concurrent_callers(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
) -> None:
    tasks = (
        _task("jd-1", channel=WebsiteChannel.JD, output_row=2),
        _task("jd-2", channel=WebsiteChannel.JD, output_row=3),
    )
    _seed(repository, *tasks)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def capture(task, _token, _control):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return _success(tmp_path, task)

    scheduler = BrowserTaskScheduler(repository, "run-1", capture)
    callers = [
        threading.Thread(target=scheduler.run_until_idle),
        threading.Thread(target=scheduler.run_until_idle),
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()

    assert maximum_active == 1
    assert all(
        repository.task_state(task.task_id) is TaskState.SUCCEEDED
        for task in tasks
    )

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from quote_app.evidence.models import EvidenceRecord, EvidenceState, MacCapturePolicy
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteTask,
    WebsiteObservationCheckpoint,
)
from quote_app.browser.worker import WorkerEvent
from quote_app.tasks.repository import AttemptToken

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _task(
    task_id: str,
    *,
    channel: WebsiteChannel = WebsiteChannel.JD,
    output_row: int = 2,
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


def _evidence(path: Path, payload: bytes) -> EvidenceRecord:
    path.write_bytes(payload)
    return EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        pixel_width=800,
        pixel_height=600,
        captured_at=NOW,
        validation_code="CAPTURE_OK",
    )


def _success(task: WebsiteTask, evidence: EvidenceRecord) -> WebsiteResult:
    return WebsiteResult(
        task_id=task.task_id,
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("3999"),
        url="https://example.test/product",
        evidence=evidence,
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )


def _failure(task: WebsiteTask, code: str) -> WebsiteResult:
    return WebsiteResult(
        task_id=task.task_id,
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url=None,
        evidence=None,
        diagnostic_path=None,
        error_code=code,
        error_message="网站技术处理失败",
    )


def _request(tmp_path: Path, tasks: tuple[WebsiteTask, ...]):
    from quote_app.services.web_run import WebsiteRunRequest

    return WebsiteRunRequest(
        run_id="run-1",
        tasks=tasks,
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
    )


def test_manual_action_controller_keeps_action_visible_until_continue() -> None:
    from quote_app.tasks.scheduler import ManualActionEvent
    from quote_app.services.web_run import WebsiteRunController

    task = _task("waiting", channel=WebsiteChannel.TMALL)
    controller = WebsiteRunController()
    action = ManualActionEvent(
        task=task,
        token=AttemptToken(task.task_id, 0, 1),
        site="天猫",
        reason="需要人工完成安全验证",
    )

    controller.publish_manual_action(action)

    assert controller.waiting_action == action
    controller.continue_current_task()
    assert controller.wait_for_resolution() is True
    assert controller.waiting_action is None


def test_service_event_sink_forwards_durable_event_and_same_repository_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    task = _task("event-sink")
    evidence = _evidence(tmp_path / "event-sink.png", b"formal")
    result = _success(task, evidence)
    checkpoint = WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("3999"),
        url="https://example.test/product",
        observed_at=NOW,
    )
    events: list[WorkerEvent] = []
    snapshots: list[object] = []
    repositories: list[object] = []

    class Repository:
        def __init__(self, _path: Path) -> None:
            self.observation: WebsiteObservationCheckpoint | None = None
            self.result: WebsiteResult | None = None
            self.state = TaskState.PENDING
            repositories.append(self)

        def load_observation(
            self, task_id: str
        ) -> WebsiteObservationCheckpoint | None:
            assert task_id == task.task_id
            return self.observation

        def load_result(self, task_id: str) -> WebsiteResult | None:
            assert task_id == task.task_id
            return self.result

        def task_state(self, task_id: str) -> TaskState:
            assert task_id == task.task_id
            return self.state

        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class Runner:
        def __init__(self, **kwargs: object) -> None:
            self.repository = kwargs["repository"]
            self.event_sink = kwargs["event_sink"]
            self.scheduler = SimpleNamespace(waiting_action=None)

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            self.event_sink(WorkerEvent("progress", "run-1", task.task_id, {}))
            self.repository.observation = checkpoint
            self.event_sink(
                WorkerEvent("observation", "run-1", task.task_id, {"price": "3999"})
            )
            self.repository.result = result
            self.repository.state = TaskState.SUCCEEDED
            self.event_sink(WorkerEvent("result", "run-1", task.task_id, {}))
            return (result,)

    runtime = SimpleNamespace(
        evidence_capture=lambda: SimpleNamespace(),
        capture_context_provider=lambda *_args: None,
    )
    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)

    web_run.run_website_tasks(
        web_run.WebsiteRunRequest(
            run_id="run-1",
            tasks=(task,),
            profile_dir=tmp_path / "profile",
            evidence_dir=tmp_path / "evidence",
            database_path=tmp_path / "state.sqlite3",
            event_sink=events.append,
            checkpoint_sink=snapshots.append,
        ),
        runtime_factory=lambda _registry: runtime,
    )

    assert [event.event for event in events] == ["progress", "observation", "result"]
    assert len(repositories) == 1
    assert len(snapshots) == 2
    assert snapshots[0].observations == (checkpoint,)
    assert snapshots[0].results == ()
    assert snapshots[1].results == (result,)


def test_checkpoint_sink_receives_only_durable_stage_events_but_ui_receives_all(
    tmp_path: Path,
) -> None:
    """Break caught: progress or retryable failures rewrite partial workbooks."""
    from quote_app.services import web_run

    task = _task("checkpoint-events")
    events: list[WorkerEvent] = []
    snapshots: list[object] = []

    class Repository:
        def load_observation(self, _task_id: str) -> None:
            return None

        def load_result(self, _task_id: str) -> None:
            return None

        def task_state(self, _task_id: str) -> TaskState:
            return TaskState.PENDING

    request = web_run.WebsiteRunRequest(
        run_id="run-1",
        tasks=(task,),
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
        event_sink=events.append,
        checkpoint_sink=snapshots.append,
    )
    sink = web_run._event_sink_for_request(request, Repository())
    assert sink is not None
    source_events = [
        WorkerEvent("progress", "run-1", task.task_id, {}),
        WorkerEvent("observation", "run-1", task.task_id, {}),
        WorkerEvent("result", "run-1", task.task_id, {}),
        WorkerEvent("waiting_for_login", "run-1", task.task_id, {}),
        WorkerEvent(
            "technical_failure",
            "run-1",
            task.task_id,
            {"retryable": True, "retry_remaining": 1},
        ),
        WorkerEvent(
            "technical_failure",
            "run-1",
            task.task_id,
            {"retryable": False, "retry_remaining": 1},
        ),
        WorkerEvent(
            "technical_failure",
            "run-1",
            task.task_id,
            {"retryable": True, "retry_remaining": 0},
        ),
    ]

    for event in source_events:
        sink(event)

    assert events == source_events
    assert len(snapshots) == 5


def test_checkpoint_index_does_not_read_tasks_before_runner_registers_them(
    tmp_path: Path,
) -> None:
    """Break caught: a new run fails before the runner can persist its tasks."""
    from quote_app.services import web_run
    from quote_app.tasks.repository import SQLiteTaskRepository

    task = _task("new-run-task")
    request = web_run.WebsiteRunRequest(
        run_id="run-1",
        tasks=(task,),
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
        checkpoint_sink=lambda _snapshot: None,
    )

    with SQLiteTaskRepository(request.database_path) as repository:
        sink = web_run._event_sink_for_request(request, repository)

    assert sink is not None


def test_checkpoint_event_updates_only_its_task_after_one_initial_snapshot_scan(
    tmp_path: Path,
) -> None:
    """Break caught: every one of 1,800 stage events scans all 900 tasks again."""
    from quote_app.services import web_run

    tasks = tuple(
        _task(f"task-{index}", output_row=index + 2)
        for index in range(900)
    )
    load_calls = {"observation": 0, "result": 0, "state": 0}

    class Repository:
        def load_observation(self, _task_id: str) -> None:
            load_calls["observation"] += 1
            return None

        def load_result(self, _task_id: str) -> None:
            load_calls["result"] += 1
            return None

        def task_state(self, _task_id: str) -> TaskState:
            load_calls["state"] += 1
            return TaskState.PENDING

    snapshots: list[object] = []
    request = web_run.WebsiteRunRequest(
        run_id="run-1",
        tasks=tasks,
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
        checkpoint_sink=snapshots.append,
    )
    sink = web_run._event_sink_for_request(request, Repository())
    assert sink is not None
    assert load_calls == {"observation": 0, "result": 0, "state": 0}

    sink(WorkerEvent("observation", "run-1", tasks[-1].task_id, {}))
    assert load_calls == {"observation": 900, "result": 900, "state": 900}
    after_initial_scan = dict(load_calls)

    sink(WorkerEvent("result", "run-1", tasks[-1].task_id, {}))
    assert {
        key: load_calls[key] - after_initial_scan[key]
        for key in load_calls
    } == {"observation": 1, "result": 1, "state": 1}
    assert len(snapshots) == 2


def test_checkpoint_failure_does_not_prevent_ui_event_delivery(tmp_path: Path) -> None:
    """Break caught: a failed partial publish hides a durable UI progress event."""
    from quote_app.services import web_run

    task = _task("checkpoint-failure")
    events: list[WorkerEvent] = []

    class Repository:
        def load_observation(self, _task_id: str) -> None:
            return None

        def load_result(self, _task_id: str) -> None:
            return None

        def task_state(self, _task_id: str) -> TaskState:
            return TaskState.PENDING

    request = web_run.WebsiteRunRequest(
        run_id="run-1",
        tasks=(task,),
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
        event_sink=events.append,
        checkpoint_sink=lambda _snapshot: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    sink = web_run._event_sink_for_request(request, Repository())
    assert sink is not None
    event = WorkerEvent("observation", "run-1", task.task_id, {})

    with pytest.raises(RuntimeError, match="boom"):
        sink(event)

    assert events == [event]


def test_service_surfaces_checkpoint_failure_before_showing_manual_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A swallowed scheduler event error must not hide a failed manual-wait flush."""
    from quote_app.services import web_run
    from quote_app.tasks.scheduler import ManualActionEvent

    task = _task("waiting-checkpoint", channel=WebsiteChannel.JD)
    events: list[WorkerEvent] = []
    manual_action_published = False

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def load_observation(self, _task_id: str) -> None:
            return None

        def load_result(self, _task_id: str) -> None:
            return None

        def task_state(self, _task_id: str) -> TaskState:
            return TaskState.WAITING_FOR_LOGIN

        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class Scheduler:
        waiting_action = ManualActionEvent(
            task=task,
            token=AttemptToken(task.task_id, 0, 1),
            site="京东",
            reason="需要人工完成安全验证",
        )

    class Runner:
        def __init__(self, **kwargs: object) -> None:
            self.scheduler = Scheduler()
            self.event_sink = kwargs["event_sink"]

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            try:
                self.event_sink(
                    WorkerEvent(
                        "waiting_for_login",
                        "run-1",
                        task.task_id,
                        {},
                    )
                )
            except RuntimeError:
                pass  # BrowserTaskScheduler records and swallows sink failures.
            return ()

    class Controller(web_run.WebsiteRunController):
        def publish_manual_action(self, action: ManualActionEvent) -> None:
            del action
            nonlocal manual_action_published
            manual_action_published = True
            raise AssertionError("manual action was shown before checkpoint flush")

    runtime = SimpleNamespace(
        evidence_capture=lambda: SimpleNamespace(capture=lambda _request: None),
        capture_context_provider=lambda *_args: None,
    )
    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)
    controller = Controller()
    request = web_run.WebsiteRunRequest(
        run_id="run-1",
        tasks=(task,),
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=tmp_path / "state.sqlite3",
        controller=controller,
        event_sink=events.append,
        checkpoint_sink=lambda _snapshot: (_ for _ in ()).throw(
            RuntimeError("processing workbook flush failed")
        ),
    )

    with pytest.raises(RuntimeError, match="processing workbook flush failed"):
        web_run.run_website_tasks(
            request,
            runtime_factory=lambda _registry: runtime,
        )

    assert manual_action_published is False
    assert controller.waiting_action is None
    assert [event.event for event in events] == ["waiting_for_login"]


def test_manual_action_controller_unblocks_worker_when_cancelled() -> None:
    from quote_app.tasks.scheduler import ManualActionEvent
    from quote_app.services.web_run import WebsiteRunController

    controller = WebsiteRunController()
    controller.publish_manual_action(
        ManualActionEvent(
            task=_task("waiting", channel=WebsiteChannel.JD),
            token=AttemptToken("waiting", 0, 1),
            site="京东",
            reason="需要人工完成安全验证",
        )
    )
    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(controller.wait_for_resolution()))
    worker.start()

    assert controller.cancel_manual_action() is True
    worker.join(timeout=1)

    assert result == [False]


def test_service_keeps_browser_open_until_manual_action_is_continued(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run
    from quote_app.tasks.scheduler import ManualActionEvent

    task = _task("waiting", channel=WebsiteChannel.TMALL)
    evidence = _evidence(tmp_path / "continued.png", b"formal")
    action_published = threading.Event()
    lifecycle: list[str] = []
    runner_calls = 0
    registry = object()
    runtime = SimpleNamespace(
        evidence_capture=lambda: "capture",
        capture_context_provider=lambda *_args: None,
    )

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def task_state(self, _task_id: str) -> TaskState:
            return TaskState.SUCCEEDED

        def close(self) -> None:
            lifecycle.append("repository-close")

    class Browser:
        def __init__(self, _profile: Path) -> None:
            pass

        def __enter__(self):
            lifecycle.append("browser-enter")
            return self

        def __exit__(self, *_args: object) -> None:
            lifecycle.append("browser-close")

    class Scheduler:
        def __init__(self) -> None:
            self.waiting_action = ManualActionEvent(
                task=task,
                token=AttemptToken(task.task_id, 0, 1),
                site="天猫",
                reason="需要人工完成安全验证",
            )

        def continue_current_task(self) -> None:
            self.waiting_action = None

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            self.scheduler = Scheduler()

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            nonlocal runner_calls
            runner_calls += 1
            if runner_calls == 1:
                action_published.set()
                return ()
            return (_success(task, evidence),)

    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)
    monkeypatch.setattr(web_run, "default_registry", lambda: registry)
    controller = web_run.WebsiteRunController()
    request = _request(tmp_path, (task,))
    request = web_run.WebsiteRunRequest(
        run_id=request.run_id,
        tasks=request.tasks,
        profile_dir=request.profile_dir,
        evidence_dir=request.evidence_dir,
        database_path=request.database_path,
        controller=controller,
    )
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(
            web_run.run_website_tasks(request, runtime_factory=lambda _registry: runtime)
        )
    )
    worker.start()
    assert action_published.wait(1)
    for _ in range(50):
        if controller.waiting_action is not None:
            break
        time.sleep(0.01)

    assert controller.waiting_action is not None
    assert lifecycle == ["browser-enter"]
    controller.continue_current_task()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert runner_calls == 2
    assert lifecycle == ["browser-enter", "browser-close", "repository-close"]
    assert result[0].succeeded == 1


def test_service_constructs_one_registry_browser_runtime_and_serial_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    task = _task("success")
    evidence = _evidence(tmp_path / "success.png", b"formal")
    result = _success(task, evidence)
    lifecycle: list[str] = []
    registry = object()
    runtime = SimpleNamespace(
        evidence_capture=lambda: "capture",
        capture_context_provider=lambda *_args: None,
    )

    class Repository:
        def __init__(self, database_path: Path) -> None:
            assert database_path == tmp_path / "state.sqlite3"
            lifecycle.append("repository")

        def task_state(self, task_id: str) -> TaskState:
            assert task_id == task.task_id
            return TaskState.SUCCEEDED

        def close(self) -> None:
            lifecycle.append("repository-close")

    class Browser:
        def __init__(self, profile_dir: Path) -> None:
            assert profile_dir == tmp_path / "profile"
            lifecycle.append("browser")

        def __enter__(self):
            lifecycle.append("browser-enter")
            return self

        def __exit__(self, *_exc_info: object) -> None:
            lifecycle.append("browser-close")

    runners: list[object] = []

    class Runner:
        def __init__(self, **kwargs: object) -> None:
            runners.append(self)
            assert kwargs["repository"].__class__ is Repository
            assert kwargs["browser_session"].__class__ is Browser
            assert kwargs["adapter_registry"] is registry
            assert kwargs["capture_context_provider"] is runtime.capture_context_provider
            assert kwargs["evidence_capture"] == "capture"
            assert kwargs["stop_after_brand_issue"] is False

        def run(self, tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            assert tasks == (task,)
            return (result,)

    registry_calls: list[object] = []

    def make_registry() -> object:
        registry_calls.append(registry)
        return registry

    runtime_registries: list[object] = []

    def runtime_factory(received_registry: object):
        runtime_registries.append(received_registry)
        return runtime

    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)
    monkeypatch.setattr(web_run, "default_registry", make_registry)

    summary = web_run.run_website_tasks(
        _request(tmp_path, (task,)),
        runtime_factory=runtime_factory,
    )

    assert len(runners) == 1
    assert registry_calls == [registry]
    assert runtime_registries == [registry]
    assert summary.succeeded == 1
    assert lifecycle == [
        "repository",
        "browser",
        "browser-enter",
        "browser-close",
        "repository-close",
    ]


def test_service_darwin_beta_uses_manual_window_session_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.evidence.macos_runtime import MacFormalCaptureRuntime
    from quote_app.evidence.models import MacCapturePolicy
    from quote_app.services import web_run

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    browser_options: list[dict[str, object]] = []

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _profile_dir: Path, **kwargs: object) -> None:
            browser_options.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            pass

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            return ()

    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)

    summary = web_run.run_website_tasks(
        _request(tmp_path, ()),
        runtime_factory=lambda _registry: MacFormalCaptureRuntime(
            policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        ),
    )

    assert summary.succeeded == 0
    assert browser_options == [
        {
            "launch_args": (
                "--window-position=24,49",
                "--window-size=1464,893",
            )
        }
    ]


def test_service_preserves_partial_results_login_and_authoritative_failure_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    success = _task("success", output_row=2)
    waiting = _task("waiting", output_row=3)
    permission = _task("permission", output_row=4)
    evidence = _evidence(tmp_path / "success.png", b"validated")
    results = (
        _success(success, evidence),
        _failure(permission, "CAPTURE_PERMISSION"),
    )
    states = {
        success.task_id: TaskState.SUCCEEDED,
        waiting.task_id: TaskState.WAITING_FOR_LOGIN,
        permission.task_id: TaskState.TECHNICAL_FAILURE,
    }

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def task_state(self, task_id: str) -> TaskState:
            return states[task_id]

        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            pass

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            return results

    registry = object()
    runtime = SimpleNamespace(
        evidence_capture=lambda: SimpleNamespace(capture=lambda _request: None),
        capture_context_provider=lambda *_args: None,
    )
    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)
    monkeypatch.setattr(web_run, "default_registry", lambda: registry)

    summary = web_run.run_website_tasks(
        _request(tmp_path, (success, waiting, permission)),
        runtime_factory=lambda received: runtime if received is registry else None,
    )

    assert summary.succeeded == 1
    assert summary.waiting_for_login == 1
    assert summary.technical_failure == 1
    assert summary.waiting_sites == ("jd",)
    assert summary.technical_failure_codes == ("CAPTURE_PERMISSION",)
    assert summary.evidence_paths == (evidence.path,)


def test_summary_excludes_missing_or_hash_mismatched_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    valid_task = _task("valid", output_row=2)
    missing_task = _task("missing", output_row=3)
    changed_task = _task("changed", output_row=4)
    valid = _success(valid_task, _evidence(tmp_path / "valid.png", b"valid"))
    missing_evidence = _evidence(tmp_path / "missing.png", b"missing")
    missing = _success(missing_task, missing_evidence)
    missing_evidence.path.unlink()
    changed_evidence = _evidence(tmp_path / "changed.png", b"before")
    changed = _success(changed_task, changed_evidence)
    changed_evidence.path.write_bytes(b"after")
    results = (valid, missing, changed)

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def task_state(self, _task_id: str) -> TaskState:
            return TaskState.SUCCEEDED

        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            pass

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            return results

    runtime = SimpleNamespace(
        evidence_capture=lambda: SimpleNamespace(capture=lambda _request: None),
        capture_context_provider=lambda *_args: None,
    )
    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)

    summary = web_run.run_website_tasks(
        _request(tmp_path, (valid_task, missing_task, changed_task)),
        runtime_factory=lambda _registry: runtime,
    )

    assert summary.succeeded == 3
    assert valid.evidence is not None
    assert summary.evidence_paths == (valid.evidence.path,)


def test_service_closes_browser_and_repository_when_runner_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    closed: list[str] = []

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def close(self) -> None:
            closed.append("repository")

    class Browser:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            closed.append("browser")

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            raise RuntimeError("runner failed")

    runtime = SimpleNamespace(
        evidence_capture=lambda: SimpleNamespace(capture=lambda _request: None),
        capture_context_provider=lambda *_args: None,
    )
    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)

    with pytest.raises(RuntimeError, match="runner failed"):
        web_run.run_website_tasks(
            _request(tmp_path, (_task("task"),)),
            runtime_factory=lambda _registry: runtime,
        )

    assert closed == ["browser", "repository"]


@pytest.mark.parametrize("mutation", ["replace", "delete"])
def test_validated_evidence_rejects_path_mutation_after_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    from quote_app.services import web_run

    task = _task("race")
    result = _success(task, _evidence(tmp_path / "race.png", b"same-bytes"))
    mutated = False

    def mutate_then_lstat(path: Path) -> os.stat_result:
        nonlocal mutated
        mutated = True
        path.unlink()
        if mutation == "replace":
            path.write_bytes(b"same-bytes")
        return os.lstat(path)

    monkeypatch.setattr(web_run, "_lstat_evidence_path", mutate_then_lstat, raising=False)

    assert web_run._validated_evidence_path(result) is None
    assert mutated


def test_validated_evidence_rejects_symlink_even_when_target_matches(
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    target = tmp_path / "target.png"
    target.write_bytes(b"formal")
    link = tmp_path / "linked.png"
    link.symlink_to(target)
    evidence = EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=link,
        sha256=hashlib.sha256(b"formal").hexdigest(),
        pixel_width=800,
        pixel_height=600,
        captured_at=NOW,
        validation_code="CAPTURE_OK",
    )

    assert web_run._validated_evidence_path(_success(_task("symlink"), evidence)) is None


def test_service_binds_beta_acceptance_to_a_beta_runtime_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catches a beta runtime being summarized or handed to the runner as strict."""
    from dataclasses import replace
    from quote_app.services import web_run

    task = _task("mac-beta")
    strict_evidence = _evidence(tmp_path / "mac-beta.png", b"formal")
    beta_evidence = replace(
        strict_evidence,
        validation_code="CAPTURE_OK_MAC_VISUAL_REVIEW",
    )
    result = _success(task, beta_evidence)
    runner_options: dict[str, object] = {}

    class Repository:
        def __init__(self, _path: Path) -> None:
            pass

        def task_state(self, _task_id: str) -> TaskState:
            return TaskState.SUCCEEDED

        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            pass

    class Runner:
        def __init__(self, **kwargs: object) -> None:
            runner_options.update(kwargs)

        def run(self, _tasks: tuple[WebsiteTask, ...]) -> tuple[WebsiteResult, ...]:
            return (result,)

    capture = SimpleNamespace(policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA)
    runtime = SimpleNamespace(
        evidence_capture=lambda: capture,
        capture_context_provider=lambda *_args: None,
    )
    monkeypatch.setattr(web_run, "SQLiteTaskRepository", Repository)
    monkeypatch.setattr(web_run, "PersistentBrowserSession", Browser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", Runner)

    summary = web_run.run_website_tasks(
        _request(tmp_path, (task,)),
        runtime_factory=lambda _registry: runtime,
    )

    assert runner_options["capture_acceptance_policy"] is (
        MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
    )
    assert summary.evidence_paths == (beta_evidence.path,)

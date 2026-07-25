from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.browser.worker import WorkerEvent
from quote_app.domain.models import QuoteMonth
from quote_app.evidence.geometry import CssRect
from quote_app.evidence.models import EvidenceRecord, EvidenceRectangle
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureRequest,
    PlatformEvidenceCapture,
)
from quote_app.tasks.models import (
    SCHEMA_VERSION,
    BusinessOutcome,
    InputFingerprint,
    RunRecord,
    RunState,
    TaskState,
    WebsiteChannel,
    WebsiteTask,
)
from quote_app.tasks.repository import SQLiteTaskRepository
from quote_app.tasks.retry import (
    LoginRequired,
    NonRetryableTechnicalError,
    RetryPolicy,
)
from quote_app.tasks.runner import FixtureObservation, WebsiteTaskRunner

NOW = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
CHANNELS = (
    WebsiteChannel.JD,
    WebsiteChannel.TMALL,
    WebsiteChannel.OFFICIAL,
)
TECHNICAL_FAILURE_IDS = frozenset(
    {
        "row-0023-jd",
        "row-0047-official",
        "row-0071-jd",
    }
)
LOGIN_TASK_ID = "row-0017-tmall"
INTERRUPT_AFTER_CAPTURES = 60


class _StableProbe:
    def semantic_hash(self) -> str:
        return "workload-stable"


class _SinglePageSession:
    def __init__(self) -> None:
        self.calls = 0
        self.pages: dict[str, object] = {}

    def page_for(self, site_family: str) -> object:
        self.calls += 1
        return self.pages.setdefault(site_family, object())


class _InterruptingCapture(PlatformEvidenceCapture):
    def __init__(self) -> None:
        self.capture_count = 0
        self.interrupted = False

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        self.capture_count += 1
        if (
            not self.interrupted
            and self.capture_count == INTERRUPT_AFTER_CAPTURES
        ):
            self.interrupted = True
            raise KeyboardInterrupt
        payload = (
            f"{request.destination.name}:{request.state.value}"
        ).encode()
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(payload)
        annotations = tuple(
            EvidenceRectangle(
                role=rectangle.role,
                x=10 + index * 30,
                y=20 + index * 30,
                width=100,
                height=40,
            )
            for index, rectangle in enumerate(request.css_rectangles)
        )
        return EvidenceRecord(
            state=request.state,
            path=request.destination,
            sha256=hashlib.sha256(payload).hexdigest(),
            pixel_width=1280,
            pixel_height=800,
            captured_at=NOW,
            validation_code="CAPTURE_OK",
            annotations=annotations,
        )


def _run_record(data_dir: Path) -> RunRecord:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=data_dir / "input" / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=10,
            modified_ns=100 + index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), 1)
    )
    return RunRecord(
        run_id="workload-900",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=data_dir / "output",
        browser_profile_dir=data_dir / "browser-profile",
        associated_rows_snapshot="[]",
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256=hashlib.sha256(b"[]").hexdigest(),
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )


def _tasks() -> tuple[WebsiteTask, ...]:
    tasks: list[WebsiteTask] = []
    for row_index in range(300):
        output_row = row_index + 2
        query_group = row_index % 10
        for channel in CHANNELS:
            tasks.append(
                WebsiteTask(
                    task_id=f"row-{row_index:04d}-{channel.value}",
                    run_id="workload-900",
                    source_row_number=output_row + 1000,
                    output_row_number=output_row,
                    material_code=f"DUP-{row_index // 2:04d}",
                    brand=("小米", "华为", "荣耀")[query_group % 3],
                    model_name=f"重复机型-{query_group:02d}",
                    ram=("8GB", "12GB")[query_group % 2],
                    storage=("256GB", "512GB")[query_group % 2],
                    color=("黑色", "白色")[query_group % 2],
                    channel=channel,
                )
            )
    return tuple(tasks)


def _observation(
    task: WebsiteTask,
    _page: object,
) -> FixtureObservation:
    if task.task_id == LOGIN_TASK_ID:
        raise LoginRequired("tmall", "需要人工登录")
    if task.task_id in TECHNICAL_FAILURE_IDS:
        raise NonRetryableTechnicalError(
            "FIXTURE_TECHNICAL_FAILURE",
            "固定技术失败",
        )
    row_index = int(task.task_id.split("-", 2)[1])
    outcome = tuple(BusinessOutcome)[row_index % 5]
    cases = {
        BusinessOutcome.PRICE_FOUND: (
            Decimal("3999.00"),
            (),
            (),
        ),
        BusinessOutcome.NO_MODEL: (
            None,
            (
                CssRect(10, 10, 120, 30, "search_keyword"),
                CssRect(10, 60, 400, 200, "result_region"),
            ),
            ("search_keyword", "result_region"),
        ),
        BusinessOutcome.CAPACITY_UNAVAILABLE: (
            None,
            (CssRect(20, 100, 140, 40, "capacity"),),
            ("capacity",),
        ),
        BusinessOutcome.COLOR_UNAVAILABLE: (
            None,
            (CssRect(180, 100, 140, 40, "color"),),
            ("color",),
        ),
        BusinessOutcome.SOLD_OUT: (
            None,
            (CssRect(20, 200, 160, 40, "stock_status"),),
            ("stock_status",),
        ),
    }
    price, rectangles, roles = cases[outcome]
    return FixtureObservation(
        outcome=outcome,
        price=price,
        url=f"https://fixture.test/{task.task_id}",
        css_rectangles=rectangles,
        expected_roles=roles,
        expected_window=BrowserWindowIdentity(
            "fixture",
            900,
            "fixture-window",
        ),
        stability_probe=_StableProbe(),
    )


def _runner(
    repository: SQLiteTaskRepository,
    data_dir: Path,
    session: _SinglePageSession,
    capture: _InterruptingCapture,
    events: list[WorkerEvent],
) -> WebsiteTaskRunner:
    def diagnostic(
        task: WebsiteTask,
        _error: BaseException,
        path: Path,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"diagnostic:{task.task_id}".encode())
        return path

    return WebsiteTaskRunner(
        repository=repository,
        run_id="workload-900",
        browser_session=session,
        adapter=_observation,
        evidence_capture=capture,
        evidence_dir=data_dir / "evidence",
        diagnostic_capture=diagnostic,
        retry_policy=RetryPolicy(
            technical_retries=0,
            retry_delay_seconds=0,
        ),
        event_sink=events.append,
    )


def _assert_under(path: Path, parent: Path) -> None:
    path.resolve().relative_to(parent.resolve())


def test_900_task_workload_restart_audit_and_locality(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "local-application-data"
    data_dir.mkdir()
    database = data_dir / "state.sqlite3"
    tasks = _tasks()
    ordered_tasks = tuple(
        sorted(
            tasks,
            key=lambda task: (
                task.brand,
                task.model_name,
                task.ram,
                task.storage,
                task.color,
                task.channel.value,
                task.output_row_number,
            ),
        )
    )
    login_position = next(
        index
        for index, task in enumerate(ordered_tasks)
        if task.task_id == LOGIN_TASK_ID
    )
    blocked_tmall_ids = {
        task.task_id
        for task in ordered_tasks[login_position + 1 :]
        if task.channel is WebsiteChannel.TMALL
    }
    assert len(tasks) == 900
    assert len({task.task_id for task in tasks}) == 900
    assert len({task.material_code for task in tasks}) == 150
    assert len(
        {
            (
                task.brand,
                task.model_name,
                task.ram,
                task.storage,
                task.color,
            )
            for task in tasks
        }
    ) == 10

    first_session = _SinglePageSession()
    first_capture = _InterruptingCapture()
    events: list[WorkerEvent] = []
    started = time.perf_counter()
    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_run_record(data_dir))
        runner = _runner(
            repository,
            data_dir,
            first_session,
            first_capture,
            events,
        )
        with pytest.raises(KeyboardInterrupt):
            runner.run(tuple(reversed(tasks)))

        successful_before_restart = {
            task.task_id: (
                repository.attempt_count(task.task_id),
                repository.load_result(task.task_id),
                repository.load_result(task.task_id).evidence.path.stat().st_mtime_ns,
                repository.load_result(task.task_id).evidence.sha256,
            )
            for task in tasks
            if repository.task_state(task.task_id) is TaskState.SUCCEEDED
        }
        running = tuple(
            task.task_id
            for task in tasks
            if repository.task_state(task.task_id) is TaskState.RUNNING
        )
        assert len(running) == 1
        assert len(successful_before_restart) > 0

    with SQLiteTaskRepository(database) as repository:
        assert all(
            repository.task_state(task_id) is TaskState.PENDING
            for task_id in running
        )
        assert all(
            repository.latest_attempt(task_id).error_code
            == "PROCESS_INTERRUPTED"
            for task_id in running
        )
        second_session = _SinglePageSession()
        second_capture = _InterruptingCapture()
        second_capture.interrupted = True
        runner = _runner(
            repository,
            data_dir,
            second_session,
            second_capture,
            events,
        )
        runner.run(tasks)

        assert all(
            repository.load_task(task.task_id) == task for task in tasks
        )
        assert all(
            repository.attempt_count(task_id) == snapshot[0]
            and repository.load_result(task_id) == snapshot[1]
            and repository.load_result(task_id).evidence.path.stat().st_mtime_ns
            == snapshot[2]
            and repository.load_result(task_id).evidence.sha256 == snapshot[3]
            for task_id, snapshot in successful_before_restart.items()
        )
        assert {
            task.task_id for task in repository.select_waiting("workload-900")
        } == {LOGIN_TASK_ID}
        assert {
            task.task_id for task in repository.select_failed("workload-900")
        } == TECHNICAL_FAILURE_IDS
        assert {
            task.task_id for task in repository.select_pending("workload-900")
        } == blocked_tmall_ids
        assert all(
            repository.task_state(task.task_id) is TaskState.SUCCEEDED
            for task in tasks
            if task.task_id
            not in (
                TECHNICAL_FAILURE_IDS
                | {LOGIN_TASK_ID}
                | blocked_tmall_ids
            )
        )
        assert all(
            repository.task_state(task.task_id) is not TaskState.PENDING
            for task in tasks
            if task.channel is not WebsiteChannel.TMALL
        )
        assert repository.audit_evidence("workload-900") == ()

        persisted_outcomes: Counter[BusinessOutcome] = Counter()
        for task in tasks:
            result = repository.load_result(task.task_id)
            if result is None:
                assert task.task_id in blocked_tmall_ids | {LOGIN_TASK_ID}
                continue
            if result.evidence is not None:
                persisted_outcomes[result.outcome] += 1
                _assert_under(result.evidence.path, data_dir)
                assert (
                    hashlib.sha256(result.evidence.path.read_bytes()).hexdigest()
                    == result.evidence.sha256
                )
            if result.diagnostic_path is not None:
                _assert_under(result.diagnostic_path, data_dir)
        expected_outcomes = Counter(
            tuple(BusinessOutcome)[
                int(task.task_id.split("-", 2)[1]) % 5
            ]
            for task in tasks
            if task.task_id
            not in TECHNICAL_FAILURE_IDS
            | {LOGIN_TASK_ID}
            | blocked_tmall_ids
        )
        assert persisted_outcomes == expected_outcomes

        for task_id in TECHNICAL_FAILURE_IDS:
            result = repository.load_result(task_id)
            assert result is not None
            assert result.state is TaskState.TECHNICAL_FAILURE
            assert result.evidence is None
            assert result.outcome is None
            assert result.diagnostic_path is not None
            _assert_under(
                result.diagnostic_path,
                data_dir / "evidence" / "diagnostics",
            )

        persisted_run = repository.load_run("workload-900")
        assert persisted_run.associated_rows_snapshot_path is None
        assert persisted_run.quote_path is None
        assert persisted_run.report_path is None
        for path in (
            persisted_run.output_dir,
            persisted_run.browser_profile_dir,
            *(item.path for item in persisted_run.input_fingerprints),
        ):
            _assert_under(path, data_dir)

    elapsed_seconds = time.perf_counter() - started
    database_size_bytes = database.stat().st_size
    evidence_files = tuple((data_dir / "evidence").glob("*.png"))
    evidence_total_bytes = sum(path.stat().st_size for path in evidence_files)
    capture_count = first_capture.capture_count + second_capture.capture_count
    assert elapsed_seconds < 120
    assert database_size_bytes < 64 * 1024 * 1024
    _assert_under(database, data_dir)
    assert set(first_session.pages) | set(second_session.pages) == {
        "jd",
        "tmall",
        "official:小米",
        "official:华为",
        "official:荣耀",
    }
    waiting_events = [
        event for event in events if event.event == "waiting_for_login"
    ]
    assert len(waiting_events) == 1
    assert waiting_events[0].task_id == LOGIN_TASK_ID
    assert dict(waiting_events[0].data) == {
        "site": "tmall",
        "condition": "login_required",
    }
    assert sum(event.event == "technical_failure" for event in events) == 3
    successful_count = (
        900
        - len(TECHNICAL_FAILURE_IDS)
        - 1
        - len(blocked_tmall_ids)
    )
    assert sum(event.event == "result" for event in events) == successful_count
    assert first_capture.interrupted is True
    assert len(evidence_files) == successful_count
    disk = shutil.disk_usage(tmp_path)
    outcome_counts = ",".join(
        f"{outcome.value}:{persisted_outcomes[outcome]}"
        for outcome in BusinessOutcome
    )
    print(
        "WORKLOAD_METRICS "
        f"tasks=900 interruption_capture={INTERRUPT_AFTER_CAPTURES} "
        f"elapsed_seconds={elapsed_seconds:.3f} "
        f"database_bytes={database_size_bytes} "
        f"capture_count={capture_count} "
        f"succeeded={successful_count} "
        f"technical_failed={len(TECHNICAL_FAILURE_IDS)} "
        f"waiting_login=1 pending_same_site={len(blocked_tmall_ids)} "
        f"evidence_files={len(evidence_files)} "
        f"evidence_bytes={evidence_total_bytes} "
        f"python={platform.python_version()} "
        f"os={platform.system()}-{platform.release()} "
        f"temp_disk_free_bytes={disk.free} executable={sys.executable} "
        f"outcome_counts={outcome_counts}"
    )

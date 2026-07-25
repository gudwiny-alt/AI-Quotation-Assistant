from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import EvidenceRecord, EvidenceRectangle, EvidenceState
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
from quote_app.tasks.repository import (
    AttemptToken,
    RepositoryError,
    ResultHistoryRecord,
    SQLiteTaskRepository,
)

NOW = datetime(2026, 7, 25, 9, tzinfo=timezone.utc)


def _run(tmp_path: Path, *, run_id: str = "run-1") -> RunRecord:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=100 + index,
            modified_ns=1_000 + index,
        )
        for index, role in enumerate(("base", "marketing", "bop"), start=1)
    )
    return RunRecord(
        run_id=run_id,
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot='{"rows":[{"material_code":"000123"}]}',
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=tmp_path / "output" / "quote.xlsx",
        report_path=None,
        state=RunState.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )


def _task(*, task_id: str = "task-1", run_id: str = "run-1") -> WebsiteTask:
    return WebsiteTask(
        task_id=task_id,
        run_id=run_id,
        source_row_number=7,
        output_row_number=2,
        material_code="000123",
        brand="小米",
        model_name="Xiaomi 17 Pro",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.JD,
    )


def _success_result(
    tmp_path: Path,
    *,
    task_id: str = "task-1",
    evidence_bytes: bytes = b"formal evidence",
) -> WebsiteResult:
    evidence_path = tmp_path / f"{task_id}-evidence.png"
    evidence_path.write_bytes(evidence_bytes)
    evidence = EvidenceRecord(
        state=EvidenceState.NORMAL,
        path=evidence_path,
        sha256=hashlib.sha256(evidence_bytes).hexdigest(),
        pixel_width=2560,
        pixel_height=1600,
        captured_at=NOW,
        validation_code="CAPTURE_OK",
        annotations=(EvidenceRectangle("price", 100, 100, 320, 80),),
    )
    return WebsiteResult(
        task_id=task_id,
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("3999.00"),
        url="https://example.test/product",
        evidence=evidence,
        diagnostic_path=tmp_path / f"{task_id}-diagnostic.png",
        error_code=None,
        error_message=None,
    )


def _failure_result(
    tmp_path: Path,
    *,
    task_id: str = "task-1",
) -> WebsiteResult:
    return WebsiteResult(
        task_id=task_id,
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url=None,
        evidence=None,
        diagnostic_path=tmp_path / f"{task_id}-diagnostic.png",
        error_code="LAYOUT_CHANGED",
        error_message="页面结构发生变化",
    )


def _seed_task(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
    *,
    task: WebsiteTask | None = None,
) -> WebsiteTask:
    repository.create_run(_run(tmp_path))
    selected = task or _task()
    repository.upsert_task(selected)
    return selected


def _save(
    repository: SQLiteTaskRepository,
    result: WebsiteResult,
) -> AttemptToken:
    token = repository.start_attempt(result.task_id, started_at=NOW)
    repository.save_result(
        result,
        token=token,
        finished_at=NOW + timedelta(minutes=1),
    )
    return token


@pytest.fixture
def repository(tmp_path: Path):
    value = SQLiteTaskRepository(tmp_path / "state.sqlite3")
    try:
        yield value
    finally:
        value.close()


def test_run_task_and_result_round_trip_after_reopening(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    run = _run(tmp_path)
    task = _task()
    result = _success_result(tmp_path)
    with SQLiteTaskRepository(database) as first:
        first.create_run(run)
        first.upsert_task(task)
        _save(first, result)

    with SQLiteTaskRepository(database) as reopened:
        assert reopened.load_run(run.run_id) == run
        assert reopened.load_task(task.task_id) == task
        assert reopened.load_result(task.task_id) == result
        assert reopened.task_state(task.task_id) is TaskState.SUCCEEDED


def test_run_preserves_exact_fingerprints_and_snapshot_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    run = _run(tmp_path)
    with SQLiteTaskRepository(database) as first:
        first.create_run(run)
    with SQLiteTaskRepository(database) as reopened:
        assert reopened.load_run(run.run_id) == run


def test_every_connection_enables_safety_pragmas(tmp_path: Path) -> None:
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        assert repository.connection_pragmas() == {
            "foreign_keys": 1,
            "journal_mode": "wal",
            "busy_timeout": 5000,
        }


def test_same_task_id_with_different_payload_requires_higher_generation(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)

    with pytest.raises(RepositoryError, match="必须显式使用更大的任务代次"):
        repository.upsert_task(replace(_task(), color="白色"))


def test_attempt_token_is_persisted_before_external_work_and_running_cannot_restart(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)

    token = repository.start_attempt("task-1", started_at=NOW)

    assert token == AttemptToken("task-1", generation=0, attempt_number=1)
    assert repository.attempt_count("task-1") == 1
    assert repository.task_state("task-1") is TaskState.RUNNING
    with pytest.raises(RepositoryError, match="当前状态不能开始新的尝试"):
        repository.start_attempt("task-1")


@pytest.mark.parametrize(
    "token",
    [
        AttemptToken("other-task", generation=0, attempt_number=1),
        AttemptToken("task-1", generation=1, attempt_number=1),
        AttemptToken("task-1", generation=0, attempt_number=2),
    ],
)
def test_save_result_rejects_wrong_task_generation_or_attempt_token(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
    token: AttemptToken,
) -> None:
    _seed_task(repository, tmp_path)
    repository.start_attempt("task-1", started_at=NOW)

    with pytest.raises(RepositoryError, match="尝试令牌"):
        repository.save_result(_failure_result(tmp_path), token=token)

    assert repository.load_result("task-1") is None
    assert repository.task_state("task-1") is TaskState.RUNNING


def test_paused_task_rejects_late_result_from_pre_pause_attempt(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)
    token = repository.start_attempt("task-1")
    repository.pause_task("task-1")

    with pytest.raises(RepositoryError, match="只有正在执行的任务才能保存结果"):
        repository.save_result(_failure_result(tmp_path), token=token)

    assert repository.task_state("task-1") is TaskState.PAUSED
    assert repository.load_result("task-1") is None


def test_save_result_rolls_back_both_writes_on_injected_failure(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)
    token = repository.start_attempt("task-1")

    def fail_between_writes() -> None:
        raise RuntimeError("forced transition failure")

    with pytest.raises(RuntimeError, match="forced transition failure"):
        repository.save_result(
            _failure_result(tmp_path),
            token=token,
            _after_result_write=fail_between_writes,
        )

    assert repository.load_result("task-1") is None
    assert repository.task_state("task-1") is TaskState.RUNNING
    assert repository.latest_attempt("task-1").finished_at is None


def test_failure_diagnostic_stays_in_attempt_and_history_after_successful_retry(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)
    failure = _failure_result(tmp_path)
    first_token = repository.start_attempt("task-1", started_at=NOW)
    repository.save_result(
        failure,
        token=first_token,
        finished_at=NOW + timedelta(minutes=1),
    )

    first_attempt = repository.latest_attempt("task-1")
    assert first_attempt is not None
    assert first_attempt.error_code == "LAYOUT_CHANGED"
    assert first_attempt.error_message == "页面结构发生变化"

    second_token = repository.start_attempt(
        "task-1",
        started_at=NOW + timedelta(minutes=2),
    )
    success = _success_result(tmp_path)
    repository.save_result(
        success,
        token=second_token,
        finished_at=NOW + timedelta(minutes=3),
    )

    assert repository.load_result("task-1") == success
    history = repository.result_history("task-1")
    assert len(history) == 1
    assert history[0].result == failure
    assert history[0].generation == 0
    assert history[0].reason == "RETRY_RESULT_REPLACED"


def test_upsert_and_resume_never_reset_succeeded_task(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    task = _seed_task(repository, tmp_path)
    result = _success_result(tmp_path)
    _save(repository, result)

    repository.upsert_task(task)
    repository.resume_task(task.task_id)

    assert repository.task_state(task.task_id) is TaskState.SUCCEEDED
    assert repository.load_result(task.task_id) == result


def test_pending_failed_waiting_and_success_selections_are_disjoint(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    repository.create_run(_run(tmp_path))
    tasks = {
        task_id: _task(task_id=task_id)
        for task_id in ("pending", "failed", "waiting-jd", "waiting-tmall", "success")
    }
    for task in tasks.values():
        repository.upsert_task(task)
    _save(repository, _failure_result(tmp_path, task_id="failed"))
    jd_token = repository.start_attempt("waiting-jd")
    repository.mark_waiting_for_login("waiting-jd", token=jd_token, site="京东")
    tmall_token = repository.start_attempt("waiting-tmall")
    repository.mark_waiting_for_login(
        "waiting-tmall",
        token=tmall_token,
        site="天猫",
    )
    _save(repository, _success_result(tmp_path, task_id="success"))

    assert [task.task_id for task in repository.select_pending("run-1")] == ["pending"]
    assert [task.task_id for task in repository.select_failed("run-1")] == ["failed"]
    assert [task.task_id for task in repository.select_waiting("run-1")] == [
        "waiting-jd",
        "waiting-tmall",
    ]
    assert [
        task.task_id for task in repository.select_waiting("run-1", site="京东")
    ] == ["waiting-jd"]


def test_waiting_transition_finishes_attempt_and_requeues_only_matching_site(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    repository.create_run(_run(tmp_path))
    for task_id in ("jd-1", "jd-2", "tmall"):
        repository.upsert_task(_task(task_id=task_id))
        token = repository.start_attempt(task_id, started_at=NOW)
        repository.mark_waiting_for_login(task_id, token=token, site=(
            "京东" if task_id.startswith("jd") else "天猫"
        ))

    attempt = repository.latest_attempt("jd-1")
    assert attempt is not None
    assert attempt.finished_at is not None
    assert attempt.error_code == "LOGIN_REQUIRED"
    assert attempt.error_message == "网站要求登录或安全验证，任务已暂停等待人工处理"

    requeued = repository.requeue_waiting_site("run-1", "京东")

    assert [task.task_id for task in requeued] == ["jd-1", "jd-2"]
    assert repository.task_state("jd-1") is TaskState.PENDING
    assert repository.task_state("jd-2") is TaskState.PENDING
    assert repository.task_state("tmall") is TaskState.WAITING_FOR_LOGIN


def test_waiting_transition_rejects_stale_token_and_protects_paused_task(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)
    token = repository.start_attempt("task-1")
    repository.pause_task("task-1")

    with pytest.raises(RepositoryError, match="只有正在执行的任务才能等待登录"):
        repository.mark_waiting_for_login("task-1", token=token, site="京东")

    assert repository.task_state("task-1") is TaskState.PAUSED


def test_higher_generation_resets_retry_budget_and_archives_success(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    original = _seed_task(repository, tmp_path)
    success = _success_result(tmp_path)
    _save(repository, success)

    replacement = replace(original, color="白色")
    repository.upsert_task(replacement, generation=1)

    assert repository.load_task("task-1") == replacement
    assert repository.task_state("task-1") is TaskState.PENDING
    assert repository.task_generation("task-1") == 1
    assert repository.attempt_count("task-1") == 0
    assert repository.load_result("task-1") is None
    attempts = repository.list_attempts("task-1")
    assert len(attempts) == 1
    assert attempts[0].generation == 0
    assert attempts[0].attempt_number == 1
    history = repository.result_history("task-1")
    assert history == (
        ResultHistoryRecord(
            task_id="task-1",
            generation=0,
            result=success,
            reason="GENERATION_REPLACED",
            archived_at=history[0].archived_at,
        ),
    )

    next_token = repository.start_attempt("task-1")
    assert next_token == AttemptToken("task-1", generation=1, attempt_number=1)


def test_explicit_higher_generation_requeries_same_payload_and_rejects_old_token(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    original = _seed_task(repository, tmp_path)
    old_token = repository.start_attempt("task-1")
    repository.save_result(
        _success_result(tmp_path),
        token=old_token,
    )

    repository.upsert_task(original, generation=1)
    current_token = repository.start_attempt("task-1")

    with pytest.raises(RepositoryError, match="尝试令牌已过期"):
        repository.save_result(
            _failure_result(tmp_path),
            token=old_token,
        )

    assert current_token == AttemptToken("task-1", generation=1, attempt_number=1)
    assert repository.task_state("task-1") is TaskState.RUNNING
    assert repository.load_result("task-1") is None
    assert len(repository.result_history("task-1")) == 1


def test_running_task_rejects_generation_change_without_mutating_attempt(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    original = _seed_task(repository, tmp_path)
    token = repository.start_attempt("task-1", started_at=NOW)

    with pytest.raises(
        RepositoryError,
        match="任务正在执行，不能切换任务代次",
    ):
        repository.upsert_task(
            replace(original, color="白色"),
            generation=1,
        )

    assert repository.load_task("task-1") == original
    assert repository.task_generation("task-1") == 0
    assert repository.attempt_count("task-1") == 1
    assert repository.task_state("task-1") is TaskState.RUNNING
    assert repository.load_result("task-1") is None
    assert repository.result_history("task-1") == ()
    attempt = repository.latest_attempt("task-1")
    assert attempt is not None
    assert attempt.generation == token.generation
    assert attempt.attempt_number == token.attempt_number
    assert attempt.finished_at is None


def test_generation_replacement_rolls_back_history_result_and_task_together(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    original = _seed_task(repository, tmp_path)
    success = _success_result(tmp_path)
    _save(repository, success)

    def fail_between_writes() -> None:
        raise RuntimeError("forced generation failure")

    with pytest.raises(RuntimeError, match="forced generation failure"):
        repository.upsert_task(
            replace(original, color="白色"),
            generation=1,
            _after_result_delete=fail_between_writes,
        )

    assert repository.load_task("task-1") == original
    assert repository.load_result("task-1") == success
    assert repository.result_history("task-1") == ()
    assert repository.task_state("task-1") is TaskState.SUCCEEDED
    assert repository.task_generation("task-1") == 0
    assert repository.attempt_count("task-1") == 1


@pytest.mark.parametrize("damage", ["missing", "hash_mismatch"])
def test_evidence_audit_archives_success_then_preserves_diagnostic_on_failure(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
    damage: str,
) -> None:
    _seed_task(repository, tmp_path)
    diagnostic = tmp_path / "task-1-diagnostic.png"
    diagnostic.write_bytes(b"diagnostic must remain")
    success = _success_result(tmp_path)
    _save(repository, success)
    if damage == "missing":
        success.evidence.path.unlink()
    else:
        success.evidence.path.write_bytes(b"tampered")

    findings = repository.audit_evidence("run-1")

    assert len(findings) == 1
    assert findings[0].error_code in {"EVIDENCE_MISSING", "EVIDENCE_HASH_MISMATCH"}
    assert repository.task_state("task-1") is TaskState.TECHNICAL_FAILURE
    active = repository.load_result("task-1")
    assert active is not None
    assert active.diagnostic_path == diagnostic.resolve()
    assert active.error_code == findings[0].error_code
    assert diagnostic.read_bytes() == b"diagnostic must remain"
    history = repository.result_history("task-1")
    assert len(history) == 1
    assert history[0].result == success
    assert history[0].reason == "EVIDENCE_AUDIT_FAILURE"


def test_second_repository_for_same_database_is_rejected_until_close(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    first = SQLiteTaskRepository(database)
    try:
        assert first.lock_path == tmp_path / "state.sqlite3.quotation.lock"
        with pytest.raises(RepositoryError, match="任务数据库正在被另一个程序使用"):
            SQLiteTaskRepository(database)
    finally:
        first.close()

    with SQLiteTaskRepository(database):
        pass


def test_initialization_failure_releases_database_lock(tmp_path: Path) -> None:
    database = tmp_path / "broken.sqlite3"
    database.write_bytes(b"not a sqlite database")
    with pytest.raises(RepositoryError, match="任务数据库无法读取或已损坏"):
        SQLiteTaskRepository(database)

    database.unlink()
    with SQLiteTaskRepository(database):
        pass


def test_unknown_future_and_incomplete_schema_are_rejected(tmp_path: Path) -> None:
    future = tmp_path / "future.sqlite3"
    with sqlite3.connect(future) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(RepositoryError, match="数据库版本过新"):
        SQLiteTaskRepository(future)

    incomplete = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(incomplete) as connection:
        connection.execute("CREATE TABLE quotation_runs(run_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    with pytest.raises(RepositoryError, match="数据库结构不完整"):
        SQLiteTaskRepository(incomplete)


def test_schema_validates_columns_primary_keys_foreign_keys_and_index(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database):
        pass

    with sqlite3.connect(database) as connection:
        task_columns = {
            row[1]: row[5] for row in connection.execute("PRAGMA table_info(website_tasks)")
        }
        attempt_columns = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_info(website_attempts)")
        }
        attempt_fks = {
            (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA foreign_key_list(website_attempts)")
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

    assert task_columns["task_id"] == 1
    assert {"generation", "attempt_count", "waiting_site"} <= task_columns.keys()
    assert attempt_columns["task_id"] == 1
    assert attempt_columns["generation"] == 2
    assert attempt_columns["attempt_number"] == 3
    assert ("website_tasks", "task_id", "task_id") in attempt_fks
    assert "website_tasks_run_state_site" in indexes


def test_unknown_state_and_payload_are_reported_as_chinese_repository_errors(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE website_tasks SET state = 'future_state' WHERE task_id = 'task-1'"
        )
    with pytest.raises(RepositoryError, match="任务状态无法识别"):
        repository.task_state("task-1")

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            UPDATE website_tasks
            SET state = 'pending',
                payload_json = '{"schema_version":999,"payload_type":"WebsiteTask","data":{}}'
            WHERE task_id = 'task-1'
            """
        )
    with pytest.raises(RepositoryError, match="持久化数据版本不受支持"):
        repository.load_task("task-1")


def test_persisted_payloads_are_versioned(
    repository: SQLiteTaskRepository, tmp_path: Path
) -> None:
    _seed_task(repository, tmp_path)
    payload = json.loads(repository.raw_task_payload("task-1"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["payload_type"] == "WebsiteTask"

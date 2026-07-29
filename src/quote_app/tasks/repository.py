from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, TypeVar, cast

from quote_app.tasks.models import (
    RunRecord,
    TaskState,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.serialization import PayloadError, from_payload, to_payload

_T = TypeVar(
    "_T",
    RunRecord,
    WebsiteTask,
    WebsiteObservationCheckpoint,
    WebsiteResult,
)

_DB_SCHEMA_VERSION = 1
_LOCK_SUFFIX = ".quotation.lock"
_INTERRUPTION_CODE = "PROCESS_INTERRUPTED"
_INTERRUPTION_MESSAGE = "上次程序在任务执行过程中中断，已恢复为待处理"
_LOGIN_CODE = "LOGIN_REQUIRED"
_LOGIN_MESSAGE = "网站要求登录或安全验证，任务已暂停等待人工处理"
_EVIDENCE_MESSAGES = {
    "EVIDENCE_MISSING": "正式证据文件不存在，任务已转为技术失败",
    "EVIDENCE_HASH_MISMATCH": "正式证据文件校验值不一致，任务已转为技术失败",
}


class RepositoryError(RuntimeError):
    """A stable Chinese error raised at the local persistence boundary."""


@dataclass(frozen=True, slots=True)
class AttemptToken:
    task_id: str
    generation: int
    attempt_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must not be blank")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    task_id: str
    generation: int
    attempt_number: int
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class ResultHistoryRecord:
    task_id: str
    generation: int
    result: WebsiteResult
    reason: str
    archived_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceAuditFinding:
    task_id: str
    error_code: str
    error_message: str
    evidence_path: Path


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


class _DatabaseLock:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.lock_path = database_path.with_name(database_path.name + _LOCK_SUFFIX)
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        handle: BinaryIO | None = None
        try:
            handle = self.lock_path.open("a+b")
            if self.lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _acquire_os_lock(handle)
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise RepositoryError(
                f"任务数据库正在被另一个程序使用：{self.database_path}"
            ) from exc
        except BaseException:
            if handle is not None:
                handle.close()
            raise
        self._handle = handle

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        release_error: OSError | None = None
        try:
            _release_os_lock(handle)
        except OSError as exc:
            release_error = exc
        finally:
            handle.close()
        if release_error is not None:
            raise RepositoryError(
                f"任务数据库锁释放失败：{self.database_path}"
            ) from release_error


class SQLiteTaskRepository:
    """Single-owner, crash-safe local storage for browser task checkpoints."""

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise ValueError("database_path must be a Path")
        self.database_path = database_path.expanduser().resolve()
        self.lock_path = self.database_path.with_name(
            self.database_path.name + _LOCK_SUFFIX
        )
        self._lock = _DatabaseLock(self.database_path)
        self._closed = True
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock.acquire()
            self._closed = False
            self._initialize_schema()
            self.recover_interrupted_tasks()
        except BaseException:
            self._closed = True
            try:
                self._lock.close()
            except RepositoryError:
                pass
            raise

    def __enter__(self) -> SQLiteTaskRepository:
        self._ensure_open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lock.close()

    def create_run(self, run: RunRecord) -> None:
        payload = _encode(run)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM quotation_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            if existing is not None:
                if cast(str, existing["payload_json"]) == payload:
                    return
                raise RepositoryError("运行标识已存在，但运行内容不同")
            connection.execute(
                """
                INSERT INTO quotation_runs(
                    run_id, schema_version, payload_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.schema_version,
                    payload,
                    run.state.value,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )

    def load_run(self, run_id: str) -> RunRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM quotation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"找不到运行记录：{run_id}")
        return _decode(cast(str, row["payload_json"]), RunRecord)

    def upsert_task(
        self,
        task: WebsiteTask,
        *,
        generation: int = 0,
        _after_result_delete: Callable[[], None] | None = None,
    ) -> None:
        _validate_generation(generation)
        payload = _encode(task)
        timestamp = _utc_now().isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT payload_json, state, generation, attempt_count
                FROM website_tasks
                WHERE task_id = ?
                """,
                (task.task_id,),
            ).fetchone()
            if row is None:
                self._ensure_run_exists(connection, task.run_id)
                connection.execute(
                    """
                    INSERT INTO website_tasks(
                        task_id, run_id, payload_json, state, attempt_count,
                        waiting_site, generation, updated_at
                    ) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.run_id,
                        payload,
                        TaskState.PENDING.value,
                        generation,
                        timestamp,
                    ),
                )
                return

            existing_payload = cast(str, row["payload_json"])
            existing_generation = cast(int, row["generation"])
            if generation == existing_generation and existing_payload == payload:
                return
            if generation <= existing_generation:
                raise RepositoryError(
                    "任务标识已存在，但任务内容不同；必须显式使用更大的任务代次"
                )
            if _task_state(cast(str, row["state"])) is TaskState.RUNNING:
                raise RepositoryError("任务正在执行，不能切换任务代次")

            self._ensure_run_exists(connection, task.run_id)
            self._archive_active_result(
                connection,
                task.task_id,
                reason="GENERATION_REPLACED",
                archived_at=timestamp,
            )
            connection.execute(
                "DELETE FROM website_results WHERE task_id = ?",
                (task.task_id,),
            )
            if _after_result_delete is not None:
                _after_result_delete()
            connection.execute(
                """
                UPDATE website_tasks
                SET run_id = ?, payload_json = ?, state = ?, attempt_count = 0,
                    waiting_site = NULL, generation = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task.run_id,
                    payload,
                    TaskState.PENDING.value,
                    generation,
                    timestamp,
                    task.task_id,
                ),
            )

    def load_task(self, task_id: str) -> WebsiteTask:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM website_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"找不到网站任务：{task_id}")
        return _decode(cast(str, row["payload_json"]), WebsiteTask)

    def load_result(self, task_id: str) -> WebsiteResult | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM website_results WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode(cast(str, row["result_json"]), WebsiteResult)

    def load_observation(
        self,
        task_id: str,
    ) -> WebsiteObservationCheckpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT o.payload_json
                FROM website_observations AS o
                JOIN website_tasks AS t
                  ON t.task_id = o.task_id AND t.generation = o.generation
                WHERE o.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode(
            cast(str, row["payload_json"]),
            WebsiteObservationCheckpoint,
        )

    def save_observation(
        self,
        checkpoint: WebsiteObservationCheckpoint,
        *,
        token: AttemptToken,
    ) -> None:
        if not isinstance(token, AttemptToken) or token.task_id != checkpoint.task_id:
            raise RepositoryError("尝试令牌与观察任务不匹配")
        payload = _encode(checkpoint)
        updated_at = _utc_now().isoformat()
        with self._transaction() as connection:
            self._require_current_attempt(connection, token, action="保存观察")
            connection.execute(
                """
                INSERT INTO website_observations(
                    task_id, generation, payload_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    generation = excluded.generation,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (checkpoint.task_id, token.generation, payload, updated_at),
            )

    def save_result(
        self,
        result: WebsiteResult,
        *,
        token: AttemptToken,
        finished_at: datetime | None = None,
        _after_result_write: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(token, AttemptToken) or token.task_id != result.task_id:
            raise RepositoryError("尝试令牌与结果任务不匹配")
        finished = _timestamp(finished_at)
        payload = _encode(result)
        with self._transaction() as connection:
            self._require_current_attempt(connection, token, action="保存结果")
            self._archive_active_result(
                connection,
                result.task_id,
                reason="RETRY_RESULT_REPLACED",
                archived_at=finished,
            )
            connection.execute(
                """
                INSERT INTO website_results(task_id, generation, result_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    generation = excluded.generation,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (result.task_id, token.generation, payload, finished),
            )
            if _after_result_write is not None:
                _after_result_write()
            connection.execute(
                """
                UPDATE website_attempts
                SET finished_at = ?, error_code = ?, error_message = ?
                WHERE task_id = ? AND generation = ? AND attempt_number = ?
                  AND finished_at IS NULL
                """,
                (
                    finished,
                    result.error_code,
                    result.error_message,
                    token.task_id,
                    token.generation,
                    token.attempt_number,
                ),
            )
            connection.execute(
                """
                UPDATE website_tasks
                SET state = ?, waiting_site = NULL, updated_at = ?
                WHERE task_id = ? AND generation = ? AND state = ?
                """,
                (
                    result.state.value,
                    finished,
                    token.task_id,
                    token.generation,
                    TaskState.RUNNING.value,
                ),
            )

    def start_attempt(
        self,
        task_id: str,
        *,
        started_at: datetime | None = None,
    ) -> AttemptToken:
        started = _timestamp(started_at)
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            state = _task_state(cast(str, row["state"]))
            if state not in {TaskState.PENDING, TaskState.TECHNICAL_FAILURE}:
                raise RepositoryError(
                    f"任务当前状态不能开始新的尝试：{state.value}"
                )
            generation = cast(int, row["generation"])
            attempt_number = cast(int, row["attempt_count"]) + 1
            connection.execute(
                """
                INSERT INTO website_attempts(
                    task_id, generation, attempt_number, started_at, finished_at,
                    error_code, error_message
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (task_id, generation, attempt_number, started),
            )
            connection.execute(
                """
                UPDATE website_tasks
                SET attempt_count = ?, state = ?, waiting_site = NULL, updated_at = ?
                WHERE task_id = ? AND generation = ?
                """,
                (
                    attempt_number,
                    TaskState.RUNNING.value,
                    started,
                    task_id,
                    generation,
                ),
            )
        return AttemptToken(task_id, generation, attempt_number)

    def mark_waiting_for_login(
        self,
        task_id: str,
        *,
        token: AttemptToken,
        site: str,
        updated_at: datetime | None = None,
    ) -> None:
        if not isinstance(site, str) or not site.strip():
            raise ValueError("site must not be blank")
        if not isinstance(token, AttemptToken) or token.task_id != task_id:
            raise RepositoryError("尝试令牌与等待登录任务不匹配")
        timestamp = _timestamp(updated_at)
        with self._transaction() as connection:
            self._require_current_attempt(
                connection,
                token,
                action="等待登录",
            )
            connection.execute(
                """
                UPDATE website_attempts
                SET finished_at = ?, error_code = ?, error_message = ?
                WHERE task_id = ? AND generation = ? AND attempt_number = ?
                  AND finished_at IS NULL
                """,
                (
                    timestamp,
                    _LOGIN_CODE,
                    _LOGIN_MESSAGE,
                    token.task_id,
                    token.generation,
                    token.attempt_number,
                ),
            )
            connection.execute(
                """
                UPDATE website_tasks
                SET state = ?, waiting_site = ?, updated_at = ?
                WHERE task_id = ? AND generation = ? AND state = ?
                """,
                (
                    TaskState.WAITING_FOR_LOGIN.value,
                    site.strip(),
                    timestamp,
                    task_id,
                    token.generation,
                    TaskState.RUNNING.value,
                ),
            )

    def pause_task(
        self,
        task_id: str,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        timestamp = _timestamp(updated_at)
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            state = _task_state(cast(str, row["state"]))
            if state in {TaskState.SUCCEEDED, TaskState.PAUSED}:
                return
            if state is TaskState.RUNNING:
                connection.execute(
                    """
                    UPDATE website_attempts
                    SET finished_at = ?, error_code = ?, error_message = ?
                    WHERE task_id = ? AND generation = ? AND attempt_number = ?
                      AND finished_at IS NULL
                    """,
                    (
                        timestamp,
                        "TASK_PAUSED",
                        "任务已由用户暂停",
                        task_id,
                        cast(int, row["generation"]),
                        cast(int, row["attempt_count"]),
                    ),
                )
            connection.execute(
                """
                UPDATE website_tasks
                SET state = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskState.PAUSED.value, timestamp, task_id),
            )

    def resume_task(
        self,
        task_id: str,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        timestamp = _timestamp(updated_at)
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            if _task_state(cast(str, row["state"])) is not TaskState.PAUSED:
                return
            connection.execute(
                """
                UPDATE website_tasks
                SET state = ?, waiting_site = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskState.PENDING.value, timestamp, task_id),
            )

    def requeue_waiting_site(
        self,
        run_id: str,
        site: str,
        *,
        updated_at: datetime | None = None,
    ) -> tuple[WebsiteTask, ...]:
        if not isinstance(site, str) or not site.strip():
            raise ValueError("site must not be blank")
        timestamp = _timestamp(updated_at)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM website_tasks
                WHERE run_id = ? AND state = ? AND waiting_site = ?
                ORDER BY rowid
                """,
                (run_id, TaskState.WAITING_FOR_LOGIN.value, site.strip()),
            ).fetchall()
            tasks = tuple(
                _decode(cast(str, row["payload_json"]), WebsiteTask)
                for row in rows
            )
            connection.execute(
                """
                UPDATE website_tasks
                SET state = ?, waiting_site = NULL, updated_at = ?
                WHERE run_id = ? AND state = ? AND waiting_site = ?
                """,
                (
                    TaskState.PENDING.value,
                    timestamp,
                    run_id,
                    TaskState.WAITING_FOR_LOGIN.value,
                    site.strip(),
                ),
            )
        return tasks

    def recover_interrupted_tasks(
        self,
        *,
        recovered_at: datetime | None = None,
    ) -> int:
        timestamp = _timestamp(recovered_at)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT task_id, generation, attempt_count
                FROM website_tasks
                WHERE state = ?
                """,
                (TaskState.RUNNING.value,),
            ).fetchall()
            for row in rows:
                task_id = cast(str, row["task_id"])
                generation = cast(int, row["generation"])
                attempt_number = cast(int, row["attempt_count"])
                connection.execute(
                    """
                    UPDATE website_attempts
                    SET finished_at = COALESCE(finished_at, ?),
                        error_code = ?,
                        error_message = ?
                    WHERE task_id = ? AND generation = ? AND attempt_number = ?
                    """,
                    (
                        timestamp,
                        _INTERRUPTION_CODE,
                        _INTERRUPTION_MESSAGE,
                        task_id,
                        generation,
                        attempt_number,
                    ),
                )
                connection.execute(
                    """
                    UPDATE website_tasks
                    SET state = ?, updated_at = ?
                    WHERE task_id = ? AND generation = ? AND state = ?
                    """,
                    (
                        TaskState.PENDING.value,
                        timestamp,
                        task_id,
                        generation,
                        TaskState.RUNNING.value,
                    ),
                )
        return len(rows)

    def select_pending(self, run_id: str) -> tuple[WebsiteTask, ...]:
        return self._select_tasks(run_id, (TaskState.PENDING,))

    def select_failed(self, run_id: str) -> tuple[WebsiteTask, ...]:
        return self._select_tasks(run_id, (TaskState.TECHNICAL_FAILURE,))

    def select_waiting(
        self,
        run_id: str,
        *,
        site: str | None = None,
    ) -> tuple[WebsiteTask, ...]:
        if site is None:
            return self._select_tasks(run_id, (TaskState.WAITING_FOR_LOGIN,))
        if not isinstance(site, str) or not site.strip():
            raise ValueError("site must not be blank")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM website_tasks
                WHERE run_id = ? AND state = ? AND waiting_site = ?
                ORDER BY rowid
                """,
                (run_id, TaskState.WAITING_FOR_LOGIN.value, site.strip()),
            ).fetchall()
        return tuple(
            _decode(cast(str, row["payload_json"]), WebsiteTask)
            for row in rows
        )

    def task_state(self, task_id: str) -> TaskState:
        with self._connection() as connection:
            row = self._task_row(connection, task_id)
            return _task_state(cast(str, row["state"]))

    def attempt_count(self, task_id: str) -> int:
        with self._connection() as connection:
            return cast(int, self._task_row(connection, task_id)["attempt_count"])

    def task_generation(self, task_id: str) -> int:
        with self._connection() as connection:
            return cast(int, self._task_row(connection, task_id)["generation"])

    def latest_attempt(self, task_id: str) -> AttemptRecord | None:
        attempts = self.list_attempts(task_id)
        return attempts[-1] if attempts else None

    def list_attempts(self, task_id: str) -> tuple[AttemptRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, generation, attempt_number, started_at, finished_at,
                       error_code, error_message
                FROM website_attempts
                WHERE task_id = ?
                ORDER BY generation, attempt_number
                """,
                (task_id,),
            ).fetchall()
        return tuple(_attempt_record(row) for row in rows)

    def result_history(self, task_id: str) -> tuple[ResultHistoryRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, generation, result_json, reason, archived_at
                FROM website_result_history
                WHERE task_id = ?
                ORDER BY history_id
                """,
                (task_id,),
            ).fetchall()
        return tuple(
            ResultHistoryRecord(
                task_id=cast(str, row["task_id"]),
                generation=cast(int, row["generation"]),
                result=_decode(cast(str, row["result_json"]), WebsiteResult),
                reason=cast(str, row["reason"]),
                archived_at=datetime.fromisoformat(cast(str, row["archived_at"])),
            )
            for row in rows
        )

    def audit_evidence(self, run_id: str) -> tuple[EvidenceAuditFinding, ...]:
        findings: list[EvidenceAuditFinding] = []
        timestamp = _utc_now().isoformat()
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT r.task_id, r.generation, r.result_json
                FROM website_results AS r
                JOIN website_tasks AS t ON t.task_id = r.task_id
                WHERE t.run_id = ? AND t.state = ?
                ORDER BY t.rowid
                """,
                (run_id, TaskState.SUCCEEDED.value),
            ).fetchall()
            for row in rows:
                result = _decode(
                    cast(str, row["result_json"]),
                    WebsiteResult,
                )
                if result.evidence is None:
                    continue
                error_code = _audit_evidence_record(
                    result.evidence.path,
                    result.evidence.sha256,
                )
                if error_code is None:
                    continue
                error_message = _EVIDENCE_MESSAGES[error_code]
                generation = cast(int, row["generation"])
                self._archive_active_result(
                    connection,
                    result.task_id,
                    reason="EVIDENCE_AUDIT_FAILURE",
                    archived_at=timestamp,
                )
                failure = WebsiteResult(
                    task_id=result.task_id,
                    state=TaskState.TECHNICAL_FAILURE,
                    outcome=None,
                    price=None,
                    url=None,
                    evidence=None,
                    diagnostic_path=result.diagnostic_path,
                    error_code=error_code,
                    error_message=error_message,
                )
                connection.execute(
                    """
                    UPDATE website_results
                    SET generation = ?, result_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (generation, _encode(failure), timestamp, result.task_id),
                )
                connection.execute(
                    """
                    UPDATE website_tasks
                    SET state = ?, updated_at = ?
                    WHERE task_id = ? AND generation = ?
                    """,
                    (
                        TaskState.TECHNICAL_FAILURE.value,
                        timestamp,
                        result.task_id,
                        generation,
                    ),
                )
                findings.append(
                    EvidenceAuditFinding(
                        task_id=result.task_id,
                        error_code=error_code,
                        error_message=error_message,
                        evidence_path=result.evidence.path,
                    )
                )
        return tuple(findings)

    def connection_pragmas(self) -> dict[str, int | str]:
        with self._connection() as connection:
            return {
                "foreign_keys": cast(
                    int,
                    connection.execute("PRAGMA foreign_keys").fetchone()[0],
                ),
                "journal_mode": cast(
                    str,
                    connection.execute("PRAGMA journal_mode").fetchone()[0],
                ).lower(),
                "busy_timeout": cast(
                    int,
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                ),
            }

    def raw_task_payload(self, task_id: str) -> str:
        with self._connection() as connection:
            return cast(str, self._task_row(connection, task_id)["payload_json"])

    def _require_current_attempt(
        self,
        connection: sqlite3.Connection,
        token: AttemptToken,
        *,
        action: str,
    ) -> None:
        row = self._task_row(connection, token.task_id)
        state = _task_state(cast(str, row["state"]))
        if state is not TaskState.RUNNING:
            if action == "等待登录":
                raise RepositoryError("只有正在执行的任务才能等待登录")
            if action == "保存观察":
                raise RepositoryError("只有正在执行的任务才能保存观察")
            raise RepositoryError("只有正在执行的任务才能保存结果")
        if (
            cast(int, row["generation"]) != token.generation
            or cast(int, row["attempt_count"]) != token.attempt_number
        ):
            raise RepositoryError("尝试令牌已过期，拒绝写入任务状态")
        attempt = connection.execute(
            """
            SELECT finished_at
            FROM website_attempts
            WHERE task_id = ? AND generation = ? AND attempt_number = ?
            """,
            (token.task_id, token.generation, token.attempt_number),
        ).fetchone()
        if attempt is None or attempt["finished_at"] is not None:
            raise RepositoryError("尝试令牌不是当前未完成的最新尝试")

    def _archive_active_result(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        reason: str,
        archived_at: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT generation, result_json
            FROM website_results
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return
        connection.execute(
            """
            INSERT INTO website_result_history(
                task_id, generation, result_json, reason, archived_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                cast(int, row["generation"]),
                cast(str, row["result_json"]),
                reason,
                archived_at,
            ),
        )

    def _select_tasks(
        self,
        run_id: str,
        states: tuple[TaskState, ...],
    ) -> tuple[WebsiteTask, ...]:
        placeholders = ", ".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM website_tasks
                WHERE run_id = ? AND state IN ({placeholders})
                ORDER BY rowid
                """,
                (run_id, *(state.value for state in states)),
            ).fetchall()
        return tuple(
            _decode(cast(str, row["payload_json"]), WebsiteTask)
            for row in rows
        )

    def _initialize_schema(self) -> None:
        with self._transaction() as connection:
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _DB_SCHEMA_VERSION:
                raise RepositoryError(
                    f"数据库版本过新（{version}），当前程序仅支持版本 {_DB_SCHEMA_VERSION}"
                )
            if version not in {0, _DB_SCHEMA_VERSION}:
                raise RepositoryError(
                    f"数据库版本 {version} 需要显式迁移，不能自动打开"
                )
            if version == _DB_SCHEMA_VERSION:
                connection.execute(_OBSERVATION_SCHEMA_STATEMENT)
                self._verify_schema(connection)
                return
            existing_tables = {
                cast(str, row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
            if existing_tables:
                raise RepositoryError("数据库缺少版本信息，需要显式迁移后才能打开")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={_DB_SCHEMA_VERSION}")
            self._verify_schema(connection)

    def _verify_schema(self, connection: sqlite3.Connection) -> None:
        for table_name, expected_columns in _EXPECTED_COLUMNS.items():
            rows = connection.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
            actual_columns = {
                cast(str, row["name"]): (
                    cast(str, row["type"]).upper(),
                    cast(int, row["notnull"]),
                    cast(int, row["pk"]),
                )
                for row in rows
            }
            if actual_columns != expected_columns:
                raise RepositoryError(
                    f"数据库结构不完整：数据表 {table_name} 的列定义不匹配"
                )
        for table_name, expected in _EXPECTED_FOREIGN_KEYS.items():
            actual_foreign_keys = {
                (
                    cast(str, row["table"]),
                    cast(str, row["from"]),
                    cast(str, row["to"]),
                )
                for row in connection.execute(
                    f"PRAGMA foreign_key_list({table_name})"
                )
            }
            if actual_foreign_keys != expected:
                raise RepositoryError(
                    f"数据库结构不完整：数据表 {table_name} 的外键定义不匹配"
                )
        index_rows = connection.execute(
            "PRAGMA index_info(website_tasks_run_state_site)"
        ).fetchall()
        index_columns = tuple(cast(str, row["name"]) for row in index_rows)
        if index_columns != ("run_id", "state", "waiting_site"):
            raise RepositoryError("数据库结构不完整：任务状态索引定义不匹配")

    def _ensure_run_exists(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM quotation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            is None
        ):
            raise RepositoryError(f"网站任务引用了不存在的运行记录：{run_id}")

    def _task_row(
        self,
        connection: sqlite3.Connection,
        task_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT task_id, run_id, payload_json, state, attempt_count,
                   waiting_site, generation, updated_at
            FROM website_tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise RepositoryError(f"找不到网站任务：{task_id}")
        return row

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        connection = _open_connection(self.database_path)
        try:
            yield connection
        except RepositoryError:
            raise
        except sqlite3.Error as exc:
            raise RepositoryError("任务数据库操作失败，请检查本地数据文件") from exc
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        connection = _open_connection(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except RepositoryError:
            _rollback_quietly(connection)
            raise
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise RepositoryError("任务数据库事务失败，操作已安全回滚") from exc
        except BaseException:
            _rollback_quietly(connection)
            raise
        finally:
            connection.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryError("任务数据库已经关闭")


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE quotation_runs (
        run_id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE website_tasks (
        task_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        waiting_site TEXT,
        generation INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES quotation_runs(run_id)
    )
    """,
    """
    CREATE TABLE website_attempts (
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error_code TEXT,
        error_message TEXT,
        PRIMARY KEY(task_id, generation, attempt_number),
        FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
    )
    """,
    """
    CREATE TABLE website_results (
        task_id TEXT PRIMARY KEY,
        generation INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS website_observations (
        task_id TEXT PRIMARY KEY,
        generation INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
    )
    """,
    """
    CREATE TABLE website_result_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        archived_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
    )
    """,
    """
    CREATE INDEX website_tasks_run_state_site
    ON website_tasks(run_id, state, waiting_site)
    """,
)

_EXPECTED_COLUMNS = {
    "quotation_runs": {
        "run_id": ("TEXT", 0, 1),
        "schema_version": ("INTEGER", 1, 0),
        "payload_json": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "created_at": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
    },
    "website_tasks": {
        "task_id": ("TEXT", 0, 1),
        "run_id": ("TEXT", 1, 0),
        "payload_json": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "attempt_count": ("INTEGER", 1, 0),
        "waiting_site": ("TEXT", 0, 0),
        "generation": ("INTEGER", 1, 0),
        "updated_at": ("TEXT", 1, 0),
    },
    "website_attempts": {
        "task_id": ("TEXT", 1, 1),
        "generation": ("INTEGER", 1, 2),
        "attempt_number": ("INTEGER", 1, 3),
        "started_at": ("TEXT", 1, 0),
        "finished_at": ("TEXT", 0, 0),
        "error_code": ("TEXT", 0, 0),
        "error_message": ("TEXT", 0, 0),
    },
    "website_results": {
        "task_id": ("TEXT", 0, 1),
        "generation": ("INTEGER", 1, 0),
        "result_json": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
    },
    "website_observations": {
        "task_id": ("TEXT", 0, 1),
        "generation": ("INTEGER", 1, 0),
        "payload_json": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
    },
    "website_result_history": {
        "history_id": ("INTEGER", 0, 1),
        "task_id": ("TEXT", 1, 0),
        "generation": ("INTEGER", 1, 0),
        "result_json": ("TEXT", 1, 0),
        "reason": ("TEXT", 1, 0),
        "archived_at": ("TEXT", 1, 0),
    },
}

_EXPECTED_FOREIGN_KEYS = {
    "quotation_runs": set(),
    "website_tasks": {("quotation_runs", "run_id", "run_id")},
    "website_attempts": {("website_tasks", "task_id", "task_id")},
    "website_results": {("website_tasks", "task_id", "task_id")},
    "website_observations": {("website_tasks", "task_id", "task_id")},
    "website_result_history": {("website_tasks", "task_id", "task_id")},
}

_OBSERVATION_SCHEMA_STATEMENT = _SCHEMA_STATEMENTS[4]


def _open_connection(database_path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        message = (
            "任务数据库无法读取或已损坏"
            if "not a database" in str(exc).lower()
            else "任务数据库连接失败"
        )
        raise RepositoryError(message) from exc


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def _acquire_os_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, import_module("msvcrt"))
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl = cast(_FcntlModule, import_module("fcntl"))
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, import_module("msvcrt"))
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = cast(_FcntlModule, import_module("fcntl"))
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        task_id=cast(str, row["task_id"]),
        generation=cast(int, row["generation"]),
        attempt_number=cast(int, row["attempt_number"]),
        started_at=datetime.fromisoformat(cast(str, row["started_at"])),
        finished_at=(
            datetime.fromisoformat(cast(str, row["finished_at"]))
            if row["finished_at"] is not None
            else None
        ),
        error_code=cast(str | None, row["error_code"]),
        error_message=cast(str | None, row["error_message"]),
    )


def _encode(
    value: RunRecord | WebsiteTask | WebsiteObservationCheckpoint | WebsiteResult,
) -> str:
    return json.dumps(
        to_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode(payload_json: str, expected_type: type[_T]) -> _T:
    try:
        decoded = from_payload(json.loads(payload_json))
    except (json.JSONDecodeError, PayloadError) as exc:
        raise RepositoryError("持久化数据版本不受支持或内容已损坏") from exc
    if not isinstance(decoded, expected_type):
        raise RepositoryError("持久化数据类型与数据库记录不一致")
    return decoded


def _task_state(raw_state: str) -> TaskState:
    try:
        return TaskState(raw_state)
    except ValueError as exc:
        raise RepositoryError(f"任务状态无法识别：{raw_state}") from exc


def _timestamp(value: datetime | None) -> str:
    timestamp = value or _utc_now()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_generation(generation: int) -> None:
    if type(generation) is not int or generation < 0:
        raise ValueError("generation must be a non-negative integer")


def _audit_evidence_record(path: Path, expected_sha256: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(source.fileno())
        path_after = path.stat()
    except OSError:
        return "EVIDENCE_MISSING"
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(path_after)
        or digest.hexdigest() != expected_sha256
    ):
        return "EVIDENCE_HASH_MISMATCH"
    return None


def _stat_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )

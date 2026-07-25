from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
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
from quote_app.tasks.repository import RepositoryError, SQLiteTaskRepository

NOW = datetime(2026, 7, 25, 9, tzinfo=timezone.utc)


def _seed_run(tmp_path: Path) -> RunRecord:
    return RunRecord(
        run_id="run-recovery",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=tuple(
            InputFingerprint(
                source_role=role,
                path=tmp_path / f"{role}.xlsx",
                sha256=str(index) * 64,
                byte_size=index,
                modified_ns=index,
            )
            for index, role in enumerate(("base", "marketing", "bop"), start=1)
        ),
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot='{"rows":[]}',
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256="a" * 64,
        quote_path=None,
        report_path=None,
        state=RunState.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )


def _task(task_id: str) -> WebsiteTask:
    return WebsiteTask(
        task_id=task_id,
        run_id="run-recovery",
        source_row_number=2,
        output_row_number=2,
        material_code=f"code-{task_id}",
        brand="小米",
        model_name="Xiaomi 17 Pro",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.JD,
    )


def _success(tmp_path: Path, task_id: str) -> WebsiteResult:
    evidence_bytes = b"recovery evidence"
    evidence_path = tmp_path / f"{task_id}.png"
    evidence_path.write_bytes(evidence_bytes)
    return WebsiteResult(
        task_id=task_id,
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("3999"),
        url="https://example.test/product",
        evidence=EvidenceRecord(
            state=EvidenceState.NORMAL,
            path=evidence_path,
            sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            pixel_width=1000,
            pixel_height=800,
            captured_at=NOW,
            validation_code="CAPTURE_OK",
            annotations=(EvidenceRectangle("price", 10, 10, 100, 50),),
        ),
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    return environment


def test_real_process_lock_rejects_live_owner_then_allows_clean_exit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database):
        pass
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from quote_app.tasks.repository import SQLiteTaskRepository\n"
        "repo = SQLiteTaskRepository(Path(sys.argv[1]))\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
        "repo.close()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_subprocess_environment(),
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(RepositoryError, match="任务数据库正在被另一个程序使用"):
            SQLiteTaskRepository(database)
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        with SQLiteTaskRepository(database):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_real_process_crash_releases_lock_and_recovers_running_attempt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_seed_run(tmp_path))
        repository.upsert_task(_task("running"))
    script = (
        "from pathlib import Path\n"
        "import os, sys\n"
        "from quote_app.tasks.repository import SQLiteTaskRepository\n"
        "repo = SQLiteTaskRepository(Path(sys.argv[1]))\n"
        "repo.start_attempt('running')\n"
        "print('running', flush=True)\n"
        "os._exit(17)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_subprocess_environment(),
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "running"
    assert child.wait(timeout=5) == 17

    with SQLiteTaskRepository(database) as recovered:
        assert recovered.task_state("running") is TaskState.PENDING
        attempt = recovered.latest_attempt("running")
        assert attempt is not None
        assert attempt.error_code == "PROCESS_INTERRUPTED"
        assert attempt.error_message == "上次程序在任务执行过程中中断，已恢复为待处理"
        assert attempt.finished_at is not None


def test_real_process_crash_between_result_and_state_write_rolls_back_half_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_seed_run(tmp_path))
        repository.upsert_task(_task("half-write"))
    script = (
        "from pathlib import Path\n"
        "import os, sys\n"
        "from quote_app.tasks.models import TaskState, WebsiteResult\n"
        "from quote_app.tasks.repository import SQLiteTaskRepository\n"
        "repo = SQLiteTaskRepository(Path(sys.argv[1]))\n"
        "token = repo.start_attempt('half-write')\n"
        "result = WebsiteResult(\n"
        " task_id='half-write', state=TaskState.TECHNICAL_FAILURE,\n"
        " outcome=None, price=None, url=None, evidence=None,\n"
        " diagnostic_path=None, error_code='TEST_CRASH',\n"
        " error_message='真实进程中断')\n"
        "repo.save_result(result, token=token,\n"
        " _after_result_write=lambda: os._exit(23))\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        capture_output=True,
        text=True,
        env=_subprocess_environment(),
        timeout=5,
        check=False,
    )
    assert child.returncode == 23

    with SQLiteTaskRepository(database) as recovered:
        assert recovered.load_result("half-write") is None
        assert recovered.task_state("half-write") is TaskState.PENDING
        attempt = recovered.latest_attempt("half-write")
        assert attempt is not None
        assert attempt.error_code == "PROCESS_INTERRUPTED"


def test_reopen_keeps_waiting_paused_and_succeeded_states(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database) as first:
        first.create_run(_seed_run(tmp_path))
        for task_id in ("waiting", "paused", "succeeded"):
            first.upsert_task(_task(task_id))
        waiting_token = first.start_attempt("waiting")
        first.mark_waiting_for_login("waiting", token=waiting_token, site="京东")
        first.pause_task("paused")
        success_token = first.start_attempt("succeeded")
        first.save_result(_success(tmp_path, "succeeded"), token=success_token)

    with SQLiteTaskRepository(database) as reopened:
        assert reopened.task_state("waiting") is TaskState.WAITING_FOR_LOGIN
        assert reopened.task_state("paused") is TaskState.PAUSED
        assert reopened.task_state("succeeded") is TaskState.SUCCEEDED
        assert [task.task_id for task in reopened.select_waiting("run-recovery")] == [
            "waiting"
        ]
        assert reopened.select_pending("run-recovery") == ()
        reopened.resume_task("paused")
        assert reopened.task_state("paused") is TaskState.PENDING

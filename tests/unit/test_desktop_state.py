"""Presentation regressions: never turn a saved price into completed evidence."""

import sqlite3
from pathlib import Path

from quote_app.browser.worker import WorkerEvent
from quote_app.desktop_state import DesktopState, compact_path, read_history


def event(kind: str, **data: object) -> WorkerEvent:
    return WorkerEvent(kind, "run-1", "task-1", {"model_name": "荣耀 500", "channel": "jd", **data})


def test_observation_does_not_complete_evidence() -> None:
    model = DesktopState()
    model.apply_event(event("observation", outcome="price_found", price="2599"))
    assert model.rows[0].price == "2599"
    assert model.rows[0].evidence_state == "pending"
    assert model.evidence_count == 0
    model.apply_event(event("result", outcome="price_found", price="2599"))
    assert model.evidence_count == 1


def test_legal_no_price_is_not_a_technical_failure() -> None:
    model = DesktopState()
    model.apply_event(event("result", outcome="sold_out", price=None))
    assert model.rows[0].price == ""
    assert model.rows[0].outcome_label == "已售罄"
    assert model.rows[0].state == "succeeded"


def test_failure_retains_saved_price_but_does_not_claim_evidence() -> None:
    model = DesktopState()
    model.apply_event(event("observation", outcome="price_found", price="0"))
    model.apply_event(event("technical_failure", error_code="CAPTURE_PERMISSION"))
    assert model.rows[0].price == "0"
    assert model.rows[0].evidence_state == "pending"
    assert model.rows[0].error == "CAPTURE_PERMISSION"


def test_new_run_resets_previous_rows_and_outputs() -> None:
    model = DesktopState()
    model.apply_event(event("result", outcome="price_found", price="2599"))
    model.quote_path = Path("/tmp/old.xlsx")
    model.begin_run()
    assert model.rows == []
    assert model.quote_path is None
    assert model.run_id is None
    assert model.running


def test_compact_path_preserves_extension_and_does_not_expose_directories() -> None:
    assert compact_path("/private/a/营销商品信息查询表.xlsx") == "营销商品信息查询表.xlsx"
    assert compact_path("") == "尚未选择"
    assert compact_path("/tmp/" + "很长名称" * 20 + ".xlsx", 24).endswith(".xlsx")
    assert len(compact_path("/tmp/" + "很长名称" * 20 + ".xlsx", 24)) <= 24


def test_history_missing_database_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "tasks.sqlite3"
    assert read_history(path).runs == ()
    assert not path.parent.exists()


def test_history_invalid_database_is_a_readable_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.sqlite3"
    path.write_text("invalid")
    assert read_history(path).error
    assert path.read_text() == "invalid"


def test_history_is_readonly_and_does_not_recover_running_tasks(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE quotation_runs (run_id TEXT, state TEXT, created_at TEXT, updated_at TEXT, payload_json TEXT)"
        )
        db.execute(
            "INSERT INTO quotation_runs VALUES ('r', 'running', '2026-09-09', '2026-09-09', '{}')"
        )
    before = path.read_bytes()
    history = read_history(path)
    assert history.runs[0].state == "running"
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".quotation.lock").exists()


def test_quote_channel_prices_follow_verified_excel_columns() -> None:
    from quote_app.desktop_state import quote_channel_prices
    from quote_app.domain.models import QuoteRow

    row = QuoteRow(2, "code", cells={"AI": 3000, "AJ": 2800, "AK": 2700, "AH": 2700})
    assert quote_channel_prices(row) == ("2700", "2800", "3000")  # official, Tmall, JD
    assert quote_channel_prices(QuoteRow(2, "code")) == ("—", "—", "—")


def saved_task_database(tmp_path: Path, *, observation_generation: int = 1) -> Path:
    import json
    from datetime import datetime, timezone
    from decimal import Decimal
    from quote_app.tasks.models import (
        BusinessOutcome,
        WebsiteChannel,
        WebsiteTask,
        WebsiteObservationCheckpoint,
    )
    from quote_app.tasks.serialization import to_payload

    task = WebsiteTask(
        "t", "r", 2, 2, "code", "HONOR", "荣耀 500", "12GB", "256GB", "黑色", WebsiteChannel.JD
    )
    observation = WebsiteObservationCheckpoint(
        "t",
        BusinessOutcome.PRICE_FOUND,
        Decimal("2599"),
        "https://example.test/product",
        datetime.now(timezone.utc),
    )
    path = tmp_path / "saved.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE website_tasks (task_id TEXT, run_id TEXT, generation INTEGER, state TEXT, payload_json TEXT)"
        )
        db.execute(
            "CREATE TABLE website_results (task_id TEXT, generation INTEGER, result_json TEXT)"
        )
        db.execute(
            "CREATE TABLE website_observations (task_id TEXT, generation INTEGER, payload_json TEXT)"
        )
        db.execute(
            "INSERT INTO website_tasks VALUES (?, ?, ?, ?, ?)",
            ("t", "r", 1, "technical_failure", json.dumps(to_payload(task))),
        )
        db.execute(
            "INSERT INTO website_observations VALUES (?, ?, ?)",
            ("t", observation_generation, json.dumps(to_payload(observation))),
        )
    return path


def test_saved_task_retains_observation_and_does_not_change_state(tmp_path: Path) -> None:
    from quote_app.desktop_state import read_task_rows

    path = saved_task_database(tmp_path)
    before = path.read_bytes()
    rows, error = read_task_rows(path, "r")
    assert error == ""
    assert rows[0].price == "2599"
    assert rows[0].state == "technical_failure"
    assert rows[0].evidence_state == "pending"
    assert rows[0].specification == "12GB / 256GB / 黑色"
    assert path.read_bytes() == before


def test_history_never_displays_an_observation_from_an_old_generation(tmp_path: Path) -> None:
    from quote_app.desktop_state import read_task_rows

    path = saved_task_database(tmp_path, observation_generation=0)
    rows, error = read_task_rows(path, "r")
    assert error == ""
    assert rows[0].price == ""
    assert rows[0].outcome == ""


def test_missing_saved_evidence_is_explicit_and_does_not_change_database(tmp_path: Path) -> None:
    import json
    from datetime import datetime, timezone
    from decimal import Decimal
    from quote_app.desktop_state import read_task_rows
    from quote_app.evidence.models import EvidenceRecord, EvidenceState
    from quote_app.tasks.models import BusinessOutcome, TaskState, WebsiteResult
    from quote_app.tasks.serialization import to_payload

    path = saved_task_database(tmp_path)
    screenshot = tmp_path / "evidence.png"
    result = WebsiteResult(
        "t",
        TaskState.SUCCEEDED,
        BusinessOutcome.PRICE_FOUND,
        Decimal("2599"),
        "https://example.test/product",
        EvidenceRecord(
            EvidenceState.NORMAL,
            screenshot,
            "a" * 64,
            1280,
            850,
            datetime.now(timezone.utc),
            "CAPTURE_OK",
        ),
        None,
        None,
        None,
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO website_results VALUES (?, ?, ?)", ("t", 1, json.dumps(to_payload(result)))
        )
    before = path.read_bytes()
    rows, error = read_task_rows(path, "r")
    assert error == ""
    assert rows[0].evidence_path == screenshot
    assert rows[0].evidence_state == "missing"
    assert rows[0].price == "2599"
    assert path.read_bytes() == before
    screenshot.write_bytes(b"exists")
    rows, error = read_task_rows(path, "r")
    assert error == ""
    assert rows[0].evidence_state == "complete"
    assert path.read_bytes() == before

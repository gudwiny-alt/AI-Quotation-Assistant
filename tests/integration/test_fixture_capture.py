from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from quote_app.browser.worker import WorkerEvent
from quote_app.domain.models import QuoteMonth
from quote_app.evidence.geometry import CssRect
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceRectangle,
)
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureRequest,
    PlatformEvidenceCapture,
    make_capture_error,
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
from quote_app.tasks.retry import LoginRequired, RetryPolicy
from quote_app.tasks.runner import (
    FixtureObservation,
    WebsiteTaskRunner,
    safe_task_file_stem,
)

NOW = datetime(2026, 7, 26, 9, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "browser"
STATE_PATTERN = re.compile(r'data-fixture-state="([^"]+)"')


class _Probe:
    def semantic_hash(self) -> str:
        return "fixture-stable"


class _Page:
    def __init__(
        self,
        fixtures_by_task: dict[str, Path],
        loads: list[str],
    ) -> None:
        self.fixtures_by_task = fixtures_by_task
        self.current_html = ""
        self.loads = loads
        self.front_count = 0

    def load(self, task_id: str) -> None:
        self.loads.append(task_id)
        self.current_html = self.fixtures_by_task[task_id].read_text(
            encoding="utf-8"
        )

    def bring_to_front(self) -> None:
        self.front_count += 1


class _Session:
    def __init__(self, fixtures_by_task: dict[str, Path]) -> None:
        self.fixtures_by_task = fixtures_by_task
        self.families: list[str] = []
        self.loads: list[str] = []
        self.pages: dict[str, _Page] = {}

    def page_for(self, site_family: str) -> _Page:
        self.families.append(site_family)
        return self.pages.setdefault(
            site_family,
            _Page(self.fixtures_by_task, self.loads),
        )


class _Capture(PlatformEvidenceCapture):
    def __init__(self, *, failing_task: str | None = None) -> None:
        self.failing_task = failing_task
        self.requests: list[CaptureRequest] = []

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        self.requests.append(request)
        file_stem = request.destination.name.split(".g", 1)[0]
        if (
            self.failing_task is not None
            and file_stem == safe_task_file_stem(self.failing_task)
        ):
            request.destination.write_bytes(b"partial")
            raise make_capture_error(
                "CAPTURE_FAILED",
                "fixture capture failed",
            )
        payload = f"fixture:{file_stem}:{request.state.value}".encode()
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(payload)
        annotations = tuple(
            EvidenceRectangle(
                role=rectangle.role,
                x=10 + index * 20,
                y=20 + index * 20,
                width=60,
                height=30,
            )
            for index, rectangle in enumerate(request.css_rectangles)
        )
        return EvidenceRecord(
            state=request.state,
            path=request.destination,
            sha256=hashlib.sha256(payload).hexdigest(),
            pixel_width=800,
            pixel_height=600,
            captured_at=NOW,
            validation_code="CAPTURE_OK",
            annotations=annotations,
        )


def _adapter(task: WebsiteTask, page: _Page) -> FixtureObservation:
    page.load(task.task_id)
    matched = STATE_PATTERN.search(page.current_html)
    assert matched is not None
    state = matched.group(1)
    if state == "login_required":
        raise LoginRequired("tmall", "fixture login required")
    cases = {
        "normal": (
            BusinessOutcome.PRICE_FOUND,
            Decimal("3999.00"),
            (),
            (),
        ),
        "no_model": (
            BusinessOutcome.NO_MODEL,
            None,
            (
                CssRect(10, 10, 100, 30, "search_keyword"),
                CssRect(10, 60, 300, 160, "result_region"),
            ),
            ("search_keyword", "result_region"),
        ),
        "capacity_disabled": (
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            None,
            (CssRect(20, 100, 120, 40, "capacity"),),
            ("capacity",),
        ),
        "color_disabled": (
            BusinessOutcome.COLOR_UNAVAILABLE,
            None,
            (CssRect(160, 100, 120, 40, "color"),),
            ("color",),
        ),
        "sold_out": (
            BusinessOutcome.SOLD_OUT,
            None,
            (CssRect(20, 180, 120, 40, "stock_status"),),
            ("stock_status",),
        ),
    }
    outcome, price, rectangles, roles = cases[state]
    return FixtureObservation(
        outcome=outcome,
        price=price,
        url=f"https://fixture.test/{state}",
        css_rectangles=rectangles,
        expected_roles=roles,
        expected_window=BrowserWindowIdentity("fixture", 42, "fixture-window"),
        stability_probe=_Probe(),
    )


def _run(tmp_path: Path, run_id: str = "fixture-run") -> RunRecord:
    fingerprints = tuple(
        InputFingerprint(
            source_role=role,
            path=tmp_path / f"{role}.xlsx",
            sha256=str(index) * 64,
            byte_size=1,
            modified_ns=1,
        )
        for index, role in enumerate(("base", "marketing", "bop"), 1)
    )
    return RunRecord(
        run_id=run_id,
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=fingerprints,
        output_dir=tmp_path / "output",
        browser_profile_dir=tmp_path / "profile",
        associated_rows_snapshot="[]",
        associated_rows_snapshot_path=None,
        associated_rows_snapshot_sha256=hashlib.sha256(b"[]").hexdigest(),
        quote_path=None,
        report_path=None,
        state=RunState.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )


def _task(
    task_id: str,
    *,
    output_row: int,
    brand: str = "小米",
    channel: WebsiteChannel = WebsiteChannel.JD,
    model: str = "小米 15",
    ram: str = "12GB",
    storage: str = "256GB",
    color: str = "黑色",
) -> WebsiteTask:
    return WebsiteTask(
        task_id=task_id,
        run_id="fixture-run",
        source_row_number=output_row + 10,
        output_row_number=output_row,
        material_code=f"CODE-{output_row}",
        brand=brand,
        model_name=model,
        ram=ram,
        storage=storage,
        color=color,
        channel=channel,
    )


def _runner(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
    fixtures_by_task: dict[str, Path],
    *,
    capture: _Capture | None = None,
    adapter=_adapter,
    events: list[WorkerEvent] | None = None,
    manual_login_callback=None,
) -> tuple[WebsiteTaskRunner, _Session, _Capture]:
    session = _Session(fixtures_by_task)
    selected_capture = capture or _Capture()

    def diagnostic(task: WebsiteTask, _error: BaseException, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"diagnostic:{task.task_id}".encode())
        return path

    runner = WebsiteTaskRunner(
        repository=repository,
        run_id="fixture-run",
        browser_session=session,
        adapter=adapter,
        evidence_capture=selected_capture,
        evidence_dir=tmp_path / "evidence",
        diagnostic_capture=diagnostic,
        retry_policy=RetryPolicy(technical_retries=0, retry_delay_seconds=0),
        event_sink=events.append if events is not None else None,
        manual_login_callback=manual_login_callback,
    )
    return runner, session, selected_capture


def test_fixture_outcomes_frames_sorting_and_output_mapping(
    tmp_path: Path,
) -> None:
    states = (
        "normal",
        "no_model",
        "capacity_disabled",
        "color_disabled",
        "sold_out",
    )
    tasks = tuple(
        _task(state, output_row=row)
        for row, state in zip((8, 4, 7, 3, 6), states, strict=True)
    )
    fixtures = {state: FIXTURES / f"{state}.html" for state in states}
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, page, capture = _runner(repository, tmp_path, fixtures)

        runner.run(tuple(reversed(tasks)))

        assert page.loads == [
            task.task_id
            for task in sorted(
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
        ]
        assert {
            repository.load_task(task.task_id).output_row_number
            for task in tasks
        } == {3, 4, 6, 7, 8}
        outcomes = {
            task.task_id: cast(
                WebsiteResult,
                repository.load_result(task.task_id),
            ).outcome
            for task in tasks
        }
        assert outcomes == {
            "normal": BusinessOutcome.PRICE_FOUND,
            "no_model": BusinessOutcome.NO_MODEL,
            "capacity_disabled": BusinessOutcome.CAPACITY_UNAVAILABLE,
            "color_disabled": BusinessOutcome.COLOR_UNAVAILABLE,
            "sold_out": BusinessOutcome.SOLD_OUT,
        }
        roles = {
            state: tuple(
                next(
                    request
                    for request in capture.requests
                    if request.destination.name.split(".g", 1)[0]
                    == safe_task_file_stem(state)
                ).expected_roles
            )
            for state in states
        }
        assert roles == {
            "normal": (),
            "no_model": ("search_keyword", "result_region"),
            "capacity_disabled": ("capacity",),
            "color_disabled": ("color",),
            "sold_out": ("stock_status",),
        }


def test_stable_sort_uses_every_business_key_and_row_tie_break(
    tmp_path: Path,
) -> None:
    tasks = (
        _task("brand", output_row=2, brand="Z"),
        _task("model", output_row=2, brand="A", model="Z"),
        _task("ram", output_row=2, brand="A", model="A", ram="Z"),
        _task(
            "storage",
            output_row=2,
            brand="A",
            model="A",
            ram="A",
            storage="Z",
        ),
        _task(
            "color",
            output_row=2,
            brand="A",
            model="A",
            ram="A",
            storage="A",
            color="Z",
        ),
        _task(
            "channel",
            output_row=2,
            brand="A",
            model="A",
            ram="A",
            storage="A",
            color="A",
            channel=WebsiteChannel.TMALL,
        ),
        _task(
            "row-later",
            output_row=9,
            brand="A",
            model="A",
            ram="A",
            storage="A",
            color="A",
            channel=WebsiteChannel.JD,
        ),
        _task(
            "row-earlier",
            output_row=3,
            brand="A",
            model="A",
            ram="A",
            storage="A",
            color="A",
            channel=WebsiteChannel.JD,
        ),
    )
    fixtures = {
        task.task_id: FIXTURES / "normal.html" for task in tasks
    }
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, page, _capture = _runner(repository, tmp_path, fixtures)

        runner.run(tuple(reversed(tasks)))

        assert page.loads == [
            "row-earlier",
            "row-later",
            "channel",
            "color",
            "storage",
            "ram",
            "model",
            "brand",
        ]


def test_login_parks_without_retry_while_other_task_completes(
    tmp_path: Path,
) -> None:
    tasks = (
        _task(
            "login",
            output_row=2,
            brand="A",
            channel=WebsiteChannel.TMALL,
        ),
        _task(
            "same-site",
            output_row=3,
            brand="B",
            channel=WebsiteChannel.TMALL,
        ),
        _task(
            "normal",
            output_row=4,
            brand="C",
            channel=WebsiteChannel.JD,
        ),
    )
    fixtures = {
        "login": FIXTURES / "login_required.html",
        "same-site": FIXTURES / "normal.html",
        "normal": FIXTURES / "normal.html",
    }
    events: list[WorkerEvent] = []
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            events=events,
        )

        runner.run(tasks)

        assert page.loads == ["login", "normal"]
        assert repository.task_state("login") is TaskState.WAITING_FOR_LOGIN
        assert repository.attempt_count("login") == 1
        assert repository.task_state("same-site") is TaskState.PENDING
        assert repository.attempt_count("same-site") == 0
        assert repository.task_state("normal") is TaskState.SUCCEEDED
        assert [event.event for event in events] == [
            "progress",
            "waiting_for_login",
            "progress",
            "result",
        ]
        assert set(page.pages) == {"tmall", "jd"}


def test_manual_login_reuses_preserved_site_page_and_resumes_only_that_site(
    tmp_path: Path,
) -> None:
    tasks = (
        _task(
            "login",
            output_row=2,
            brand="A",
            channel=WebsiteChannel.TMALL,
        ),
        _task(
            "same-site",
            output_row=3,
            brand="B",
            channel=WebsiteChannel.TMALL,
        ),
        _task(
            "other-site",
            output_row=4,
            brand="C",
            channel=WebsiteChannel.JD,
        ),
    )
    fixtures = {
        "login": FIXTURES / "login_required.html",
        "same-site": FIXTURES / "normal.html",
        "other-site": FIXTURES / "normal.html",
    }
    manual_sites: list[str] = []

    def confirm_fixture_login(site: str) -> None:
        manual_sites.append(site)
        fixtures["login"] = FIXTURES / "normal.html"

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, session, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            manual_login_callback=confirm_fixture_login,
        )
        runner.run(tasks)
        preserved_page = session.pages["tmall"]
        other_attempts = repository.attempt_count("other-site")

        runner.scheduler.enter_manual_login("tmall")
        runner.scheduler.confirm_manual_login("tmall")
        runner.scheduler.run_until_idle()

        assert manual_sites == ["tmall"]
        assert session.pages["tmall"] is preserved_page
        assert preserved_page.front_count == 1
        assert repository.task_state("login") is TaskState.SUCCEEDED
        assert repository.task_state("same-site") is TaskState.SUCCEEDED
        assert repository.task_state("other-site") is TaskState.SUCCEEDED
        assert repository.attempt_count("other-site") == other_attempts


def test_official_site_family_isolated_by_brand(tmp_path: Path) -> None:
    blocked = _task(
        "blocked",
        output_row=2,
        brand="品牌甲",
        channel=WebsiteChannel.OFFICIAL,
    )
    unrelated = _task(
        "unrelated",
        output_row=3,
        brand="品牌乙",
        channel=WebsiteChannel.OFFICIAL,
    )
    fixtures = {
        "blocked": FIXTURES / "login_required.html",
        "unrelated": FIXTURES / "normal.html",
    }

    def official_adapter(
        task: WebsiteTask,
        page: _Page,
    ) -> FixtureObservation:
        if task.task_id == "blocked":
            page.load(task.task_id)
            raise LoginRequired(
                f"official:{task.brand}",
                "fixture login required",
            )
        return _adapter(task, page)

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, session, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            adapter=official_adapter,
        )
        runner.run((unrelated, blocked))

        assert repository.task_state("blocked") is TaskState.WAITING_FOR_LOGIN
        assert repository.task_state("unrelated") is TaskState.SUCCEEDED
        assert set(session.pages) == {
            "official:品牌甲",
            "official:品牌乙",
        }


def test_capture_failure_is_diagnostic_only_and_never_business_no(
    tmp_path: Path,
) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        events: list[WorkerEvent] = []
        runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=_Capture(failing_task="normal"),
            events=events,
        )

        runner.run((task,))

        result = repository.load_result(task.task_id)
        assert result is not None
        assert result.state is TaskState.TECHNICAL_FAILURE
        assert result.outcome is None
        assert result.price is None
        assert result.evidence is None
        assert result.diagnostic_path is not None
        assert result.diagnostic_path.exists()
        assert not tuple(
            (tmp_path / "evidence").glob(
                f"{safe_task_file_stem('normal')}.g*.png"
            )
        )
        assert [event.event for event in events] == [
            "progress",
            "technical_failure",
        ]


def test_adapter_technical_failure_also_gets_diagnostic(
    tmp_path: Path,
) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}

    def fail_adapter(_task: WebsiteTask, _page: _Page) -> FixtureObservation:
        raise TimeoutError("fixture layout wait")

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, _page, capture = _runner(
            repository,
            tmp_path,
            fixtures,
            adapter=fail_adapter,
        )

        runner.run((task,))

        result = repository.load_result(task.task_id)
        assert result is not None
        assert result.error_code == "PLAYWRIGHT_TIMEOUT"
        assert result.diagnostic_path is not None
        assert result.diagnostic_path.exists()
        assert capture.requests == []


def test_result_validation_failure_removes_formal_and_records_diagnostic(
    tmp_path: Path,
) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}

    def invalid_result_adapter(
        task: WebsiteTask,
        page: _Page,
    ) -> FixtureObservation:
        return replace(_adapter(task, page), url="file:///not-http")

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, _session, capture = _runner(
            repository,
            tmp_path,
            fixtures,
            adapter=invalid_result_adapter,
        )
        runner.run((task,))

        result = repository.load_result(task.task_id)
        assert result is not None
        assert result.state is TaskState.TECHNICAL_FAILURE
        assert result.evidence is None
        assert result.diagnostic_path is not None
        assert len(capture.requests) == 1
        assert not capture.requests[0].destination.exists()


def test_external_diagnostic_path_is_not_persisted(tmp_path: Path) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}
    session = _Session(fixtures)
    capture = _Capture(failing_task="normal")
    outside = tmp_path / "outside-diagnostic.png"

    def external_diagnostic(
        _task: WebsiteTask,
        _error: BaseException,
        _requested: Path,
    ) -> Path:
        outside.write_bytes(b"outside")
        return outside

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner = WebsiteTaskRunner(
            repository=repository,
            run_id="fixture-run",
            browser_session=session,
            adapter=_adapter,
            evidence_capture=capture,
            evidence_dir=tmp_path / "evidence",
            diagnostic_capture=external_diagnostic,
            retry_policy=RetryPolicy(
                technical_retries=0,
                retry_delay_seconds=0,
            ),
        )
        runner.run((task,))

        result = repository.load_result(task.task_id)
        assert result is not None
        assert result.diagnostic_path is None
        assert outside.is_file()


def test_symlinked_diagnostics_directory_is_rejected_before_callback(
    tmp_path: Path,
) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}
    session = _Session(fixtures)
    capture = _Capture(failing_task="normal")
    evidence_dir = tmp_path / "evidence"
    outside = tmp_path / "outside"
    evidence_dir.mkdir()
    outside.mkdir()
    (evidence_dir / "diagnostics").symlink_to(
        outside,
        target_is_directory=True,
    )
    diagnostic_called = False

    def should_not_run(
        _task: WebsiteTask,
        _error: BaseException,
        _requested: Path,
    ) -> Path:
        nonlocal diagnostic_called
        diagnostic_called = True
        raise AssertionError("diagnostic callback must not run")

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner = WebsiteTaskRunner(
            repository=repository,
            run_id="fixture-run",
            browser_session=session,
            adapter=_adapter,
            evidence_capture=capture,
            evidence_dir=evidence_dir,
            diagnostic_capture=should_not_run,
            retry_policy=RetryPolicy(
                technical_retries=0,
                retry_delay_seconds=0,
            ),
        )
        runner.run((task,))

        result = repository.load_result(task.task_id)
        assert result is not None
        assert result.diagnostic_path is None
        assert diagnostic_called is False
        assert tuple(outside.iterdir()) == ()


def test_retry_diagnostics_are_attempt_scoped_and_not_overwritten(
    tmp_path: Path,
) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}
    session = _Session(fixtures)
    capture = _Capture(failing_task="normal")
    diagnostic_paths: list[Path] = []

    def diagnostic(
        _task: WebsiteTask,
        _error: BaseException,
        requested: Path,
    ) -> Path:
        requested.parent.mkdir(parents=True, exist_ok=True)
        requested.write_bytes(requested.name.encode())
        diagnostic_paths.append(requested)
        return requested

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner = WebsiteTaskRunner(
            repository=repository,
            run_id="fixture-run",
            browser_session=session,
            adapter=_adapter,
            evidence_capture=capture,
            evidence_dir=tmp_path / "evidence",
            diagnostic_capture=diagnostic,
            retry_policy=RetryPolicy(
                technical_retries=1,
                retry_delay_seconds=0,
            ),
        )
        runner.run((task,))

        assert len(diagnostic_paths) == 2
        assert diagnostic_paths[0] != diagnostic_paths[1]
        assert ".a1.png" in diagnostic_paths[0].name
        assert ".a2.png" in diagnostic_paths[1].name
        assert all(path.is_file() for path in diagnostic_paths)
        result = repository.load_result(task.task_id)
        assert result is not None
        assert result.diagnostic_path == diagnostic_paths[1].resolve()


@pytest.mark.parametrize("damage", ["missing", "hash_mismatch"])
def test_validated_success_skips_and_damaged_evidence_is_recaptured(
    tmp_path: Path,
    damage: str,
) -> None:
    task = _task("normal", output_row=2)
    fixtures = {"normal": FIXTURES / "normal.html"}
    database = tmp_path / "state.sqlite3"

    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_run(tmp_path))
        first_capture = _Capture()
        runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=first_capture,
        )
        runner.run((task,))
        first_result = repository.load_result(task.task_id)
        assert first_result is not None and first_result.evidence is not None
        first_path = first_result.evidence.path
        assert repository.attempt_count(task.task_id) == 1

        skip_capture = _Capture()
        skip_runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=skip_capture,
        )
        skip_runner.run((task,))
        assert skip_capture.requests == []
        assert repository.attempt_count(task.task_id) == 1

        if damage == "missing":
            first_path.unlink()
        else:
            first_path.write_bytes(b"corrupted fixture evidence")
        repair_capture = _Capture()
        repair_runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=repair_capture,
        )
        repair_runner.run((task,))

        repaired = repository.load_result(task.task_id)
        assert repaired is not None
        assert repaired.state is TaskState.SUCCEEDED
        assert repaired.evidence is not None
        assert repaired.evidence.path.is_file()
        assert len(repair_capture.requests) == 1
        assert repository.attempt_count(task.task_id) == 2


def test_failed_new_generation_preserves_prior_validated_evidence(
    tmp_path: Path,
) -> None:
    original = _task("normal", output_row=2)
    changed = replace(original, color="白色")
    fixtures = {"normal": FIXTURES / "normal.html"}
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        first_runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
        )
        first_runner.run((original,))
        prior = repository.load_result(original.task_id)
        assert prior is not None and prior.evidence is not None
        prior_path = prior.evidence.path
        prior_payload = prior_path.read_bytes()

        failing_runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=_Capture(failing_task="normal"),
        )
        failing_runner.run((changed,))

        current = repository.load_result(changed.task_id)
        assert current is not None
        assert current.state is TaskState.TECHNICAL_FAILURE
        assert current.evidence is None
        assert prior_path.read_bytes() == prior_payload
        assert prior_path.is_file()


def test_two_concurrent_run_calls_never_overlap_visible_attempts(
    tmp_path: Path,
) -> None:
    tasks = (
        _task("first", output_row=2),
        _task("second", output_row=3),
    )
    fixtures = {
        task.task_id: FIXTURES / "normal.html" for task in tasks
    }
    active = 0
    maximum_active = 0
    active_lock = threading.Lock()

    def observed_adapter(
        task: WebsiteTask,
        page: _Page,
    ) -> FixtureObservation:
        nonlocal active, maximum_active
        with active_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.01)
            return _adapter(task, page)
        finally:
            with active_lock:
                active -= 1

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            adapter=observed_adapter,
        )
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                runner.run(tasks)
            except BaseException as error:
                errors.append(error)

        threads = (threading.Thread(target=invoke), threading.Thread(target=invoke))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert maximum_active == 1
        assert repository.attempt_count("first") == 1
        assert repository.attempt_count("second") == 1


def test_interruption_restart_skips_validated_success(
    tmp_path: Path,
) -> None:
    first = _task("normal", output_row=2)
    second = _task("sold_out", output_row=3)
    fixtures = {
        "normal": FIXTURES / "normal.html",
        "sold_out": FIXTURES / "sold_out.html",
    }
    database = tmp_path / "state.sqlite3"
    capture = _Capture()
    interrupted = False

    def interrupt_second(task: WebsiteTask, page: _Page) -> FixtureObservation:
        nonlocal interrupted
        if task.task_id == "sold_out" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return _adapter(task, page)

    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_run(tmp_path))
        runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=capture,
            adapter=interrupt_second,
        )
        with pytest.raises(KeyboardInterrupt):
            runner.run((second, first))
        assert repository.task_state("normal") is TaskState.SUCCEEDED
        assert repository.task_state("sold_out") is TaskState.RUNNING

    with SQLiteTaskRepository(database) as repository:
        runner, _page, _capture = _runner(
            repository,
            tmp_path,
            fixtures,
            capture=capture,
        )
        runner.run((second, first))
        assert repository.task_state("normal") is TaskState.SUCCEEDED
        assert repository.task_state("sold_out") is TaskState.SUCCEEDED
        assert [
            request.destination.name for request in capture.requests
        ] == [
            f"{safe_task_file_stem('normal')}.g0.a1.png",
            f"{safe_task_file_stem('sold_out')}.g0.a2.png",
        ]


def test_task_id_cannot_escape_evidence_directory(tmp_path: Path) -> None:
    task = _task("../outside/evil", output_row=2)
    fixtures = {task.task_id: FIXTURES / "normal.html"}
    evidence_dir = (tmp_path / "evidence").resolve()
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        runner, _page, capture = _runner(
            repository,
            tmp_path,
            fixtures,
        )

        runner.run((task,))

        assert len(capture.requests) == 1
        path = capture.requests[0].destination
        assert path.parent == evidence_dir
        assert path.name.startswith("task-")
        assert "/" not in path.name
        assert ".." not in path.name
        assert not (tmp_path / "outside").exists()

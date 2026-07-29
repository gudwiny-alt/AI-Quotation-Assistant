from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import EvidenceRecord
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureContext,
    CaptureRequest,
    make_capture_error,
)
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.catalog import SiteSpec
from quote_app.sites.official import OfficialSiteAdapter
from quote_app.sites.protocol import AdapterObservation, BrowserPage
from quote_app.sites.registry import AdapterRegistry
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
from quote_app.tasks.retry import LoginRequired

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


class _Probe:
    def semantic_hash(self) -> str:
        return "stable"


class _Page:
    @property
    def url(self) -> str:
        return "https://example.test/product"


class _Session:
    opened = 0
    closed = 0
    pages_by_family: dict[str, object] = {}

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.page = _Page()

    def __enter__(self):
        type(self).opened += 1
        return self

    def __exit__(self, *_exc_info: object) -> None:
        type(self).closed += 1

    def page_for(self, _site_family: str) -> _Page:
        return self.pages_by_family.get(_site_family, self.page)  # type: ignore[return-value]


class _Adapter:
    def __init__(self, spec: SiteSpec) -> None:
        self.spec = spec
        self.channel = spec.channel

    def observe(
        self,
        task: WebsiteTask,
        _page: BrowserPage,
    ) -> AdapterObservation:
        if task.task_id == "waiting":
            raise LoginRequired("jd", "京东需要人工登录")
        state = VerifiedSemanticState(
            canonical_url="https://example.test/product",
            brand=task.brand,
            model_name=task.model_name,
            capacity=f"{task.ram}+{task.storage}",
            color=task.color,
            current_sku="sku-1",
            region="福建",
            stock_state="有货",
            price=Decimal("3999"),
            outcome=BusinessOutcome.PRICE_FOUND,
            css_rectangles=(),
        )
        return AdapterObservation(
            outcome=BusinessOutcome.PRICE_FOUND,
            price=Decimal("3999"),
            url=state.canonical_url,
            css_rectangles=(),
            semantic_state=state,
        )

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("production runner must use observe")


class _Capture:
    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        payload = b"formal-macos-evidence"
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(payload)
        return EvidenceRecord(
            state=request.state,
            path=request.destination,
            sha256=hashlib.sha256(payload).hexdigest(),
            pixel_width=800,
            pixel_height=600,
            captured_at=NOW,
            validation_code="CAPTURE_OK",
        )


class _Runtime:
    def __init__(self) -> None:
        self.capture = _Capture()

    def evidence_capture(self) -> _Capture:
        return self.capture

    def capture_context_provider(
        self,
        task: WebsiteTask,
        _page: object,
        _state: VerifiedSemanticState,
    ) -> CaptureContext:
        if task.task_id == "permission":
            raise make_capture_error(
                "CAPTURE_PERMISSION",
                "macOS screen recording is unavailable",
            )
        return CaptureContext(
            expected_window=BrowserWindowIdentity("macos", 42, "window-1"),
            stability_probe=_Probe(),
        )


def _run(tmp_path: Path) -> RunRecord:
    return RunRecord(
        run_id="run-1",
        schema_version=SCHEMA_VERSION,
        quote_month=QuoteMonth(2026, 8),
        input_fingerprints=tuple(
            InputFingerprint(
                source_role=role,
                path=tmp_path / f"{role}.xlsx",
                sha256=str(index) * 64,
                byte_size=1,
                modified_ns=1,
            )
            for index, role in enumerate(("base", "marketing", "bop"), 1)
        ),
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


def test_service_composes_real_runner_and_preserves_partial_macos_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from quote_app.services import web_run

    _Session.opened = 0
    _Session.closed = 0
    _Session.pages_by_family = {}

    tasks = (
        _task("success", WebsiteChannel.JD, 2),
        _task("waiting", WebsiteChannel.JD, 3),
        _task("permission", WebsiteChannel.JD, 4),
    )
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_run(tmp_path))

    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: _Adapter,
            WebsiteChannel.OFFICIAL: _Adapter,
        }
    )
    runtime_registries: list[AdapterRegistry] = []
    monkeypatch.setattr(web_run, "PersistentBrowserSession", _Session)
    monkeypatch.setattr(web_run, "default_registry", lambda: registry)

    def runtime_factory(received: AdapterRegistry) -> _Runtime:
        runtime_registries.append(received)
        return _Runtime()

    summary = web_run.run_website_tasks(
        web_run.WebsiteRunRequest(
            run_id="run-1",
            tasks=tasks,
            profile_dir=tmp_path / "profile",
            evidence_dir=tmp_path / "evidence",
            database_path=database,
        ),
        runtime_factory=runtime_factory,
    )

    assert runtime_registries == [registry]
    assert summary.succeeded == 1
    assert summary.waiting_for_login == 1
    assert summary.technical_failure == 0
    assert summary.waiting_sites == ("jd",)
    assert summary.technical_failure_codes == ()
    assert len(summary.evidence_paths) == 1
    assert summary.evidence_paths[0].is_file()
    assert _Session.opened == 1
    assert _Session.closed == 1
    with SQLiteTaskRepository(database) as repository:
        assert repository.task_state("waiting") is TaskState.WAITING_FOR_LOGIN
        assert repository.task_state("permission") is TaskState.PENDING
        assert repository.task_state("success") is TaskState.SUCCEEDED
        assert repository.attempt_count("permission") == 0


def test_official_login_pauses_before_later_channel_and_resumes_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    official_case: Any,
) -> None:
    from quote_app.services import web_run

    _adapter, _fixture_task, login_page, _capture = official_case(
        "小米",
        "normal",
        mutate=lambda html: html.replace(
            '<main data-screen="store">',
            (
                '<main data-screen="store">'
                '<div data-official-role="login">请登录</div>'
            ),
            1,
        ),
    )
    waiting = _task("official-waiting", WebsiteChannel.OFFICIAL, 2)
    succeeded = _task("jd-success", WebsiteChannel.JD, 3)
    tasks = (waiting, succeeded)
    database = tmp_path / "state.sqlite3"
    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_run(tmp_path))

    registry = AdapterRegistry(
        factories={
            WebsiteChannel.JD: _Adapter,
            WebsiteChannel.OFFICIAL: OfficialSiteAdapter,
        }
    )
    _Session.opened = 0
    _Session.closed = 0
    _Session.pages_by_family = {"quotation-automation": login_page}
    monkeypatch.setattr(web_run, "PersistentBrowserSession", _Session)
    monkeypatch.setattr(web_run, "default_registry", lambda: registry)
    request = web_run.WebsiteRunRequest(
        run_id="run-1",
        tasks=tasks,
        profile_dir=tmp_path / "profile",
        evidence_dir=tmp_path / "evidence",
        database_path=database,
    )

    first = web_run.run_website_tasks(
        request,
        runtime_factory=lambda _registry: _Runtime(),
    )
    resumed = web_run.run_website_tasks(
        request,
        runtime_factory=lambda _registry: _Runtime(),
    )

    assert first.waiting_for_login == 1
    assert first.technical_failure == 0
    assert first.succeeded == 0
    assert first.waiting_sites == ("official:小米",)
    assert resumed.waiting_for_login == 1
    assert resumed.technical_failure == 0
    assert resumed.succeeded == 0
    with SQLiteTaskRepository(database) as repository:
        assert repository.task_state(waiting.task_id) is TaskState.WAITING_FOR_LOGIN
        assert repository.task_state(succeeded.task_id) is TaskState.PENDING
        assert repository.attempt_count(waiting.task_id) == 1
        assert repository.attempt_count(succeeded.task_id) == 0
    assert _Session.opened == 2
    assert _Session.closed == 2

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.geometry import CssRect
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceRectangle,
    EvidenceState,
    MacCapturePolicy,
)
from quote_app.evidence.platform import (
    BrowserWindowIdentity,
    CaptureContext,
    CaptureRequest,
    NonRetryableEvidenceCaptureError,
)
from quote_app.evidence.semantic_state import VerifiedSemanticState
from quote_app.sites.catalog import SiteSpec, load_site_catalog
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
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.repository import SQLiteTaskRepository
from quote_app.tasks.retry import RetryPolicy
from quote_app.tasks.runner import (
    WebsiteTaskRunner,
    safe_task_file_stem,
    task_sort_key,
)

NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def _xiaomi_jd_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.JD
    )


def _xiaomi_official_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.OFFICIAL
    )


class _Probe:
    def semantic_hash(self) -> str:
        return "stable"


class _Page:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []
        self.wait_calls: list[int] = []

    @property
    def url(self) -> str:
        return "https://example.test/product"

    def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)


class _Session:
    def __init__(self) -> None:
        self.page = _Page()
        self.families: list[str] = []

    def page_for(self, site_family: str) -> _Page:
        self.families.append(site_family)
        return self.page


class _RecordingCapture:
    def __init__(self, validation_code: str = "CAPTURE_OK") -> None:
        self.requests: list[CaptureRequest] = []
        self.validation_code = validation_code
        self.fail_codes: list[str | None] = []

    def capture(self, request: CaptureRequest) -> EvidenceRecord:
        self.requests.append(request)
        if self.fail_codes:
            code = self.fail_codes.pop(0)
            if code is not None:
                raise NonRetryableEvidenceCaptureError(
                    code,
                    "fixture capture failed",
                )
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        payload = request.state.value.encode()
        request.destination.write_bytes(payload)
        return EvidenceRecord(
            state=request.state,
            path=request.destination,
            sha256=hashlib.sha256(payload).hexdigest(),
            pixel_width=800,
            pixel_height=600,
            captured_at=NOW,
            validation_code=self.validation_code,
            annotations=tuple(
                EvidenceRectangle(rectangle.role, 1, 1, 10, 10)
                for rectangle in request.css_rectangles
            ),
        )


class _ObservationAdapter:
    def __init__(self, spec: SiteSpec, observation: AdapterObservation) -> None:
        self.spec = spec
        self.channel = spec.channel
        self.observation = observation
        self.observed: list[tuple[WebsiteTask, BrowserPage]] = []
        self.execute_called = False

    def observe(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        self.observed.append((task, page))
        return self.observation

    def execute(self, *args: Any, **kwargs: Any) -> None:
        self.execute_called = True
        raise AssertionError("runner must not use direct adapter execution")


class _ResumableObservationAdapter(_ObservationAdapter):
    def __init__(self, spec: SiteSpec, observation: AdapterObservation) -> None:
        super().__init__(spec, observation)
        self.resumed: list[
            tuple[WebsiteTask, BrowserPage, WebsiteObservationCheckpoint]
        ] = []

    def resume(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        self.resumed.append((task, page, checkpoint))
        page.goto(checkpoint.url)  # type: ignore[attr-defined]
        return self.observation


class _OfficialObservationAdapter(_ObservationAdapter):
    def __init__(
        self,
        spec: SiteSpec,
        observation: AdapterObservation,
        official_detail_url: str,
    ) -> None:
        super().__init__(spec, observation)
        self.official_detail_url = official_detail_url

    def observe(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        page.goto(self.official_detail_url)
        return super().observe(task, page)


class _ExecuteOnlyAdapter:
    def __init__(self, spec: SiteSpec) -> None:
        self.spec = spec
        self.channel = spec.channel
        self.execute_called = False

    def execute(self, *args: Any, **kwargs: Any) -> None:
        self.execute_called = True
        raise AssertionError("runner must not use direct adapter execution")


def _task() -> WebsiteTask:
    return WebsiteTask(
        task_id="task-1",
        run_id="run-1",
        source_row_number=11,
        output_row_number=2,
        material_code="CODE-1",
        brand="小米",
        model_name="小米 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=WebsiteChannel.JD,
    )


def test_task_sort_is_brand_then_channel_then_output_row() -> None:
    base = _task()
    tasks = (
        replace(
            base,
            task_id="tmall-row-2",
            channel=WebsiteChannel.TMALL,
            output_row_number=2,
        ),
        replace(
            base,
            task_id="jd-row-3",
            channel=WebsiteChannel.JD,
            output_row_number=3,
        ),
        replace(
            base,
            task_id="official-row-3",
            channel=WebsiteChannel.OFFICIAL,
            output_row_number=3,
        ),
        replace(
            base,
            task_id="jd-row-2",
            channel=WebsiteChannel.JD,
            output_row_number=2,
        ),
        replace(
            base,
            task_id="official-row-2",
            channel=WebsiteChannel.OFFICIAL,
            output_row_number=2,
        ),
        replace(
            base,
            task_id="tmall-row-3",
            channel=WebsiteChannel.TMALL,
            output_row_number=3,
        ),
    )

    assert [
        (task.channel, task.output_row_number)
        for task in sorted(tasks, key=task_sort_key)
    ] == [
        (WebsiteChannel.OFFICIAL, 2),
        (WebsiteChannel.OFFICIAL, 3),
        (WebsiteChannel.JD, 2),
        (WebsiteChannel.JD, 3),
        (WebsiteChannel.TMALL, 2),
        (WebsiteChannel.TMALL, 3),
    ]


def _run(tmp_path: Path) -> RunRecord:
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
        run_id="run-1",
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


def _observation(outcome: BusinessOutcome) -> AdapterObservation:
    rectangles = {
        BusinessOutcome.PRICE_FOUND: (),
        BusinessOutcome.NO_MODEL: (
            CssRect(1, 1, 10, 10, "search_keyword"),
            CssRect(2, 2, 10, 10, "result_region"),
        ),
        BusinessOutcome.CAPACITY_UNAVAILABLE: (
            CssRect(1, 1, 10, 10, "capacity"),
        ),
        BusinessOutcome.COLOR_UNAVAILABLE: (
            CssRect(1, 1, 10, 10, "color"),
        ),
        BusinessOutcome.SOLD_OUT: (
            CssRect(1, 1, 10, 10, "stock_status"),
        ),
    }
    price = Decimal("3999") if outcome is BusinessOutcome.PRICE_FOUND else None
    is_not_applicable = outcome in {
        BusinessOutcome.NO_MODEL,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        BusinessOutcome.COLOR_UNAVAILABLE,
    }
    semantic_state = VerifiedSemanticState(
        canonical_url="https://example.test/product",
        brand="小米",
        model_name="小米 15",
        capacity="12GB+256GB",
        color="黑色",
        current_sku=(
            "not-applicable"
            if is_not_applicable
            else "fixture-sku-1"
        ),
        region=(
            "not-applicable"
            if is_not_applicable
            else "fixture-region"
        ),
        stock_state=(
            "not-applicable"
            if is_not_applicable
            else (
                "有货"
                if outcome is BusinessOutcome.PRICE_FOUND
                else "售罄"
            )
        ),
        price=price,
        outcome=outcome,
        css_rectangles=rectangles[outcome],
    )
    return AdapterObservation(
        outcome=outcome,
        price=price,
        url="https://example.test/product",
        css_rectangles=rectangles[outcome],
        semantic_state=semantic_state,
    )


def _registry(adapter: object) -> AdapterRegistry:
    def factory(spec: SiteSpec) -> object:
        setattr(adapter, "spec", spec)
        setattr(adapter, "channel", spec.channel)
        return adapter

    return AdapterRegistry(factories={WebsiteChannel.JD: factory})  # type: ignore[arg-type]


def _context(
    _task: WebsiteTask,
    _page: _Page,
    _semantic_state: VerifiedSemanticState,
) -> CaptureContext:
    return CaptureContext(
        expected_window=BrowserWindowIdentity("fixture", 1, "window-1"),
        stability_probe=_Probe(),
    )


def _runner(
    repository: SQLiteTaskRepository,
    tmp_path: Path,
    *,
    adapter_registry: AdapterRegistry,
    capture: _RecordingCapture,
    context_provider: Any = _context,
    capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT,
    browser_session: _Session | None = None,
) -> WebsiteTaskRunner:
    return WebsiteTaskRunner(
        repository=repository,
        run_id="run-1",
        browser_session=browser_session or _Session(),
        adapter_registry=adapter_registry,
        capture_context_provider=context_provider,
        evidence_capture=capture,
        evidence_dir=tmp_path / "evidence",
        retry_policy=RetryPolicy(technical_retries=0, retry_delay_seconds=0),
        capture_acceptance_policy=capture_acceptance_policy,
    )


@dataclass(slots=True)
class RunnerCase:
    runner: WebsiteTaskRunner
    task: WebsiteTask
    repository: SQLiteTaskRepository
    capture: _RecordingCapture
    page: _Page
    official_detail_url: str


@pytest.fixture
def runner_case(tmp_path: Path) -> Iterator[RunnerCase]:
    task = replace(_task(), channel=WebsiteChannel.OFFICIAL)
    page = _Page()
    session = _Session()
    session.page = page
    capture = _RecordingCapture()
    official_detail_url = "https://www.mi.com/shop/buy/detail?product_id=1"
    observation = _observation(BusinessOutcome.PRICE_FOUND)
    observation = replace(
        observation,
        price=Decimal("4999"),
        semantic_state=replace(observation.semantic_state, price=Decimal("4999")),
    )
    adapter = _OfficialObservationAdapter(
        _xiaomi_official_spec(),
        observation,
        official_detail_url,
    )

    def factory(spec: SiteSpec) -> _OfficialObservationAdapter:
        adapter.spec = spec
        adapter.channel = spec.channel
        return adapter

    repository = SQLiteTaskRepository(tmp_path / "state.sqlite3")
    repository.create_run(_run(tmp_path))
    try:
        yield RunnerCase(
            runner=_runner(
                repository,
                tmp_path,
                adapter_registry=AdapterRegistry(
                    factories={WebsiteChannel.OFFICIAL: factory}
                ),
                capture=capture,
                browser_session=session,
            ),
            task=task,
            repository=repository,
            capture=capture,
            page=page,
            official_detail_url=official_detail_url,
        )
    finally:
        repository.close()


def test_capture_failure_keeps_the_saved_observation_and_retries_same_page(
    runner_case: RunnerCase,
) -> None:
    runner_case.capture.fail_codes = [
        "CAPTURE_FOREGROUND",
        "CAPTURE_FOREGROUND",
        None,
    ]

    results = runner_case.runner.run((runner_case.task,))

    checkpoint = runner_case.repository.load_observation(
        runner_case.task.task_id
    )
    assert checkpoint is not None
    assert checkpoint.price == Decimal("4999")
    assert runner_case.page.goto_calls == [runner_case.official_detail_url]
    assert results[0].state is TaskState.SUCCEEDED


def test_restart_resumes_saved_observation_without_repeating_store_search(
    tmp_path: Path,
) -> None:
    """Break caught: restart silently overwrites a durable observation by searching again."""
    task = _task()
    spec = _xiaomi_jd_spec()
    observation = _observation(BusinessOutcome.PRICE_FOUND)
    adapter = _ResumableObservationAdapter(spec, observation)
    database = tmp_path / "state.sqlite3"
    session = _Session()

    class InterruptCapture:
        def capture(self, _request: CaptureRequest) -> EvidenceRecord:
            raise SystemExit("process stopped after observation")

    with SQLiteTaskRepository(database) as repository:
        repository.create_run(_run(tmp_path))
        interrupted = WebsiteTaskRunner(
            repository=repository,
            run_id="run-1",
            browser_session=session,
            adapter_registry=_registry(adapter),
            capture_context_provider=_context,
            evidence_capture=InterruptCapture(),
            evidence_dir=tmp_path / "evidence",
        )
        with pytest.raises(SystemExit, match="process stopped"):
            interrupted.run((task,))
        saved = repository.load_observation(task.task_id)
        assert saved is not None

    with SQLiteTaskRepository(database) as repository:
        recovered = _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(adapter),
            capture=_RecordingCapture(),
            browser_session=session,
        )
        result = recovered.run((task,))
        assert repository.load_observation(task.task_id) == saved

    assert result[0].state is TaskState.SUCCEEDED
    assert len(adapter.observed) == 1
    assert [call[2] for call in adapter.resumed] == [saved]


@pytest.mark.parametrize("failures", [2, 3], ids=("then-success", "exhausted"))
def test_capture_context_failure_retries_on_the_same_observed_page(
    runner_case: RunnerCase,
    failures: int,
) -> None:
    original_provider = runner_case.runner.capture_context_provider
    calls = 0

    def provider(*args: object) -> CaptureContext:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise NonRetryableEvidenceCaptureError(
                "CAPTURE_FOREGROUND", "fixture context failed"
            )
        assert original_provider is not None
        return original_provider(*args)  # type: ignore[arg-type]

    runner_case.runner.capture_context_provider = provider

    results = runner_case.runner.run((runner_case.task,))

    assert calls == 3
    assert runner_case.page.goto_calls == [runner_case.official_detail_url]
    assert runner_case.page.wait_calls == [500, 500]
    assert results[0].state is (
        TaskState.TECHNICAL_FAILURE if failures == 3 else TaskState.SUCCEEDED
    )


def test_runner_uses_final_capture_rectangles_from_the_capture_context(
    runner_case: RunnerCase,
) -> None:
    """A site may scroll after observation, so capture geometry is read last."""

    final_rectangles = (
        CssRect(30, 40, 160, 50, "search_keyword"),
        CssRect(30, 110, 500, 300, "result_region"),
    )
    no_model_observation = _observation(BusinessOutcome.NO_MODEL)
    adapter = _ObservationAdapter(_xiaomi_jd_spec(), no_model_observation)
    runner_case.runner.adapter_registry = _registry(adapter)
    runner_case.runner.capture_context_provider = lambda *_args: CaptureContext(
        expected_window=BrowserWindowIdentity("fixture", 1, "window-1"),
        stability_probe=_Probe(),
        css_rectangles=final_rectangles,
    )
    runner_case.task = replace(runner_case.task, channel=WebsiteChannel.JD)

    results = runner_case.runner.run((runner_case.task,))

    assert results[0].state is TaskState.SUCCEEDED
    assert runner_case.capture.requests[-1].css_rectangles == final_rectangles


def test_runner_requires_exactly_one_source_and_scopes_capture_context(
    tmp_path: Path,
) -> None:
    spec = _xiaomi_jd_spec()
    registry = _registry(_ObservationAdapter(spec, _observation(BusinessOutcome.PRICE_FOUND)))
    common = {
        "run_id": "run-1",
        "browser_session": _Session(),
        "evidence_capture": _RecordingCapture(),
        "evidence_dir": tmp_path / "evidence",
    }
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        with pytest.raises(ValueError, match="exactly one"):
            WebsiteTaskRunner(repository=repository, **common)
        with pytest.raises(ValueError, match="exactly one"):
            WebsiteTaskRunner(
                repository=repository,
                adapter=lambda _task, _page: None,
                adapter_registry=registry,
                capture_context_provider=_context,
                **common,
            )
        with pytest.raises(ValueError, match="capture_context_provider"):
            WebsiteTaskRunner(
                repository=repository,
                adapter_registry=registry,
                **common,
            )
        with pytest.raises(ValueError, match="capture_context_provider"):
            WebsiteTaskRunner(
                repository=repository,
                adapter=lambda _task, _page: None,
                capture_context_provider=_context,
                **common,
            )


def test_runner_registry_path_owns_capture_request_and_result(tmp_path: Path) -> None:
    spec = _xiaomi_jd_spec()
    adapter = _ObservationAdapter(spec, _observation(BusinessOutcome.NO_MODEL))
    capture = _RecordingCapture()
    task = _task()
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        results = _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(adapter),
            capture=capture,
        ).run((task,))

    assert results[0].state is TaskState.SUCCEEDED
    assert results[0].outcome is BusinessOutcome.NO_MODEL
    assert results[0].evidence is not None
    assert len(adapter.observed) == 1
    assert adapter.observed[0][0] == task
    assert isinstance(adapter.observed[0][1], _Page)
    assert not adapter.execute_called
    assert len(capture.requests) == 1
    request = capture.requests[0]
    assert request.state is EvidenceState.NO_MODEL
    assert request.expected_roles == ("search_keyword", "result_region")
    assert request.destination.name == f"{safe_task_file_stem(task.task_id)}.g0.a1.png"


def test_runner_persists_safe_web_area_diagnostic_without_evidence_file(
    tmp_path: Path,
) -> None:
    spec = _xiaomi_jd_spec()
    task = _task()

    def unavailable_context(
        _task: WebsiteTask,
        _page: _Page,
        _semantic_state: VerifiedSemanticState,
    ) -> CaptureContext:
        raise NonRetryableEvidenceCaptureError(
            "CAPTURE_ACCESSIBILITY",
            "WEB_AREA_NOT_FOUND",
        )

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        results = _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(
                _ObservationAdapter(spec, _observation(BusinessOutcome.PRICE_FOUND))
            ),
            capture=_RecordingCapture(),
            context_provider=unavailable_context,
        ).run((task,))
        attempt = repository.latest_attempt(task.task_id)

    result = results[0]
    assert result.state is TaskState.TECHNICAL_FAILURE
    assert result.error_code == "CAPTURE_ACCESSIBILITY"
    assert result.error_message == "WEB_AREA_NOT_FOUND"
    assert result.evidence is None
    assert attempt is not None
    assert attempt.error_code == "CAPTURE_ACCESSIBILITY"
    assert attempt.error_message == "WEB_AREA_NOT_FOUND"
    assert not (tmp_path / "evidence").exists()


def test_runner_accepts_mac_visual_review_only_under_the_explicit_policy(
    tmp_path: Path,
) -> None:
    """Catches a runner silently treating the beta validation code as strict."""
    spec = _xiaomi_jd_spec()
    task = _task()
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        results = _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(
                _ObservationAdapter(spec, _observation(BusinessOutcome.PRICE_FOUND))
            ),
            capture=_RecordingCapture("CAPTURE_OK_MAC_VISUAL_REVIEW"),
            capture_acceptance_policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
        ).run((task,))

    assert results[0].state is TaskState.SUCCEEDED
    assert results[0].evidence is not None
    assert results[0].evidence.validation_code == "CAPTURE_OK_MAC_VISUAL_REVIEW"


def test_runner_passes_the_observed_semantic_state_to_context_provider(
    tmp_path: Path,
) -> None:
    spec = _xiaomi_jd_spec()
    observation = _observation(BusinessOutcome.PRICE_FOUND)
    seen: list[VerifiedSemanticState] = []

    def context_provider(
        task: WebsiteTask,
        page: _Page,
        semantic_state: VerifiedSemanticState,
    ) -> CaptureContext:
        assert task == _task()
        assert isinstance(page, _Page)
        seen.append(semantic_state)
        return _context(task, page, semantic_state)

    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(
                _ObservationAdapter(spec, observation)
            ),
            capture=_RecordingCapture(),
            context_provider=context_provider,
        ).run((_task(),))

    assert seen == [observation.semantic_state]


@pytest.mark.parametrize(
    ("outcome", "state", "roles"),
    [
        (BusinessOutcome.NO_MODEL, EvidenceState.NO_MODEL, ("search_keyword", "result_region")),
        (BusinessOutcome.CAPACITY_UNAVAILABLE, EvidenceState.CAPACITY_UNAVAILABLE, ("capacity",)),
        (BusinessOutcome.COLOR_UNAVAILABLE, EvidenceState.COLOR_UNAVAILABLE, ("color",)),
        (BusinessOutcome.SOLD_OUT, EvidenceState.SOLD_OUT, ("stock_status",)),
    ],
)
def test_runner_registry_path_maps_each_legal_no_to_exact_evidence_contract(
    tmp_path: Path,
    outcome: BusinessOutcome,
    state: EvidenceState,
    roles: tuple[str, ...],
) -> None:
    spec = _xiaomi_jd_spec()
    capture = _RecordingCapture()
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(_ObservationAdapter(spec, _observation(outcome))),
            capture=capture,
        ).run((_task(),))

    assert capture.requests[0].state is state
    assert capture.requests[0].expected_roles == roles


def test_runner_registry_path_rejects_execute_only_adapter_before_capture(
    tmp_path: Path,
) -> None:
    spec = next(spec for spec in load_site_catalog() if spec.channel is WebsiteChannel.JD)
    adapter = _ExecuteOnlyAdapter(spec)
    capture = _RecordingCapture()
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        results = _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(adapter),
            capture=capture,
        ).run((_task(),))

    assert results[0].state is TaskState.TECHNICAL_FAILURE
    assert results[0].evidence is None
    assert not adapter.execute_called
    assert capture.requests == []


def test_runner_registry_path_rejects_malformed_observation_before_capture(
    tmp_path: Path,
) -> None:
    malformed = object.__new__(AdapterObservation)
    object.__setattr__(malformed, "outcome", BusinessOutcome.PRICE_FOUND)
    object.__setattr__(malformed, "price", Decimal("NaN"))
    object.__setattr__(malformed, "url", "https://example.test/product")
    object.__setattr__(malformed, "css_rectangles", ())
    adapter = _ObservationAdapter(_xiaomi_jd_spec(), malformed)
    capture = _RecordingCapture()
    with SQLiteTaskRepository(tmp_path / "state.sqlite3") as repository:
        repository.create_run(_run(tmp_path))
        results = _runner(
            repository,
            tmp_path,
            adapter_registry=_registry(adapter),
            capture=capture,
        ).run((_task(),))

    assert results[0].state is TaskState.TECHNICAL_FAILURE
    assert results[0].evidence is None
    assert capture.requests == []

# Incremental Channel Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every confirmed website price before capture, update one partial Excel pair stage-by-stage for small runs and in bounded batches for production-scale runs, and repair the HONOR official/JD/Tmall live flows without allowing later failures to erase earlier work.

**Architecture:** Add a durable `WebsiteObservationCheckpoint` beside the existing final `WebsiteResult`. Split task execution into observation and capture stages. Runs below 100 website tasks publish the stable `-处理中` workbook/report pair after every meaningful event; larger runs keep SQLite/UI stage progress immediate and use one count-bounded background publisher with a final/manual-wait flush. Publish the normal final pair at run completion. Keep the existing channel-major order and exact-match/fail-closed rules.

**Tech Stack:** Python 3.12, dataclasses, SQLite, Playwright, openpyxl, Pillow, tkinter, pytest, PyInstaller.

## Global Constraints

- The Mac acceptance build processes only all HONOR rows from the base workbook.
- Channel order is all HONOR official tasks, then all HONOR JD tasks, then all HONOR Tmall tasks.
- A later channel failure must never clear an earlier channel observation or screenshot.
- A confirmed price or legal “无” is written to the partial workbook even when its screenshot is still missing; the report must say “截图待补”.
- For 100 or more website tasks, the durable checkpoint and UI update are immediate while the processing workbook is refreshed every 25 stage events; manual wait, cancellation, close, and final return force the latest snapshot.
- Only validated formal evidence may be embedded in `AL/AM/AN`.
- JD/Tmall login or security verification pauses the exact current task and resumes it atomically.
- Mac keeps the user-positioned normal Chrome window; the program must not maximize or resize it.
- No new runtime dependency or network service is introduced.

---

## File Structure

- `src/quote_app/tasks/models.py`: define the observation-checkpoint domain value.
- `src/quote_app/tasks/serialization.py`: serialize checkpoints without credentials.
- `src/quote_app/tasks/repository.py`: store/load generation-bound observation checkpoints.
- `src/quote_app/tasks/runner.py`: split observe/capture stages and persist observation first.
- `src/quote_app/tasks/scheduler.py`: emit only repository-backed stage events.
- `src/quote_app/services/web_run.py`: expose event delivery to the full pipeline.
- `src/quote_app/services/incremental_publication.py`: own stable partial paths and snapshot publication.
- `src/quote_app/services/full_pipeline.py`: create the initial partial pair, publish on events, and write the final pair.
- `src/quote_app/services/web_to_excel.py`: merge final results and observation checkpoints.
- `src/quote_app/excel/quote_writer.py`: support an explicit atomically replaced destination.
- `src/quote_app/excel/report_writer.py`: describe checkpoint-only channel states and support an explicit destination.
- `src/quote_app/evidence/macos_runtime.py`: bounded retry for transient foreground activation.
- `src/quote_app/sites/jd.py`: explicit SKU scrolling, capture-view positioning, and bounded state rereads.
- `src/quote_app/sites/tmall.py`: bounded page readiness plus SKU scrolling.

### Task 1: Observation checkpoint domain and SQLite persistence

**Files:**
- Modify: `src/quote_app/tasks/models.py`
- Modify: `src/quote_app/tasks/serialization.py`
- Modify: `src/quote_app/tasks/repository.py`
- Test: `tests/unit/test_task_serialization.py`
- Test: `tests/unit/test_task_repository.py`

**Interfaces:**
- Produces: `WebsiteObservationCheckpoint(task_id, outcome, price, url, observed_at)`.
- Produces: `SQLiteTaskRepository.save_observation(checkpoint, token=token)`.
- Produces: `SQLiteTaskRepository.load_observation(task_id)`.

- [ ] **Step 1: Write failing domain and serialization tests**

```python
def test_observation_checkpoint_round_trips_without_formal_evidence() -> None:
    observed_at = datetime(2026, 7, 30, 0, 15, tzinfo=timezone.utc)
    checkpoint = WebsiteObservationCheckpoint(
        task_id="task-honor-official",
        outcome=BusinessOutcome.PRICE_FOUND,
        price=Decimal("4999"),
        url="https://www.honor.com/cn/shop/product/10086252969809.html",
        observed_at=observed_at,
    )

    assert from_payload(to_payload(checkpoint)) == checkpoint
```

Add literal validation tests proving `PRICE_FOUND` requires a finite non-negative price, legal “无” forbids a price, URLs must be credential-free HTTP(S), and timestamps must be timezone-aware.

- [ ] **Step 2: Run the serialization tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_task_serialization.py -k observation_checkpoint
```

Expected: collection or import failure because `WebsiteObservationCheckpoint` does not exist.

- [ ] **Step 3: Implement the domain value and serializer**

Add this shape to `tasks/models.py` and mirror the existing credential checks:

```python
@dataclass(frozen=True, slots=True)
class WebsiteObservationCheckpoint:
    task_id: str
    outcome: BusinessOutcome
    price: Decimal | None
    url: str
    observed_at: datetime
```

Register `"WebsiteObservationCheckpoint"` in `to_payload()` and `from_payload()` with exact keys:

```python
{
    "task_id",
    "outcome",
    "price",
    "url",
    "observed_at",
}
```

- [ ] **Step 4: Write failing repository tests**

```python
def test_observation_survives_a_later_capture_failure(
    task_repository: SQLiteTaskRepository,
    website_task: WebsiteTask,
) -> None:
    token = task_repository.start_attempt(website_task.task_id)
    checkpoint = make_observation_checkpoint(website_task.task_id)
    task_repository.save_observation(checkpoint, token=token)
    task_repository.save_result(
        make_technical_result(
            website_task.task_id,
            code="CAPTURE_FOREGROUND",
        ),
        token=token,
    )

    assert task_repository.load_observation(website_task.task_id) == checkpoint
```

Also test that changing a task generation makes the previous checkpoint unavailable.

- [ ] **Step 5: Run the repository tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_task_repository.py -k observation
```

Expected: failure because the repository methods and table do not exist.

- [ ] **Step 6: Implement the observation table**

Create the table during idempotent schema initialization:

```sql
CREATE TABLE IF NOT EXISTS website_observations (
    task_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
)
```

`save_observation()` must call `_require_current_attempt()`, store the current generation, and leave the observation untouched when `save_result()` later writes a technical failure. `load_observation()` must join `website_tasks` and return a row only when both generations match.

- [ ] **Step 7: Run Task 1 tests**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_task_serialization.py tests/unit/test_task_repository.py
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/quote_app/tasks/models.py src/quote_app/tasks/serialization.py src/quote_app/tasks/repository.py tests/unit/test_task_serialization.py tests/unit/test_task_repository.py
git commit -m "feat: persist website observation checkpoints"
```

### Task 2: Observe first, retry capture on the current page, and emit durable events

**Files:**
- Modify: `src/quote_app/tasks/runner.py`
- Modify: `src/quote_app/tasks/scheduler.py`
- Modify: `src/quote_app/services/web_run.py`
- Test: `tests/unit/test_runner_registry_observation.py`
- Test: `tests/unit/test_scheduler.py`
- Test: `tests/unit/test_web_run_service.py`

**Interfaces:**
- Consumes: `SQLiteTaskRepository.save_observation()`.
- Produces: `WebsiteRunRequest.event_sink: EventSink | None`.
- Produces: `WebsiteRunRequest.checkpoint_sink: CheckpointSink | None`.
- Produces: `WebsiteRunSnapshot(observations, results, waiting_task_ids)`.
- Emits: `WorkerEvent(event="observation", task_id=..., data={"outcome": ..., "price": ...})` only after the checkpoint transaction commits.

- [ ] **Step 1: Write a failing runner test**

```python
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
```

The test fails if capture retry reruns `adapter.observe()` and navigates again.

- [ ] **Step 2: Run the runner test and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_runner_registry_observation.py -k capture_failure_keeps
```

Expected: failure because the observation is not persisted and capture is attempted once.

- [ ] **Step 3: Split observation from capture**

Change `_site_observation()` to return only `AdapterObservation`. In `_attempt()`:

```python
observation = self._site_observation(task, page)
checkpoint = WebsiteObservationCheckpoint(
    task_id=task.task_id,
    outcome=observation.outcome,
    price=observation.price,
    url=observation.url,
    observed_at=datetime.now(timezone.utc),
)
self.repository.save_observation(checkpoint, token=token)
self.scheduler.publish_observation(task, checkpoint)
evidence = self._capture_current_observation(
    task,
    page,
    observation,
    destination,
)
```

`_capture_current_observation()` performs at most three capture attempts on the same page. It recreates the capture context between attempts and retries only:

```python
{
    "CAPTURE_FOREGROUND",
    "CAPTURE_UNSTABLE",
    "CAPTURE_OBSCURED",
    "CAPTURE_GEOMETRY",
    "CAPTURE_FAILED",
}
```

Use `page.wait_for_timeout(500)` between capture attempts. A final failure returns the existing technical `WebsiteResult`; the checkpoint remains.

- [ ] **Step 4: Write event-order tests**

Assert the event sequence for a successful task is:

```python
["progress", "observation", "result"]
```

Assert the observation event can load the checkpoint from SQLite inside the event sink, proving the event occurs after commit.

- [ ] **Step 5: Run event tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_scheduler.py tests/unit/test_web_run_service.py -k 'observation or event_sink'
```

Expected: failure because `WebsiteRunRequest` does not expose the sink and the runner does not emit the event.

- [ ] **Step 6: Wire the event sink**

Add the optional field:

```python
event_sink: EventSink | None = None
checkpoint_sink: CheckpointSink | None = None
```

to `WebsiteRunRequest` and validate both optional sinks are callable. Define:

```python
@dataclass(frozen=True, slots=True)
class WebsiteRunSnapshot:
    observations: tuple[WebsiteObservationCheckpoint, ...]
    results: tuple[WebsiteResult, ...]
    waiting_task_ids: frozenset[str]


CheckpointSink = Callable[[WebsiteRunSnapshot], None]
```

Wrap the scheduler event sink inside `run_website_tasks()`. After a meaningful event has been durably saved, load the snapshot with the already-open repository instance and invoke `checkpoint_sink`; this avoids attempting to acquire a second task-database lock during the run. Forward the original `WorkerEvent` to `event_sink` for UI progress.

Add this public scheduler boundary so runner code does not call `_emit()` directly:

```python
def publish_observation(
    self,
    task: WebsiteTask,
    checkpoint: WebsiteObservationCheckpoint,
) -> None:
    self._emit(
        "observation",
        task.task_id,
        {
            "outcome": checkpoint.outcome.value,
            "price": (
                str(checkpoint.price)
                if checkpoint.price is not None
                else None
            ),
        },
    )
```

- [ ] **Step 7: Run Task 2 tests**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_runner_registry_observation.py tests/unit/test_scheduler.py tests/unit/test_web_run_service.py
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/quote_app/tasks/runner.py src/quote_app/tasks/scheduler.py src/quote_app/services/web_run.py tests/unit/test_runner_registry_observation.py tests/unit/test_scheduler.py tests/unit/test_web_run_service.py
git commit -m "feat: checkpoint observations before capture"
```

### Task 3: Render checkpoint-only prices and screenshot-pending report states

**Files:**
- Modify: `src/quote_app/services/web_to_excel.py`
- Modify: `src/quote_app/excel/report_writer.py`
- Modify: `tests/factories/web_run_factory.py`
- Test: `tests/integration/test_web_to_excel.py`
- Test: `tests/unit/test_report_writer.py`

**Interfaces:**
- Consumes: `WebsiteObservationCheckpoint`.
- Extends: `WebToExcelRequest.observations`.
- Extends: `ReportWriteRequest.website_observations`.

- [ ] **Step 1: Write failing Excel tests**

```python
def test_checkpoint_price_is_written_when_capture_failed(
    tmp_path: Path,
) -> None:
    row = make_quote_row()
    tasks = make_tasks((row,))
    observation = make_observation_checkpoint(
        tasks[2],
        price=Decimal("4999"),
    )
    failure = make_technical_result(
        tasks[2],
        code="CAPTURE_FOREGROUND",
    )

    fixture = run_web_fixture(
        tmp_path,
        (row,),
        tasks,
        (failure,),
        observations=(observation,),
    )

    workbook = load_workbook(fixture.quote_path)
    assert workbook["5G手机"]["AK2"].value == 4999
    assert workbook["5G手机"]["AN2"].value is None
```

Add a second test with official checkpoint, JD failure, and Tmall failure proving `AK2` remains `4999`. Add a legal-“无” checkpoint test.

- [ ] **Step 2: Run the Excel tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/integration/test_web_to_excel.py -k 'checkpoint or later_channel'
```

Expected: constructor failure because `WebToExcelRequest` has no observations field.

- [ ] **Step 3: Merge complete results and observations**

Add:

```python
observations: tuple[WebsiteObservationCheckpoint, ...] = ()
```

Validate each checkpoint belongs to exactly one task. In `_map_results()`, select a publishable channel value in this order:

```python
complete_result = publishable_result_by_task.get(task.task_id)
observation = observation_by_task.get(task.task_id)
source = (
    complete_result
    if (
        complete_result is not None
        and complete_result.state is TaskState.SUCCEEDED
    )
    else observation
)
```

Only `complete_result` contributes an image. Both sources may contribute the price/“无” and minimum-price fields.

- [ ] **Step 4: Write failing report tests**

Assert exact channel text:

```python
"价格成功（4999）；截图待补（CAPTURE_FOREGROUND）"
```

and row status `部分完成`.

- [ ] **Step 5: Run the report tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_report_writer.py -k screenshot_pending
```

Expected: report shows only a generic technical failure.

- [ ] **Step 6: Extend report context**

Add observations to `_WebsiteReportContext`. If a channel has an observation but no complete result, format:

```python
f"价格成功（{price}）；截图待补（{error_code}）"
```

For legal “无”, use the existing `_LEGAL_NO_LABELS[outcome]` plus `；截图待补`.

- [ ] **Step 7: Run Task 3 tests**

Run:

```bash
.venv/bin/pytest -q tests/integration/test_web_to_excel.py tests/unit/test_report_writer.py
```

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/quote_app/services/web_to_excel.py src/quote_app/excel/report_writer.py tests/factories/web_run_factory.py tests/integration/test_web_to_excel.py tests/unit/test_report_writer.py
git commit -m "feat: publish checkpoint prices with pending screenshots"
```

### Task 4: Stable partial workbook/report pair updated after every stage

**Files:**
- Create: `src/quote_app/services/incremental_publication.py`
- Modify: `src/quote_app/excel/quote_writer.py`
- Modify: `src/quote_app/excel/report_writer.py`
- Modify: `src/quote_app/services/full_pipeline.py`
- Test: `tests/unit/test_quote_writer.py`
- Test: `tests/integration/test_full_pipeline_fixture_sites.py`

**Interfaces:**
- Produces: `IncrementalPublicationPaths(quote_path, report_path)`.
- Produces: `IncrementalExcelPublisher.publish(snapshot) -> WebToExcelResult`.
- Extends: `QuoteWriteRequest.destination_path` and `ReportWriteRequest.destination_path`.

- [ ] **Step 1: Write failing explicit-destination writer tests**

```python
def test_explicit_destination_is_atomically_replaced(
    tmp_path: Path,
    quote_request: QuoteWriteRequest,
) -> None:
    destination = tmp_path / "2026年08月终端供货价报价表-处理中.xlsx"
    destination.write_bytes(b"old-snapshot")

    path = write_quote_workbook(
        replace(quote_request, destination_path=destination)
    )

    assert path == destination
    assert load_workbook(path)["5G手机"]["C2"].value == "910200000040365"
    assert not tuple(tmp_path.glob(".quote-*.tmp.xlsx"))
```

Add the equivalent report-writer test.

- [ ] **Step 2: Run writer tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_quote_writer.py tests/unit/test_report_writer.py -k explicit_destination
```

Expected: request constructors reject `destination_path`.

- [ ] **Step 3: Implement explicit atomic replacement**

After saving the existing temporary file, validate the destination is a direct child of `output_dir`, then:

```python
os.replace(temporary_path, destination_path)
```

Keep the existing collision-safe hard-link publication path when `destination_path is None`.

- [ ] **Step 4: Write a failing incremental-pipeline test**

Use a website runner fake that invokes `request.checkpoint_sink` with three immutable snapshots after saving official observation, official result, and JD failure. After each callback, load the same partial path and record cell values. Assert literal snapshots:

```python
[
    {"AK2": 4999, "AN2_images": 0},
    {"AK2": 4999, "AN2_images": 1},
    {"AK2": 4999, "AI2": None, "AN2_images": 1},
]
```

Assert only one `-处理中.xlsx` quote and one report exist.

- [ ] **Step 5: Run the pipeline test and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/integration/test_full_pipeline_fixture_sites.py -k incremental
```

Expected: no event sink or partial publication exists.

- [ ] **Step 6: Implement `IncrementalExcelPublisher`**

The publisher owns immutable rows/tasks/paths. `publish(snapshot)` consumes the immutable `WebsiteRunSnapshot` delivered by the runner after the repository transaction commits; it must not open or lock the task database while the website runner owns it. Stable names are:

```python
quote = output_dir / f"{year}年{month:02d}月终端供货价报价表-处理中.xlsx"
report = output_dir / f"{year}年{month:02d}月报价执行报告-处理中.xlsx"
```

Guard publication with a lock. Use explicit destinations so the same two files are replaced rather than multiplied.

- [ ] **Step 7: Wire the full pipeline**

After task creation, publish an empty snapshot:

```python
publisher.publish(
    WebsiteRunSnapshot(
        observations=(),
        results=(),
        waiting_task_ids=frozenset(),
    )
)
```

Pass `publisher.publish` as `WebsiteRunRequest.checkpoint_sink`. The runner delivers snapshots after:

```python
{"observation", "result", "waiting_for_login"}
```

and after `technical_failure` only when `retry_remaining == 0` or `retryable is False`. Call `publisher.publish(final_snapshot)` once more before the final collision-safe output.

- [ ] **Step 8: Run Task 4 tests**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_quote_writer.py tests/unit/test_report_writer.py tests/integration/test_full_pipeline_fixture_sites.py
```

Expected: PASS.

- [ ] **Step 9: Commit Task 4**

```bash
git add src/quote_app/services/incremental_publication.py src/quote_app/excel/quote_writer.py src/quote_app/excel/report_writer.py src/quote_app/services/full_pipeline.py tests/unit/test_quote_writer.py tests/unit/test_report_writer.py tests/integration/test_full_pipeline_fixture_sites.py
git commit -m "feat: update one partial Excel pair after each stage"
```

### Task 5: Mac foreground activation retry without resizing

**Files:**
- Modify: `src/quote_app/evidence/macos_runtime.py`
- Test: `tests/unit/test_macos_capture_runtime.py`

**Interfaces:**
- Produces: `_activate_bound_window(identity)` bounded to three attempts.
- Preserves: `ChromiumWindowMode.PRESERVE` for Mac visual-review beta.

- [ ] **Step 1: Write a failing transient-foreground test**

```python
def test_mac_beta_retries_transient_foreground_activation(
    mac_runtime_case: MacRuntimeCase,
) -> None:
    mac_runtime_case.bridge.activation_errors = [
        make_capture_error("CAPTURE_FOREGROUND", "not focused"),
        None,
    ]

    context = mac_runtime_case.runtime.capture_context_provider(
        mac_runtime_case.task,
        mac_runtime_case.page,
        mac_runtime_case.semantic_state,
    )

    assert context.expected_window == mac_runtime_case.identity
    assert mac_runtime_case.bridge.activate_calls == 2
    assert mac_runtime_case.sampler.window_mode is ChromiumWindowMode.PRESERVE
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_macos_capture_runtime.py -k retries_transient_foreground
```

Expected: `CAPTURE_FOREGROUND` escapes after the first activation.

- [ ] **Step 3: Implement bounded activation**

Use the runtime sleeper and three attempts:

```python
def _activate_bound_window(self, identity: BrowserWindowIdentity) -> None:
    for attempt in range(3):
        try:
            self._bridge.activate_window(identity)
            return
        except EvidenceCaptureError as error:
            if error.code != "CAPTURE_FOREGROUND" or attempt == 2:
                raise
            self._sleeper(0.15)
```

Use this helper in both capture-context setup and capture preparation. Do not call any window-resize API.

- [ ] **Step 4: Run Mac capture tests**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_macos_capture_runtime.py tests/unit/test_macos_native_bridge.py tests/integration/test_macos_runner_capture.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/quote_app/evidence/macos_runtime.py tests/unit/test_macos_capture_runtime.py
git commit -m "fix: retry transient mac foreground activation"
```

### Task 6: JD specification scrolling and bounded state convergence

**Files:**
- Modify: `src/quote_app/sites/jd.py`
- Modify: `tests/conftest.py`
- Test: `tests/contract/test_jd_adapter.py`

**Interfaces:**
- Produces: `_prepare_exact_option(locator)` for scroll-before-click.
- Produces: `_position_specification_for_capture(page, capacity)`.
- Produces: bounded verified-state reread before returning `CAPTURE_UNSTABLE`.

- [ ] **Step 1: Write failing JD scroll tests**

```python
def test_jd_modern_scrolls_capacity_and_color_and_positions_capture_view(
    jd_modern_case: JDModernCase,
) -> None:
    jd_modern_case.adapter.observe(
        jd_modern_case.task,
        jd_modern_case.page,
    )

    assert jd_modern_case.page.option_scrolls == [
        "modern-capacity",
        "modern-color",
    ]
    assert jd_modern_case.page.capture_view_positions == [
        "modern-capacity",
    ]
```

Add the same assertion for the legacy detail fixture.

- [ ] **Step 2: Run JD tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_jd_adapter.py -k 'scrolls_capacity or capture_view'
```

Expected: no explicit scroll or final capture positioning is recorded.

- [ ] **Step 3: Implement the JD page preparation**

Before every capacity/color click:

```python
option.scroll_into_view_if_needed()
option.click()
```

After both selections, align the capacity row near the top of the viewport without resizing:

```python
capacity.evaluate(
    "(element) => element.scrollIntoView({block: 'start', inline: 'nearest'})"
)
page.evaluate("() => window.scrollBy(0, -120)")
page.wait_for_timeout(500)
```

Use the same path for modern and legacy detail pages.

- [ ] **Step 4: Write a failing transient-state test**

Configure the fake reader so the first two rereads raise `LayoutRecognitionError` and the third returns the exact expected state. Assert the reader succeeds and performs three reads.

- [ ] **Step 5: Run the transient-state test and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_jd_adapter.py -k transient_verified_state
```

Expected: the first transient error escapes.

- [ ] **Step 6: Implement bounded rereads**

In `verified_state_reader()`, retry up to ten times with `250 ms` waits. Return only when the reread equals the expected semantic state. Re-run authentication checks on every poll. After the tenth mismatch/error, raise `LayoutRecognitionError("JD verified offer did not stabilize before capture")`.

- [ ] **Step 7: Run JD contract and runner tests**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_jd_adapter.py tests/unit/test_runner_registry_observation.py -k 'jd or registry'
```

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

```bash
git add src/quote_app/sites/jd.py tests/conftest.py tests/contract/test_jd_adapter.py
git commit -m "fix: prepare JD specifications for stable capture"
```

### Task 7: Tmall bounded readiness and specification scrolling

**Files:**
- Modify: `src/quote_app/sites/tmall.py`
- Modify: `tests/conftest.py`
- Test: `tests/contract/test_tmall_adapter.py`

**Interfaces:**
- Produces: `_wait_for_approved_store(page)`.
- Produces: `_wait_for_detail_layout(page, task, detail_url)`.
- Preserves: login iframe and risk-control pause behavior on every poll.

- [ ] **Step 1: Write failing delayed-render tests**

```python
def test_tmall_waits_for_delayed_store_identity_and_detail_layout(
    tmall_delayed_case: TmallDelayedCase,
) -> None:
    result = tmall_delayed_case.adapter.observe(
        tmall_delayed_case.task,
        tmall_delayed_case.page,
    )

    assert result.price == Decimal("4999")
    assert tmall_delayed_case.page.wait_timeout_milliseconds == [
        500,
        500,
        500,
    ]
```

Add a delayed login-iframe test proving the second poll raises `LoginRequired` instead of ending as `LAYOUT_CHANGED`.

- [ ] **Step 2: Run Tmall tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py -k delayed
```

Expected: immediate `LAYOUT_CHANGED`.

- [ ] **Step 3: Implement bounded readiness**

For at most ten polls:

```python
self._raise_if_blocked_or_error(page)
try:
    self._require_approved_store(page)
    return
except LayoutRecognitionError:
    if poll == 9:
        raise
    page.wait_for_timeout(500)
```

Apply the same pattern to result-region and detail-title/seller recognition. Every poll checks login, login iframe, risk control, and system-error markers first.

- [ ] **Step 4: Write failing Tmall scroll tests**

Assert capacity and color each call `scroll_into_view_if_needed()` before their clicks and the capacity row is positioned for capture after selection.

- [ ] **Step 5: Run Tmall scroll tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py -k scroll
```

Expected: no scroll calls are recorded.

- [ ] **Step 6: Implement Tmall scrolling and settle wait**

Use the same non-resizing sequence as JD:

```python
option.scroll_into_view_if_needed()
option.click()
```

After both selections, position the capacity row with `scrollIntoView({block: "start"})`, scroll up `120 px`, and wait `500 ms` before the final price/SKU read.

- [ ] **Step 7: Run Tmall tests**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py tests/unit/test_runner_registry_observation.py -k 'tmall or registry'
```

Expected: PASS.

- [ ] **Step 8: Commit Task 7**

```bash
git add src/quote_app/sites/tmall.py tests/conftest.py tests/contract/test_tmall_adapter.py
git commit -m "fix: wait and position Tmall detail pages"
```

### Task 8: End-to-end recovery, UI copy, full verification, and Mac package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Modify: `tests/integration/test_macos_web_to_excel.py`
- Modify: `dist-honor-jd-tmall-closure/验收说明.md`

**Interfaces:**
- Consumes: partial publication paths returned by the full pipeline.
- Produces: a new Mac `.app` with an incremented build label.

- [ ] **Step 1: Write failing recovery and UI tests**

Add an integration test that stops after an official observation checkpoint, restarts the runner with the same run/task generation, completes the screenshot, and proves the same partial workbook changes from price-only to price-plus-image.

Add a UI status assertion containing:

```python
"已保存阶段结果：官网 / 荣耀Magic8 / 价格4999 / 截图待补"
```

- [ ] **Step 2: Run the recovery/UI tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/integration/test_macos_web_to_excel.py tests/unit/test_app.py -k 'recovery or stage_result'
```

Expected: no partial-path/status behavior exists.

- [ ] **Step 3: Implement UI progress copy and bump the build label**

Display observation/result events without blocking the Tk thread. Keep the only brand choice as `仅 HONOR`. Set:

```python
APP_BUILD_LABEL = "荣耀逐条保存·官网京东天猫闭环版（全部荣耀行）2026.07.30.1"
```

and update `验收说明.md` with:

```text
本版逐条保存：价格确认后立即写入“处理中”Excel，截图成功后立即补图。
```

- [ ] **Step 4: Run the focused acceptance suite**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_app.py tests/unit/test_task_serialization.py tests/unit/test_task_repository.py tests/unit/test_runner_registry_observation.py tests/unit/test_web_run_service.py tests/unit/test_quote_writer.py tests/unit/test_report_writer.py tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/integration/test_web_to_excel.py tests/integration/test_macos_web_to_excel.py tests/integration/test_full_pipeline_fixture_sites.py tests/integration/test_macos_runner_capture.py
```

Expected: PASS.

- [ ] **Step 5: Run full verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

Expected: all tests pass, Ruff prints `All checks passed!`, and mypy reports no issues.

- [ ] **Step 6: Build and sign the Mac app**

Run:

```bash
.venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist-honor-jd-tmall-closure --workpath build-honor-jd-tmall-closure packaging/quotation_app.spec
codesign --force --deep --sign - dist-honor-jd-tmall-closure/福建移动铺货报价助手.app
codesign --verify --deep --strict --verbose=2 dist-honor-jd-tmall-closure/福建移动铺货报价助手.app
```

Expected: PyInstaller completes successfully and codesign reports the app is valid on disk.

- [ ] **Step 7: Perform frozen-package smoke checks**

Verify the bundled `quote_app.app` code object contains the new build label, the executable is `arm64`, and an intentionally invalid CLI option exits with argparse status `2`.

- [ ] **Step 8: Commit Task 8**

```bash
git add src/quote_app/app.py tests/unit/test_app.py tests/integration/test_macos_web_to_excel.py dist-honor-jd-tmall-closure/验收说明.md
git commit -m "build: package incremental HONOR closure app"
```

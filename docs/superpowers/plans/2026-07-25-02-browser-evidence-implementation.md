# Browser Runtime and Screenshot Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a completely local, persistent Chrome/Edge runtime that converts associated
`QuoteRow` records into resumable website tasks, distinguishes valid business “无” from technical
failures, captures true operating-system full-screen evidence, reuses login state, and safely
survives application or operating-system restarts.

**Architecture:** Create a versioned run/task/result boundary between the Excel core and browser
automation. Run synchronous Playwright in one dedicated worker process, with one visible managed
browser context and serial foreground capture. Store run metadata, input fingerprints, associated
row snapshots, tasks, attempts, and results in SQLite. A site waiting for login is parked instead
of blocking other sites. Route foreground control, viewport-to-screen coordinate conversion,
full-screen capture, and evidence validation through Mac/Windows platform adapters.

**Tech Stack:** Python 3.12, Playwright 1.x using installed Chrome/Edge, SQLite, mss 10.x,
Pillow 11.x, platform-native browser/window APIs, pytest, and a local fixture HTTP server.

## Global Constraints

- All browser state, checkpoints, screenshots, diagnostics, and logs remain on the local computer.
- Account passwords are never read, requested by the program, serialized, logged, or stored.
- Try normal logged-out access first; request manual login only after a site proves it is required.
- Never bypass CAPTCHA, risk control, or security verification.
- A login/CAPTCHA condition parks only affected site tasks and consumes no technical retry.
- When the user actively enters manual-login mode, foreground automation pauses until confirmation;
  tasks for other sites remain queued and resume afterward.
- A valid no-model/no-capacity/no-color/sold-out result is business success only when it has a
  compliant URL and validated screenshot.
- Network, persistent login, CAPTCHA, layout-recognition, foreground, coordinate, and capture
  failures are technical failures.
- Windows evidence must include tabs, address bar, page, taskbar, date, and time.
- Mac evidence must include tabs, address bar, page, menu-bar date/time, and Dock.
- Use a maximized browser window on the primary display, not browser full-screen/F11 mode.
- DOM rectangles are CSS viewport coordinates; they must be converted to captured-screen physical
  pixels by the platform adapter before annotation.
- Capture one visible page at a time. Browser navigation and operating-system capture are never
  run concurrently in multiple foreground workers.
- Save a checkpoint after every state transition and every attempt, not only after final success.
- Default technical retry count is two after the initial attempt.
- Retry waits must be interruptible by pause/stop and must not use an uninterruptible long sleep.
- Successful results are never reset by resume or failure-only rerun. An explicit re-query creates
  a new run or task generation.
- A run may resume only against the exact stored input fingerprints and schema version.
- The dedicated browser profile is protected by a cross-platform single-process lock.
- Preserve an original high-quality evidence image; Excel-size compression is a later derived copy.
- Use TDD and commit after each task.

---

## Planned File Structure

```text
src/quote_app/
  browser/
    channel_detection.py
    profile_lock.py
    session.py
    worker.py
  evidence/
    models.py
    geometry.py
    annotations.py
    quality.py
    platform.py
    macos.py
    windows.py
  tasks/
    models.py
    serialization.py
    builder.py
    repository.py
    retry.py
    scheduler.py
    runner.py
tests/
  fixtures/browser/
    normal.html
    no_model.html
    capacity_disabled.html
    color_disabled.html
    sold_out.html
    login_required.html
  unit/
    test_browser_contracts.py
    test_task_serialization.py
    test_task_builder.py
    test_channel_detection.py
    test_profile_lock.py
    test_task_repository.py
    test_retry.py
    test_scheduler.py
    test_evidence_geometry.py
    test_annotations.py
    test_evidence_quality.py
  integration/
    test_persistent_browser.py
    test_checkpoint_recovery.py
    test_fixture_capture.py
    test_browser_workload_900.py
docs/testing/
  macos-evidence-smoke.md
  browser-evidence-acceptance.md
```

## Task 1: Versioned Run, Task, Result, and Evidence Contracts

**Files:**
- Modify: `pyproject.toml`
- Create: `src/quote_app/tasks/models.py`
- Create: `src/quote_app/tasks/serialization.py`
- Create: `src/quote_app/evidence/models.py`
- Create: `tests/unit/test_browser_contracts.py`
- Create: `tests/unit/test_task_serialization.py`

**Interfaces:**
- Consumes: `QuoteMonth`, local paths, and source-workbook fingerprints.
- Produces: `RunRecord`, `InputFingerprint`, `WebsiteTask`, `WebsiteResult`,
  `WebsiteChannel`, `TaskState`, `BusinessOutcome`, `EvidenceRecord`, `EvidenceState`,
  and versioned `to_payload`/`from_payload`.

- [ ] **Step 1: Write failing contract, invariant, and round-trip tests**

Cover at minimum:

```python
from decimal import Decimal
from pathlib import Path

import pytest

from quote_app.evidence.models import EvidenceRecord, EvidenceState
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteResult,
)
from quote_app.tasks.serialization import from_payload, to_payload


def test_business_and_technical_states_are_distinct() -> None:
    assert BusinessOutcome.NO_MODEL.value == "no_model"
    assert TaskState.TECHNICAL_FAILURE.value == "technical_failure"


def test_legal_no_requires_url_and_validated_evidence(tmp_path) -> None:
    with pytest.raises(ValueError, match="business success requires validated evidence"):
        WebsiteResult(
            task_id="task-1",
            state=TaskState.SUCCEEDED,
            outcome=BusinessOutcome.NO_MODEL,
            price=None,
            url="https://example.test/search",
            evidence=None,
            diagnostic_path=None,
            error_code=None,
            error_message=None,
        )


def test_versioned_payload_round_trips_paths_decimal_and_enums(sample_result) -> None:
    loaded = from_payload(to_payload(sample_result))
    assert loaded == sample_result
    assert loaded.price == Decimal("4499.50")
    assert isinstance(loaded.evidence.path, Path)
```

Also test:

- `PRICE_FOUND` requires a non-negative price, URL, and validated evidence.
- Legal “无” outcomes require `price is None`, URL, and validated evidence.
- `TECHNICAL_FAILURE` requires an error code/message and cannot carry formal evidence.
- `WAITING_FOR_LOGIN` is a task state, not a completed `WebsiteResult`.
- unknown future `schema_version` is rejected with a stable error;
- payloads never contain a password, cookie value, authorization header, or token.

- [ ] **Step 2: Run tests and verify missing modules**

Run:

```bash
python -m pytest tests/unit/test_browser_contracts.py tests/unit/test_task_serialization.py -v
```

Expected: FAIL during import.

- [ ] **Step 3: Add dependencies and implement typed contracts**

Add runtime dependencies:

```toml
"playwright>=1.50,<2",
"mss>=10,<11",
```

Define:

```python
SCHEMA_VERSION = 1


class WebsiteChannel(StrEnum):
    JD = "jd"
    TMALL = "tmall"
    OFFICIAL = "official"


class BusinessOutcome(StrEnum):
    PRICE_FOUND = "price_found"
    NO_MODEL = "no_model"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    COLOR_UNAVAILABLE = "color_unavailable"
    SOLD_OUT = "sold_out"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_LOGIN = "waiting_for_login"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    TECHNICAL_FAILURE = "technical_failure"


class EvidenceState(StrEnum):
    NORMAL = "normal"
    NO_MODEL = "no_model"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    COLOR_UNAVAILABLE = "color_unavailable"
    SOLD_OUT = "sold_out"
```

`InputFingerprint` contains source role, normalized path, SHA-256, byte size, and modified
nanoseconds. SHA-256 is authoritative; size/mtime are diagnostic.

`RunRecord` contains:

- `run_id`, `schema_version`, and `quote_month`;
- the three immutable input fingerprints;
- local output directory and browser-profile directory;
- serialized associated-row snapshot or snapshot path plus SHA-256;
- optional current quote/report paths;
- run state and created/updated timestamps.

`WebsiteTask` contains:

- stable `task_id` and parent `run_id`;
- `source_row_number` for source diagnostics;
- `output_row_number` for exact AI:AN Excel placement;
- normalized material code, brand, model, RAM, storage, color, and channel.

`EvidenceRecord` contains:

- `state`, original image path, SHA-256, pixel width/height;
- capture timestamp and validation code;
- annotation roles and final physical-pixel rectangles.

`WebsiteResult.price` is `Decimal | None`; JSON stores it as a string. Paths are stored as
normalized strings and enums by their values. Implement explicit allow-listed serializers rather
than generic `asdict`, and reject malformed or future-version payloads.

Enforce the outcome-to-evidence mapping:

```text
PRICE_FOUND           -> NORMAL
NO_MODEL              -> NO_MODEL
CAPACITY_UNAVAILABLE  -> CAPACITY_UNAVAILABLE
COLOR_UNAVAILABLE     -> COLOR_UNAVAILABLE
SOLD_OUT              -> SOLD_OUT
```

For `SOLD_OUT`, the stock-status area must be red-framed. This makes the existing “下架或无货”
legal-“无” rule testable.

- [ ] **Step 4: Run tests and static checks**

Run:

```bash
python -m pytest tests/unit/test_browser_contracts.py tests/unit/test_task_serialization.py -v
python -m ruff check src/quote_app/tasks src/quote_app/evidence tests/unit/test_browser_contracts.py tests/unit/test_task_serialization.py
python -m mypy src/quote_app/tasks src/quote_app/evidence
```

Expected: all PASS.

- [ ] **Step 5: Commit runtime contracts**

```bash
git add pyproject.toml src/quote_app/tasks/models.py src/quote_app/tasks/serialization.py \
  src/quote_app/evidence/models.py tests/unit/test_browser_contracts.py \
  tests/unit/test_task_serialization.py
git commit -m "feat: define durable browser task contracts"
```

## Task 2: Run Identity, Input Fingerprints, and WebsiteTask Builder

**Files:**
- Create: `src/quote_app/tasks/builder.py`
- Create: `tests/unit/test_task_builder.py`

**Interfaces:**
- Consumes: one `QuoteMonth`, exact input paths, browser/output paths, and ordered Plan 1
  `list[QuoteRow]`.
- Produces: `create_run_record(...) -> RunRecord` and
  `build_website_tasks(run, rows) -> TaskBuildResult`.

- [ ] **Step 1: Write failing run and task-construction tests**

Tests must prove:

- an eligible row produces exactly JD, Tmall, and official tasks;
- task query fields come from `QuoteRow.web_query`, not presentation cells;
- output rows are assigned by `enumerate(rows, start=2)`;
- a blank source row before a valid row does not shift the target Excel row;
- source row number remains available for diagnostics;
- duplicate material codes remain independent output-row tasks;
- all task IDs are stable when rebuilt with the same `run_id`;
- a new run gets a new `run_id`, so prices are never cached across months/runs;
- missing brand/model/RAM/storage/color yields no tasks and a `WEB_FIELDS_MISSING` build issue;
- an unsupported nonblank brand yields no tasks and an `UNSUPPORTED_BRAND` build issue;
- each source fingerprint changes when workbook bytes change, even if the filename is unchanged.

Example:

```python
def test_builder_uses_output_row_not_sparse_source_row(eligible_rows, run_record) -> None:
    eligible_rows[0].source_row_number = 7
    result = build_website_tasks(run_record, eligible_rows)
    assert {task.output_row_number for task in result.tasks} == {2}
    assert {task.source_row_number for task in result.tasks} == {7}
```

- [ ] **Step 2: Run tests and verify the builder is missing**

Run: `python -m pytest tests/unit/test_task_builder.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement deterministic task construction**

Use the seven supported normalized brands:

```python
SUPPORTED_BRANDS = frozenset(
    {"HONOR", "华为", "维沃", "欧珀", "小米", "苹果", "ZTE中兴"}
)
```

Generate one random UUID run ID only when creating a new run. Generate each task ID
deterministically from:

```text
run_id + output_row_number + material_code + channel
```

Use a fixed UUID namespace or SHA-256, not Python's randomized `hash()`. Return tasks plus row-level
build issues; never fabricate missing query values.

Store a versioned, SHA-256-protected snapshot of the ordered associated rows so restart does not
depend on re-reading changed source workbooks.

- [ ] **Step 4: Run builder and Plan 1 regression tests**

Run:

```bash
python -m pytest tests/unit/test_task_builder.py tests/unit/test_association.py \
  tests/unit/test_domain_models.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit the run/task bridge**

```bash
git add src/quote_app/tasks/builder.py tests/unit/test_task_builder.py
git commit -m "feat: build stable website tasks from quote rows"
```

## Task 3: Installed-Browser Detection, Persistent Session, and Profile Lock

**Files:**
- Create: `src/quote_app/browser/channel_detection.py`
- Create: `src/quote_app/browser/profile_lock.py`
- Create: `src/quote_app/browser/session.py`
- Create: `tests/unit/test_channel_detection.py`
- Create: `tests/unit/test_profile_lock.py`
- Create: `tests/integration/test_persistent_browser.py`

**Interfaces:**
- Consumes: platform identifier and a local application-data browser-profile directory.
- Produces: `detect_browser_choice() -> BrowserChoice`, `BrowserProfileLock`, and
  `PersistentBrowserSession`.

- [ ] **Step 1: Write failing detection, lock, persistence, and cleanup tests**

Cover:

- Windows prefers an existing Chrome executable, then an existing Edge executable.
- Windows checks both system and per-user standard install paths.
- Mac accepts an existing Google Chrome application executable.
- a candidate dictionary entry whose path does not exist is rejected;
- a second process/session cannot acquire the same profile lock;
- a released lock can be acquired again;
- an abandoned lock is recoverable only after the OS file lock is no longer held;
- context startup failure releases Playwright and the lock;
- local HTTP-origin Cookie and `localStorage` values survive close/reopen.

- [ ] **Step 2: Run tests and verify modules are missing**

Run:

```bash
python -m pytest tests/unit/test_channel_detection.py tests/unit/test_profile_lock.py \
  tests/integration/test_persistent_browser.py -v
```

Expected: FAIL during import.

- [ ] **Step 3: Implement the persistent installed-browser session**

Detection checks real executable existence. Default candidates include:

- macOS: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` and the per-user
  application location;
- Windows Chrome: `%LOCALAPPDATA%`, `%PROGRAMFILES%`, and `%PROGRAMFILES(X86)%`;
- Windows Edge: `%PROGRAMFILES%` and `%PROGRAMFILES(X86)%`.

Use an OS-held lock (`fcntl` on macOS, `msvcrt` on Windows) rather than trusting a stale lock-file
timestamp. Keep the lock handle open for the full browser session.

Launch one persistent context with:

- the dedicated profile, never the user's normal Chrome/Edge profile;
- installed executable path;
- `headless=False`, `viewport=None`;
- maximized non-F11 window on the primary display;
- one reusable page/tab per site family so login pages can be parked.

Playwright creation and use remain in the same worker process/thread. `close()` is idempotent and
releases context, Playwright, and profile lock in `finally` blocks.

- [ ] **Step 4: Run tests and a local persistence smoke**

Run:

```bash
python -m pytest tests/unit/test_channel_detection.py tests/unit/test_profile_lock.py \
  tests/integration/test_persistent_browser.py -v
```

Browser tests may skip only when no supported installed browser exists, with the searched paths in
the skip reason.

- [ ] **Step 5: Commit persistent browser support**

```bash
git add src/quote_app/browser/channel_detection.py src/quote_app/browser/profile_lock.py \
  src/quote_app/browser/session.py tests/unit/test_channel_detection.py \
  tests/unit/test_profile_lock.py tests/integration/test_persistent_browser.py
git commit -m "feat: lock and persist local browser sessions"
```

## Task 4: SQLite Run Repository and Crash-Safe State Transitions

**Files:**
- Create: `src/quote_app/tasks/repository.py`
- Create: `tests/unit/test_task_repository.py`
- Create: `tests/integration/test_checkpoint_recovery.py`

**Interfaces:**
- Consumes: `RunRecord`, `WebsiteTask`, attempt records, and `WebsiteResult`.
- Produces: run creation/loading, task upsert, atomic state transitions, pending/failed/waiting
  selection, file auditing, pause/resume, and crash recovery.

- [ ] **Step 1: Write failing repository and recovery tests**

Cover:

- run/task/result round trip after closing and reopening SQLite;
- exact input fingerprints and associated-row snapshot survive process restart;
- `upsert_task` rejects a different payload under an existing task ID;
- upsert/resume never resets `SUCCEEDED`;
- `save_result` atomically inserts the result and changes task state;
- a forced exception between writes leaves neither half committed;
- `start_attempt` increments and persists attempt count before external work;
- a process crash while `RUNNING` is recovered to `PENDING`;
- `WAITING_FOR_LOGIN` remains parked and discoverable after restart;
- `PAUSED` remains paused until explicit resume;
- failure-only selection excludes success and waiting-login tasks;
- missing or hash-mismatched formal evidence is reported by an audit and safely moves the task to
  technical failure without deleting the prior diagnostic record;
- only a new run or explicit new generation may supersede successful data.

- [ ] **Step 2: Run tests and verify repository is missing**

Run:

```bash
python -m pytest tests/unit/test_task_repository.py \
  tests/integration/test_checkpoint_recovery.py -v
```

Expected: FAIL during import.

- [ ] **Step 3: Implement versioned schema and atomic transitions**

Use at least:

```sql
CREATE TABLE quotation_runs (
    run_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE website_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    waiting_site TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES quotation_runs(run_id)
);

CREATE TABLE website_attempts (
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    error_message TEXT,
    PRIMARY KEY(task_id, attempt_number),
    FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
);

CREATE TABLE website_results (
    task_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
);
```

On every connection enable:

```sql
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
```

Use one transaction for each transition. On controlled startup, recover stale `RUNNING` tasks to
`PENDING` and add an interruption diagnostic; do not alter `SUCCEEDED`, `WAITING_FOR_LOGIN`, or
`PAUSED`. Schema migration is explicit and rejects unknown future versions.

- [ ] **Step 4: Run recovery tests and static checks**

Run:

```bash
python -m pytest tests/unit/test_task_repository.py \
  tests/integration/test_checkpoint_recovery.py -v
python -m ruff check src/quote_app/tasks tests/unit/test_task_repository.py \
  tests/integration/test_checkpoint_recovery.py
python -m mypy src/quote_app/tasks
```

Expected: all PASS.

- [ ] **Step 5: Commit checkpoint storage**

```bash
git add src/quote_app/tasks/repository.py tests/unit/test_task_repository.py \
  tests/integration/test_checkpoint_recovery.py
git commit -m "feat: persist crash-safe browser checkpoints"
```

## Task 5: Classified Retry and Non-Blocking Login Scheduler

**Files:**
- Create: `src/quote_app/tasks/retry.py`
- Create: `src/quote_app/tasks/scheduler.py`
- Create: `src/quote_app/browser/worker.py`
- Create: `tests/unit/test_retry.py`
- Create: `tests/unit/test_scheduler.py`

**Interfaces:**
- Consumes: repository task selections, one-attempt callback, error classifier, pause/stop token,
  and login/CAPTCHA predicates.
- Produces: bounded retry, serial task scheduling, parked site queues, and serializable worker
  events `progress`, `waiting_for_login`, `result`, and `technical_failure`.

- [ ] **Step 1: Write failing retry and scheduling tests**

Tests must prove:

- initial attempt plus two technical retries equals three attempts;
- Playwright timeout, network, layout, coordinate, foreground, and capture errors receive stable
  technical error codes;
- login/CAPTCHA produces zero retry consumption;
- a Tmall login task is parked while JD and official tasks continue;
- all tasks waiting for the same site are discoverable;
- entering active manual-login mode pauses browser navigation/capture globally;
- confirmation requeues only the matching site's parked tasks;
- confirmation while the site is still logged out simply parks again without an infinite busy loop;
- restart preserves waiting tasks and resumes unrelated pending tasks;
- retry delay exits promptly when pause/stop is requested;
- worker events contain only versioned JSON-safe values;
- one scheduler never executes two foreground capture operations concurrently.

- [ ] **Step 2: Run tests and verify missing modules**

Run:

```bash
python -m pytest tests/unit/test_retry.py tests/unit/test_scheduler.py -v
```

Expected: FAIL during import.

- [ ] **Step 3: Implement explicit outcomes instead of blocking gates**

Define classified attempt outcomes or exceptions such as:

- `LoginRequired(site, reason)` and `SecurityVerificationRequired(site, reason)`;
- `RetryableTechnicalError(code, message)`;
- `NonRetryableTechnicalError(code, message)`.

The scheduler must never call an indefinite `Event.wait()` inside the single browser loop.
On login/CAPTCHA:

1. persist `WAITING_FOR_LOGIN`;
2. emit `waiting_for_login`;
3. park the task by site;
4. continue selecting unrelated pending work.

When the user chooses to handle login, enter manual-login mode, foreground the site's retained tab,
and stop all automated page interaction. After confirmation, exit manual mode and requeue that
site's parked tasks. The application never inspects credentials.

Retry persistence boundary:

1. write attempt start and increment count;
2. execute one attempt;
3. persist result or classified error;
4. schedule an interruptible delay when another retry remains.

- [ ] **Step 4: Run retry/scheduler and repository integration tests**

Run:

```bash
python -m pytest tests/unit/test_retry.py tests/unit/test_scheduler.py \
  tests/unit/test_task_repository.py tests/integration/test_checkpoint_recovery.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit retry and login scheduling**

```bash
git add src/quote_app/tasks/retry.py src/quote_app/tasks/scheduler.py \
  src/quote_app/browser/worker.py tests/unit/test_retry.py tests/unit/test_scheduler.py
git commit -m "feat: park login tasks without blocking browser work"
```

## Task 6: CaptureRequest, Screen Geometry, OS Capture, and Evidence Quality

**Files:**
- Create: `src/quote_app/evidence/geometry.py`
- Create: `src/quote_app/evidence/platform.py`
- Create: `src/quote_app/evidence/macos.py`
- Create: `src/quote_app/evidence/windows.py`
- Create: `src/quote_app/evidence/annotations.py`
- Create: `src/quote_app/evidence/quality.py`
- Create: `tests/unit/test_evidence_geometry.py`
- Create: `tests/unit/test_annotations.py`
- Create: `tests/unit/test_evidence_quality.py`

**Interfaces:**
- Consumes: `CaptureRequest`, current browser/CDP window identity and bounds, DOM CSS viewport
  rectangles with semantic roles, page-state probe, and destination.
- Produces: a validated `EvidenceRecord` or a classified capture error.

- [ ] **Step 1: Write failing geometry, annotation, and quality tests**

Define test matrices for:

- macOS Retina 2× scaling;
- Windows 100%, 125%, and 150% scaling;
- non-zero browser chrome inset;
- primary-display origin and a negative secondary-display origin;
- rectangles touching or exceeding image edges;
- two required no-model frames: search keyword and result region;
- one capacity frame, one color frame, and one sold-out stock-status frame;
- normal sale with no frame;
- blank/near-white/near-black capture;
- unexpected foreground window before or after capture;
- unstable page hashes;
- missing screen-recording permission;
- atomic destination publication.

Example:

```python
def test_css_rect_converts_to_retina_capture_pixels() -> None:
    geometry = ViewportGeometry(
        client_origin_x_dip=100,
        client_origin_y_dip=180,
        scale_x=2.0,
        scale_y=2.0,
        capture_origin_x_px=0,
        capture_origin_y_px=0,
    )
    assert css_to_capture_rect(CssRect(10, 20, 50, 30, "capacity"), geometry) == (
        ScreenRect(220, 400, 100, 60, "capacity")
    )
```

- [ ] **Step 2: Run tests and verify evidence modules are missing**

Run:

```bash
python -m pytest tests/unit/test_evidence_geometry.py tests/unit/test_annotations.py \
  tests/unit/test_evidence_quality.py -v
```

Expected: FAIL during import.

- [ ] **Step 3: Implement the complete capture contract**

Use contracts equivalent to:

```python
@dataclass(frozen=True, slots=True)
class CssRect:
    x: float
    y: float
    width: float
    height: float
    role: str


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    destination: Path
    state: EvidenceState
    css_rectangles: tuple[CssRect, ...]
    expected_roles: tuple[str, ...]
    expected_window: BrowserWindowIdentity
    stability_probe: PageStateProbe


class PlatformEvidenceCapture(Protocol):
    def capture(self, request: CaptureRequest) -> EvidenceRecord: ...
```

The platform adapter must:

1. move/maximize the managed browser on the primary display without F11;
2. ensure the taskbar/Dock and system date/time are visible, or return an environment failure;
3. activate the managed browser and verify its foreground identity;
4. obtain the client-area origin in display-independent pixels, capture origin in physical pixels,
   and per-axis scale for the current page;
5. wait for two equal semantic page-state hashes separated by a minimum interval;
6. convert CSS viewport rectangles to capture pixels and validate their semantic roles;
7. capture the entire primary display through `mss` to a temporary high-quality PNG;
8. verify the same browser is still foreground after capture;
9. validate dimensions, luminance/entropy, sharpness, expected browser bounds, and rectangle bounds;
10. draw red frames only for non-normal business states;
11. hash and atomically publish the final original evidence file.

Use browser CDP window bounds to place/maximize the Chrome/Edge window where possible. Platform
code handles foreground identity and client-origin/DPI conversion. Platform-only imports must be
lazy so macOS can import Windows modules and vice versa during tests.

Stable validation codes include:

```text
CAPTURE_OK
CAPTURE_PERMISSION
CAPTURE_ENVIRONMENT
CAPTURE_BLANK
CAPTURE_UNREADABLE
CAPTURE_OBSCURED
CAPTURE_UNSTABLE
CAPTURE_GEOMETRY
```

Do not claim OCR-level readability without OCR. `CAPTURE_UNREADABLE` is based on measurable image
sharpness/contrast plus verified browser geometry; final strict readability remains a manual
platform acceptance item.

- [ ] **Step 4: Run unit tests and Mac manual smoke**

Run:

```bash
python -m pytest tests/unit/test_evidence_geometry.py tests/unit/test_annotations.py \
  tests/unit/test_evidence_quality.py -v
```

On Mac:

- grant screen-recording permission if prompted;
- capture a maximized Chrome page on the primary display;
- confirm tabs, address bar, page, menu-bar date/time, and Dock;
- confirm a DOM target frame aligns at Retina scale;
- switch another app to the foreground during a test capture and confirm `CAPTURE_OBSCURED`;
- record exact screen resolution/scaling, screenshot path, and result in
  `docs/testing/macos-evidence-smoke.md`.

- [ ] **Step 5: Commit evidence capture**

```bash
git add src/quote_app/evidence tests/unit/test_evidence_geometry.py \
  tests/unit/test_annotations.py tests/unit/test_evidence_quality.py \
  docs/testing/macos-evidence-smoke.md
git commit -m "feat: capture coordinate-correct full-screen evidence"
```

## Task 7: Fixture-Site Runner and End-to-End Checkpoint Boundaries

**Files:**
- Create: `tests/fixtures/browser/normal.html`
- Create: `tests/fixtures/browser/no_model.html`
- Create: `tests/fixtures/browser/capacity_disabled.html`
- Create: `tests/fixtures/browser/color_disabled.html`
- Create: `tests/fixtures/browser/sold_out.html`
- Create: `tests/fixtures/browser/login_required.html`
- Create: `src/quote_app/tasks/runner.py`
- Create: `tests/integration/test_fixture_capture.py`

**Interfaces:**
- Consumes: ordered `WebsiteTask` records, fixture adapter callback, browser session,
  `PlatformEvidenceCapture`, retry policy, scheduler, and repository.
- Produces: persisted `WebsiteResult` records and progress/login/failure events.

- [ ] **Step 1: Add deterministic fixture pages and failing integration tests**

Each fixture exposes stable semantic attributes for model, capacity, color, price, search region,
stock state, and login state. Tests cover:

- price found with normal evidence and no red frame;
- no model with search and result frames;
- capacity unavailable with capacity frame;
- color unavailable with color frame;
- sold out with stock-status frame;
- login parked without retry, while unrelated tasks complete;
- technical capture failure leaves the formal evidence path empty and stores a diagnostic;
- result and task state are committed together;
- browser-process interruption after one success resumes without re-running that success;
- task sorting is stable by brand/model/RAM/storage/color/channel while output-row mapping remains
  unchanged.

- [ ] **Step 2: Run integration test and verify the runner is missing**

Run: `python -m pytest tests/integration/test_fixture_capture.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement runner ordering and result boundaries**

The runner:

1. returns an existing validated success without executing it;
2. selects only eligible pending/retry tasks;
3. delegates each attempt through the scheduler/retry classifier;
4. uses one visible browser page and capture at a time;
5. persists all state changes through repository APIs;
6. saves formal evidence only after validation;
7. saves technical screenshots only as diagnostics;
8. never turns a technical failure into business “无”.

Use stable ordering:

```text
(brand, model_name, ram, storage, color, channel, output_row_number)
```

- [ ] **Step 4: Run browser/evidence integration and regression tests**

Run:

```bash
python -m pytest tests/unit tests/integration/test_persistent_browser.py \
  tests/integration/test_checkpoint_recovery.py \
  tests/integration/test_fixture_capture.py -v
```

Expected: all PASS, or installed-browser tests skip only when no supported browser exists.

- [ ] **Step 5: Commit the resumable fixture runner**

```bash
git add src/quote_app/tasks/runner.py tests/fixtures/browser \
  tests/integration/test_fixture_capture.py
git commit -m "feat: run validated resumable browser evidence tasks"
```

## Task 8: 900-Task Performance, Restart, Scaling, and Plan Acceptance

**Files:**
- Create: `tests/integration/test_browser_workload_900.py`
- Create: `docs/testing/browser-evidence-acceptance.md`
- Modify: `docs/testing/macos-evidence-smoke.md`

**Interfaces:**
- Consumes: the complete Plan 2 runtime with fixture attempts and fake/real platform captures.
- Produces: scale/recovery evidence and the Plan 2 acceptance record.

- [ ] **Step 1: Write the failing 900-task workload and restart test**

Create 300 deterministic rows and 900 website tasks with:

- repeated exact query keys;
- duplicate material codes on separate output rows;
- all five business outcomes;
- technical failures;
- at least one parked-login site;
- a simulated process interruption after a fixed number of results.

Assert:

- all 900 tasks insert and reload without loss or ID collision;
- persisted output-row mapping remains exact;
- after restart, `RUNNING` is recovered and success attempt counts do not increase;
- waiting-login tasks stay parked while unrelated pending tasks finish;
- failure-only selection contains exactly technical failures;
- formal evidence hashes survive reopen and audit;
- repository creation, selection, and summary remain practical for 900 tasks;
- DB and screenshot paths remain under the supplied local data directory.

Use a generous deterministic repository-only time bound and record actual duration; do not put live
website latency under a brittle CI limit.

- [ ] **Step 2: Run the workload before final acceptance**

Run:

```bash
python -m pytest tests/integration/test_browser_workload_900.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full Plan 2 verification gate**

```bash
python -m pytest tests/unit tests/integration/test_persistent_browser.py \
  tests/integration/test_checkpoint_recovery.py \
  tests/integration/test_fixture_capture.py \
  tests/integration/test_browser_workload_900.py -v
python -m ruff check src tests
python -m mypy src/quote_app
```

Expected: all PASS. Browser-dependent tests may skip only for a genuinely missing installed
browser; Plan 2 cannot pass manual acceptance on the Mac pilot without Chrome.

- [ ] **Step 4: Complete Mac and cross-platform acceptance records**

Manually verify on the Mac pilot:

- dedicated Chrome profile retains Cookie/local-storage state after application restart;
- a normal logged-out fixture proceeds without a login prompt;
- a login fixture parks without spending a retry;
- unrelated site tasks continue until the user enters active manual-login mode;
- confirmation resumes only the affected site;
- all five business states produce correct result and evidence mapping;
- normal evidence has no red frame;
- all legal-“无” evidence has required, correctly aligned red frames;
- menu bar, date/time, Dock, tabs, address bar, page, configuration, and price/stock state are
  readable;
- app/process restart resumes pending work without repeating success;
- opening a second session against the same profile is refused safely;
- all files remain under local task/application-data directories.

For Windows, run mocked/unit geometry cases at 100%, 125%, and 150% now. Actual Windows foreground,
taskbar, date/time, and strict screenshot acceptance remains mandatory in Plan 4 on a clean
non-admin Windows machine; it must not be inferred from the Mac result.

Record:

- test counts and skip reasons;
- Mac browser/version, display resolution, and scale;
- profile persistence result;
- 900-task DB duration and size;
- interruption point and resume totals;
- screenshot permission/foreground/geometry results;
- known environment limitations.

- [ ] **Step 5: Commit Plan 2 acceptance**

```bash
git add tests/integration/test_browser_workload_900.py \
  docs/testing/macos-evidence-smoke.md \
  docs/testing/browser-evidence-acceptance.md
git commit -m "test: record browser evidence acceptance"
```

## Plan 2 Completion Gate

Plan 2 is complete only when:

- `QuoteRow.web_query` produces stable three-channel tasks with correct Excel output-row mapping;
- run metadata, fingerprints, row snapshots, tasks, attempts, results, and evidence reopen exactly;
- interrupted `RUNNING` work recovers without repeating successful tasks;
- login/CAPTCHA parks affected site tasks without retry or global scheduler blockage;
- active manual login prevents automation from stealing the browser foreground;
- the dedicated profile is persistent and single-instance locked;
- CSS viewport rectangles align with full-screen physical pixels on the Mac Retina pilot and all
  Windows scaling unit matrices;
- valid price and every legal-“无” outcome enforce URL plus validated evidence;
- `SOLD_OUT` uses its explicit stock-status evidence rule;
- technical failures never populate formal evidence;
- the 900-task repository/restart workload passes;
- all automated tests, Ruff, and mypy pass;
- Mac manual evidence acceptance is recorded;
- all persisted data is demonstrably local.

# Browser Runtime and Screenshot Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, persistent Chrome/Edge runtime that can execute resumable website tasks, distinguish valid business “无” from technical failures, capture true operating-system full-screen evidence, and survive application restarts.

**Architecture:** Run synchronous Playwright in a dedicated worker process, persist browser user data separately from program files, and communicate through typed task/result records. Store checkpoints in SQLite after every channel task. Route full-screen capture and foreground control through Mac/Windows platform adapters so site adapters remain platform-neutral.

**Tech Stack:** Python 3.12, Playwright 1.x with installed Chrome/Edge channels, SQLite, mss 10.x, Pillow 11.x, pytest, local fixture HTTP server.

## Global Constraints

- Browser state is local and persistent; account passwords are never read or stored.
- Try logged-out access first and request manual login only when a site requires it.
- Never bypass CAPTCHA, risk control, or security verification.
- A valid no-model/no-capacity/no-color result is business success only when supported by a compliant screenshot.
- Network, login, CAPTCHA, layout-recognition, and capture failures are technical failures.
- Windows evidence must include tabs, address bar, page, taskbar, date, and time.
- Mac evidence must include tabs, address bar, page, menu-bar date/time, and Dock.
- Use a maximized browser window, not browser full-screen mode.
- Save a checkpoint after every website task.
- Default technical retry count is two after the initial attempt.
- Successful results are not destroyed by a failure-only rerun.
- All screenshots, browser state, diagnostics, and checkpoints remain on the local computer.
- Use TDD and commit after each task.

---

## Planned File Structure

```text
src/quote_app/
  browser/
    channel_detection.py
    session.py
    login_gate.py
    worker.py
  evidence/
    models.py
    annotations.py
    quality.py
    platform.py
    macos.py
    windows.py
  tasks/
    models.py
    repository.py
    retry.py
    runner.py
tests/
  fixtures/browser/
    normal.html
    no_model.html
    capacity_disabled.html
    color_disabled.html
  unit/
    test_channel_detection.py
    test_task_repository.py
    test_retry.py
    test_annotations.py
    test_evidence_quality.py
  integration/
    test_persistent_browser.py
    test_fixture_capture.py
```

## Task 1: Browser Task and Evidence Contracts

**Files:**
- Modify: `pyproject.toml`
- Create: `src/quote_app/tasks/models.py`
- Create: `src/quote_app/evidence/models.py`
- Create: `tests/unit/test_browser_contracts.py`

**Interfaces:**
- Consumes: `QuoteMonth` and material codes from Plan 1.
- Produces: `WebsiteChannel`, `TaskState`, `BusinessOutcome`, `WebsiteTask`, `WebsiteResult`, `EvidenceState`, and `ScreenRect`.

- [ ] **Step 1: Write failing enum and serialization tests**

```python
from quote_app.evidence.models import EvidenceState, ScreenRect
from quote_app.tasks.models import BusinessOutcome, TaskState, WebsiteChannel


def test_business_and_technical_states_are_distinct() -> None:
    assert BusinessOutcome.NO_MODEL.value == "no_model"
    assert TaskState.TECHNICAL_FAILURE.value == "technical_failure"
    assert BusinessOutcome.NO_MODEL.value != TaskState.TECHNICAL_FAILURE.value


def test_screen_rect_rejects_negative_dimensions() -> None:
    try:
        ScreenRect(10, 10, -1, 20)
    except ValueError as exc:
        assert str(exc) == "screen rectangle dimensions must be positive"
    else:
        raise AssertionError("negative width was accepted")
```

- [ ] **Step 2: Run the test and verify the modules are missing**

Run: `python -m pytest tests/unit/test_browser_contracts.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Add runtime dependencies and implement typed contracts**

Add to `pyproject.toml`:

```toml
"playwright>=1.50,<2",
"mss>=10,<11",
```

Implement:

```python
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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
    SUCCEEDED = "succeeded"
    TECHNICAL_FAILURE = "technical_failure"


@dataclass(frozen=True, slots=True)
class WebsiteTask:
    task_id: str
    run_id: str
    row_number: int
    material_code: str
    brand: str
    model_name: str
    ram: str
    storage: str
    color: str
    channel: WebsiteChannel


@dataclass(frozen=True, slots=True)
class WebsiteResult:
    task_id: str
    state: TaskState
    outcome: BusinessOutcome | None
    price: int | None
    url: str | None
    evidence_path: Path | None
    diagnostic_path: Path | None
    error_code: str | None
    error_message: str | None
```

```python
from dataclasses import dataclass
from enum import StrEnum


class EvidenceState(StrEnum):
    NORMAL = "normal"
    NO_MODEL = "no_model"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    COLOR_UNAVAILABLE = "color_unavailable"


@dataclass(frozen=True, slots=True)
class ScreenRect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("screen rectangle dimensions must be positive")
```

- [ ] **Step 4: Run tests and static checks**

Run: `python -m pytest tests/unit/test_browser_contracts.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit runtime contracts**

```bash
git add pyproject.toml src/quote_app/tasks src/quote_app/evidence/models.py tests/unit/test_browser_contracts.py
git commit -m "feat: define browser task and evidence contracts"
```

## Task 2: Persistent Chrome/Edge Session

**Files:**
- Create: `src/quote_app/browser/channel_detection.py`
- Create: `src/quote_app/browser/session.py`
- Create: `tests/unit/test_channel_detection.py`
- Create: `tests/integration/test_persistent_browser.py`

**Interfaces:**
- Consumes: a local application-data directory.
- Produces: `detect_browser_channel(platform, existing_paths) -> BrowserChoice` and `PersistentBrowserSession`.

- [ ] **Step 1: Write failing browser-selection tests**

```python
from pathlib import Path
from quote_app.browser.channel_detection import detect_browser_channel


def test_windows_prefers_chrome_then_edge() -> None:
    choice = detect_browser_channel(
        "win32",
        {
            "chrome": Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            "msedge": Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        },
    )
    assert choice.channel == "chrome"


def test_windows_uses_edge_when_chrome_is_absent() -> None:
    choice = detect_browser_channel(
        "win32",
        {"msedge": Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")},
    )
    assert choice.channel == "msedge"
```

- [ ] **Step 2: Run the selection test and verify import failure**

Run: `python -m pytest tests/unit/test_channel_detection.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement detection and a persistent context wrapper**

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BrowserChoice:
    channel: str
    executable: Path


def detect_browser_channel(platform: str, candidates: dict[str, Path]) -> BrowserChoice:
    order = ("chrome", "msedge") if platform == "win32" else ("chrome",)
    for channel in order:
        executable = candidates.get(channel)
        if executable is not None:
            return BrowserChoice(channel, executable)
    raise FileNotFoundError("未找到可用的 Chrome 或 Edge 浏览器")
```

```python
from pathlib import Path
from playwright.sync_api import BrowserContext, Playwright, sync_playwright


class PersistentBrowserSession:
    def __init__(self, profile_dir: Path, choice: BrowserChoice) -> None:
        self.profile_dir = profile_dir
        self.choice = choice
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    def open(self) -> BrowserContext:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            executable_path=str(self.choice.executable),
            headless=False,
            viewport=None,
            args=["--start-maximized"],
        )
        return self.context

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()
```

- [ ] **Step 4: Verify persistence with a local page and storage value**

The integration test opens a local fixture page, sets `localStorage["quote-login-test"]="retained"`, closes the session, reopens with the same profile directory, and asserts the value is retained.

Run: `python -m pytest tests/unit/test_channel_detection.py tests/integration/test_persistent_browser.py -v`

Expected: PASS on a machine with Chrome; skip with an explicit reason when no supported browser is installed.

- [ ] **Step 5: Commit the persistent browser session**

```bash
git add src/quote_app/browser tests/unit/test_channel_detection.py tests/integration/test_persistent_browser.py
git commit -m "feat: persist local browser sessions"
```

## Task 3: SQLite Checkpoint Repository

**Files:**
- Create: `src/quote_app/tasks/repository.py`
- Create: `tests/unit/test_task_repository.py`

**Interfaces:**
- Consumes: `WebsiteTask`, `WebsiteResult`, run identifiers.
- Produces: `TaskRepository.create_run`, `upsert_task`, `start_task`, `save_result`, `pending_tasks`, `failed_tasks`, and `summary`.

- [ ] **Step 1: Write failing persistence and non-destructive-rerun tests**

```python
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteTask,
)
from quote_app.tasks.repository import TaskRepository


def test_successful_task_survives_failure_only_rerun(tmp_path) -> None:
    website_task = WebsiteTask(
        task_id="run-1-row-2-jd",
        run_id="run-1",
        row_number=2,
        material_code="9101",
        brand="小米",
        model_name="小米17",
        ram="12GB",
        storage="256GB",
        color="雪山粉",
        channel=WebsiteChannel.JD,
    )
    successful_result = WebsiteResult(
        task_id=website_task.task_id,
        state=TaskState.SUCCEEDED,
        outcome=BusinessOutcome.PRICE_FOUND,
        price=4499,
        url="https://jd.example/product",
        evidence_path=tmp_path / "jd.jpg",
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.upsert_task(website_task)
    repository.save_result(successful_result)
    assert repository.failed_tasks(website_task.run_id) == []
    loaded = repository.result_for(website_task.task_id)
    assert loaded == successful_result
```

- [ ] **Step 2: Run the test and verify the repository is missing**

Run: `python -m pytest tests/unit/test_task_repository.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement schema creation and transaction-safe writes**

```sql
CREATE TABLE IF NOT EXISTS website_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS website_results (
    task_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES website_tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_run_state
ON website_tasks(run_id, state);
```

Use `sqlite3.connect`, `PRAGMA journal_mode=WAL`, JSON serialization of dataclasses, and one transaction per state transition. `upsert_task` must not reset an existing `SUCCEEDED` row.

- [ ] **Step 4: Run repository tests including process reopen**

Run: `python -m pytest tests/unit/test_task_repository.py -v`

Expected: all tests PASS, including closing and reopening the SQLite file.

- [ ] **Step 5: Commit checkpoint storage**

```bash
git add src/quote_app/tasks/repository.py tests/unit/test_task_repository.py
git commit -m "feat: persist website task checkpoints"
```

## Task 4: Retry, Login Gate, and Worker Events

**Files:**
- Create: `src/quote_app/tasks/retry.py`
- Create: `src/quote_app/browser/login_gate.py`
- Create: `src/quote_app/browser/worker.py`
- Create: `tests/unit/test_retry.py`
- Create: `tests/unit/test_login_gate.py`

**Interfaces:**
- Consumes: `WebsiteTask`, callbacks that perform one attempt, login-detection predicates.
- Produces: `RetryPolicy(max_retries=2)`, `LoginRequired` event, and worker events `progress`, `waiting_for_login`, `result`.

- [ ] **Step 1: Write failing retry and login tests**

```python
import pytest
from quote_app.tasks.retry import RetryPolicy, run_with_retry


def test_initial_attempt_plus_two_retries() -> None:
    calls = 0

    def failing_attempt() -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("network")

    with pytest.raises(TimeoutError, match="network"):
        run_with_retry(failing_attempt, RetryPolicy(max_retries=2, delays=(0, 0)))
    assert calls == 3
```

```python
from quote_app.browser.login_gate import LoginGate


def test_login_gate_waits_and_resumes_after_signal() -> None:
    gate = LoginGate()
    gate.require_login("tmall")
    assert gate.waiting_site == "tmall"
    gate.resume("tmall")
    assert gate.waiting_site is None
```

- [ ] **Step 2: Run the tests and verify missing modules**

Run: `python -m pytest tests/unit/test_retry.py tests/unit/test_login_gate.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement bounded retry and an explicit login event gate**

```python
from dataclasses import dataclass
from time import sleep
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 2
    delays: tuple[float, ...] = (2.0, 5.0)


def run_with_retry(operation: Callable[[], T], policy: RetryPolicy) -> T:
    for attempt in range(policy.max_retries + 1):
        try:
            return operation()
        except (TimeoutError, ConnectionError):
            if attempt == policy.max_retries:
                raise
            sleep(policy.delays[min(attempt, len(policy.delays) - 1)])
    raise RuntimeError("unreachable retry state")
```

The worker must emit a `waiting_for_login` event instead of consuming a retry when a login/CAPTCHA predicate is true. It resumes only after `LoginGate.resume(site)` and must persist `WAITING_FOR_LOGIN` before waiting.

- [ ] **Step 4: Run retry, gate, and worker event-order tests**

Run: `python -m pytest tests/unit/test_retry.py tests/unit/test_login_gate.py -v`

Expected: all tests PASS and event order is `running -> waiting_for_login -> running -> result`.

- [ ] **Step 5: Commit retry and login coordination**

```bash
git add src/quote_app/tasks/retry.py src/quote_app/browser/login_gate.py src/quote_app/browser/worker.py tests/unit/test_retry.py tests/unit/test_login_gate.py
git commit -m "feat: coordinate retries and manual login"
```

## Task 5: OS Screenshot, Red Annotations, and Quality Checks

**Files:**
- Create: `src/quote_app/evidence/platform.py`
- Create: `src/quote_app/evidence/macos.py`
- Create: `src/quote_app/evidence/windows.py`
- Create: `src/quote_app/evidence/annotations.py`
- Create: `src/quote_app/evidence/quality.py`
- Create: `tests/unit/test_annotations.py`
- Create: `tests/unit/test_evidence_quality.py`

**Interfaces:**
- Consumes: browser window title, DOM bounding boxes, `EvidenceState`.
- Produces: `PlatformEvidenceCapture.capture(destination) -> Path`, `annotate_evidence`, and `validate_evidence`.

- [ ] **Step 1: Write failing annotation and quality tests**

```python
from PIL import Image
from quote_app.evidence.annotations import annotate_evidence
from quote_app.evidence.models import EvidenceState, ScreenRect


def test_normal_sale_has_no_red_frame(tmp_path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "normal.png"
    Image.new("RGB", (400, 300), "white").save(source)
    annotate_evidence(source, output, EvidenceState.NORMAL, [])
    assert Image.open(output).getpixel((10, 10)) == (255, 255, 255)


def test_unavailable_option_gets_red_frame(tmp_path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "marked.png"
    Image.new("RGB", (400, 300), "white").save(source)
    annotate_evidence(source, output, EvidenceState.CAPACITY_UNAVAILABLE, [ScreenRect(40, 50, 100, 30)])
    assert Image.open(output).getpixel((40, 50))[0] > 200
```

- [ ] **Step 2: Run tests and verify evidence functions are missing**

Run: `python -m pytest tests/unit/test_annotations.py tests/unit/test_evidence_quality.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement full-screen capture contracts and annotations**

```python
from pathlib import Path
from PIL import Image, ImageDraw


def annotate_evidence(
    source: Path,
    destination: Path,
    state: EvidenceState,
    rectangles: list[ScreenRect],
) -> Path:
    image = Image.open(source).convert("RGB")
    if state is not EvidenceState.NORMAL:
        draw = ImageDraw.Draw(image)
        for rectangle in rectangles:
            draw.rectangle(
                (
                    rectangle.x,
                    rectangle.y,
                    rectangle.x + rectangle.width,
                    rectangle.y + rectangle.height,
                ),
                outline=(255, 0, 0),
                width=6,
            )
    image.save(destination, format="JPEG", quality=82, optimize=True)
    return destination
```

`MacEvidenceCapture` and `WindowsEvidenceCapture` must:

- activate and maximize the managed browser without entering F11 full-screen;
- expose the taskbar/Dock before capture;
- capture the entire primary display through `mss`;
- return a diagnostic error if the platform permission is denied;
- wait for two consecutive matching page-state hashes before capture.

The shared platform interface is:

```python
from pathlib import Path
from typing import Protocol


class PlatformEvidenceCapture(Protocol):
    def capture(
        self,
        destination: Path,
        state: EvidenceState,
        rectangles: list[ScreenRect],
    ) -> Path:
        raise NotImplementedError
```

`validate_evidence` must reject images that are smaller than 1280×720, predominantly black/white, unreadable, or missing the expected browser window. It returns explicit codes `CAPTURE_PERMISSION`, `CAPTURE_BLANK`, `CAPTURE_OBSCURED`, or `CAPTURE_OK`.

- [ ] **Step 4: Run image tests and manual platform smoke captures**

Run: `python -m pytest tests/unit/test_annotations.py tests/unit/test_evidence_quality.py -v`

Expected: all tests PASS.

On Mac, capture one maximized Chrome screen and verify tabs, address bar, menu bar time, and Dock. Record the screenshot path and result in `docs/testing/macos-evidence-smoke.md`.

- [ ] **Step 5: Commit evidence capture**

```bash
git add src/quote_app/evidence tests/unit/test_annotations.py tests/unit/test_evidence_quality.py docs/testing/macos-evidence-smoke.md
git commit -m "feat: capture and validate full-screen evidence"
```

## Task 6: Fixture-Site Integration Runner

**Files:**
- Create: `tests/fixtures/browser/normal.html`
- Create: `tests/fixtures/browser/no_model.html`
- Create: `tests/fixtures/browser/capacity_disabled.html`
- Create: `tests/fixtures/browser/color_disabled.html`
- Create: `src/quote_app/tasks/runner.py`
- Create: `tests/integration/test_fixture_capture.py`

**Interfaces:**
- Consumes: a `WebsiteTask`, one site adapter callback, browser session, evidence capture, retry policy, repository.
- Produces: a persisted `WebsiteResult` and progress events.

- [ ] **Step 1: Add four deterministic fixture pages and failing integration tests**

Each fixture includes semantic attributes:

```html
<!doctype html>
<html lang="zh-CN">
<body>
  <input aria-label="店内搜索" value="小米17">
  <main data-quote-role="results">
    <h1 data-quote-role="model">小米17</h1>
    <button data-quote-role="capacity" aria-pressed="true">12GB+256GB</button>
    <button data-quote-role="color" aria-pressed="true">雪山粉</button>
    <strong data-quote-role="price">¥4499</strong>
  </main>
</body>
</html>
```

The three other fixtures respectively omit the exact model, disable the target capacity, and disable the target color. The integration test runs each task and asserts the correct `BusinessOutcome`, price, evidence state, SQLite checkpoint, and red-frame count.

- [ ] **Step 2: Run integration tests and verify the runner is missing**

Run: `python -m pytest tests/integration/test_fixture_capture.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement runner ordering and checkpoint boundaries**

```python
class WebsiteTaskRunner:
    def __init__(self, repository, browser, capture, retry_policy) -> None:
        self.repository = repository
        self.browser = browser
        self.capture = capture
        self.retry_policy = retry_policy

    def run(self, task: WebsiteTask, adapter) -> WebsiteResult:
        existing = self.repository.result_for(task.task_id)
        if existing is not None and existing.state is TaskState.SUCCEEDED:
            return existing
        self.repository.start_task(task.task_id)
        result = run_with_retry(
            lambda: adapter.execute(task, self.browser, self.capture),
            self.retry_policy,
        )
        self.repository.save_result(result)
        return result
```

The runner must sort tasks by `(brand, model_name, ram, storage, color, channel)` and expose failure-only selection through the repository.

- [ ] **Step 4: Run all browser/evidence tests**

Run: `python -m pytest tests/unit tests/integration/test_persistent_browser.py tests/integration/test_fixture_capture.py -v`

Expected: all tests PASS or browser-dependent tests skip only when a browser is genuinely unavailable.

- [ ] **Step 5: Commit the resumable browser runner**

```bash
git add src/quote_app/tasks/runner.py tests/fixtures/browser tests/integration/test_fixture_capture.py
git commit -m "feat: run resumable browser evidence tasks"
```

## Plan 2 Completion Gate

Run:

```bash
python -m pytest tests/unit tests/integration/test_persistent_browser.py tests/integration/test_fixture_capture.py -v
python -m ruff check src tests
python -m mypy src/quote_app
```

Then manually verify on the Mac test machine:

- the dedicated Chrome profile retains a test login/local-storage marker after restart;
- a login-required fixture pauses without spending a retry and resumes after confirmation;
- all four business states produce the expected price/`无` classification;
- normal evidence has no red frame;
- no-model, capacity-unavailable, and color-unavailable evidence has the required red frame;
- application restart resumes pending tasks without repeating succeeded tasks;
- all files remain under the local task/application-data directories.

Record results and commit:

```bash
git add docs/testing/browser-evidence-acceptance.md
git commit -m "test: record browser evidence acceptance"
```

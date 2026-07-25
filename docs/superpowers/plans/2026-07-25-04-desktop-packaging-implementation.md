# Desktop Integration and Cross-Platform Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a non-technical local desktop application that orchestrates the full quotation workflow, exposes progress/login/resume/result actions, packages as a Mac `.app` for pilot testing, and packages as a no-admin Windows portable application with strict screenshot acceptance.

**Architecture:** Keep the Tk/ttk UI passive and drive it through a testable controller. Run the core and browser pipelines in a worker process so the UI remains responsive. Store preferences, task checkpoints, browser profiles, diagnostics, and logs in per-user application-data folders, separate from replaceable program files.

**Tech Stack:** Python 3.12, tkinter/ttk, multiprocessing, queue, platformdirs 4.x, Playwright, openpyxl, SQLite, PyInstaller 6.x, pytest, Windows pywin32, macOS native commands/APIs.

## Global Constraints

- The interface is for non-technical product managers and must not expose a console.
- Quote month defaults to the current natural month and remains editable.
- The user selects exactly three source workbooks and one output folder.
- Only file-level fatal errors stop the run; row-level issues continue and are reported.
- The run can pause, resume, survive application/OS restart, and retry only failures.
- A site waiting for login/CAPTCHA is clearly shown; passwords are never collected.
- The UI summary and Excel report use the same counts and statuses.
- Existing output files are not overwritten.
- Mac pilot uses Chrome and equivalent Mac full-screen evidence.
- Windows package requires no administrator permission, uses Chrome or Edge, and follows the strict screenshot examples.
- Program replacement must preserve tasks and browser login state.
- No cloud processing, upload, telemetry, or automatic network updater is permitted.
- Use TDD and commit after each task.

---

## Planned File Structure

```text
src/quote_app/
  app.py
  paths.py
  settings.py
  logging_config.py
  orchestration/
    events.py
    controller.py
    worker_process.py
    full_pipeline.py
  ui/
    main_window.py
    file_section.py
    progress_section.py
    current_task_section.py
    result_section.py
    dialogs.py
packaging/
  quotation_app.spec
  macos/
    build.sh
    entitlements.plist
  windows/
    build.ps1
    launch.cmd
tests/
  conftest.py
  factories/
    fake_worker.py
    full_run.py
  unit/
    test_paths.py
    test_settings.py
    test_controller.py
    test_summary_consistency.py
  integration/
    test_full_pipeline_fixture_sites.py
    test_restart_resume.py
    test_output_versioning.py
  smoke/
    test_app_import.py
docs/
  user-guide.md
  testing/
    mac-package-acceptance.md
    windows-package-acceptance.md
    stress-300-row.md
```

## Task 1: Per-User Storage, Settings, and Privacy-Safe Logging

**Files:**
- Modify: `pyproject.toml`
- Create: `src/quote_app/paths.py`
- Create: `src/quote_app/settings.py`
- Create: `src/quote_app/logging_config.py`
- Create: `tests/unit/test_paths.py`
- Create: `tests/unit/test_settings.py`

**Interfaces:**
- Consumes: operating-system identifier and optional environment override used only by tests.
- Produces: `AppPaths`, `UserSettings`, `load_settings`, `save_settings`, and `configure_logging`.

- [ ] **Step 1: Write failing platform-path and settings tests**

```python
from quote_app.paths import build_app_paths


def test_windows_data_is_separate_from_program_files(tmp_path) -> None:
    paths = build_app_paths("Windows", home=tmp_path)
    assert "AppData" in str(paths.data_dir)
    assert paths.browser_profile.parent == paths.data_dir
    assert paths.task_db.parent == paths.data_dir


def test_settings_store_paths_but_no_password_or_cookie(tmp_path) -> None:
    settings = UserSettings(last_output_dir=str(tmp_path), last_quote_year=2026, last_quote_month=8)
    save_settings(tmp_path / "settings.json", settings)
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "password" not in text.lower()
    assert "cookie" not in text.lower()
```

- [ ] **Step 2: Run tests and verify path/settings imports fail**

Run: `python -m pytest tests/unit/test_paths.py tests/unit/test_settings.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement deterministic local paths and minimal settings**

Add runtime dependencies:

```toml
"platformdirs>=4.3,<5",
"pywin32>=310,<311; sys_platform == 'win32'",
```

Add build dependency under `[project.optional-dependencies].dev`:

```toml
"pyinstaller>=6.14,<7",
```

```python
from dataclasses import dataclass
from pathlib import Path
from platformdirs import user_data_dir

APP_NAME = "福建移动铺货报价助手"
APP_AUTHOR = "福建移动终端公司"


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    browser_profile: Path
    task_db: Path
    logs_dir: Path
    diagnostics_dir: Path


def build_app_paths(system: str | None = None, home: Path | None = None) -> AppPaths:
    if home is None:
        root = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    elif system == "Windows":
        root = home / "AppData" / "Local" / APP_NAME
    else:
        root = home / "Library" / "Application Support" / APP_NAME
    return AppPaths(
        data_dir=root,
        browser_profile=root / "browser-profile",
        task_db=root / "tasks.sqlite3",
        logs_dir=root / "logs",
        diagnostics_dir=root / "diagnostics",
    )
```

Logging must rotate at 5 MB with five backups and redact values whose keys contain `password`, `cookie`, `authorization`, or `token`.

- [ ] **Step 4: Run tests and inspect a redacted log fixture**

Run: `python -m pytest tests/unit/test_paths.py tests/unit/test_settings.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit local storage boundaries**

```bash
git add pyproject.toml src/quote_app/paths.py src/quote_app/settings.py src/quote_app/logging_config.py tests/unit/test_paths.py tests/unit/test_settings.py
git commit -m "feat: isolate local app data and safe logs"
```

## Task 2: UI Event Model and Testable Controller

**Files:**
- Create: `src/quote_app/orchestration/events.py`
- Create: `src/quote_app/orchestration/controller.py`
- Create: `tests/factories/fake_worker.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/test_controller.py`
- Create: `tests/unit/test_summary_consistency.py`

**Interfaces:**
- Consumes: UI commands and worker events.
- Produces: `AppViewState`, `AppController.start`, `pause`, `resume`, `stop`, `resume_login`, `retry_failures`, and `poll_events`.

- [ ] **Step 1: Write failing controller-state tests**

```python
from quote_app.orchestration.controller import AppController, ControllerState


def test_start_requires_three_files_and_output_folder(fake_worker) -> None:
    controller = AppController(fake_worker)
    result = controller.start()
    assert result.accepted is False
    assert result.message == "请选择基础表、营销商品信息查询表、BOP资源信息表和输出文件夹"


def test_waiting_login_event_updates_current_site(fake_worker, valid_selection) -> None:
    controller = AppController(fake_worker)
    controller.set_selection(valid_selection)
    controller.start()
    fake_worker.emit({"type": "waiting_for_login", "site": "天猫"})
    controller.poll_events()
    assert controller.view_state.state is ControllerState.WAITING_FOR_LOGIN
    assert controller.view_state.current_site == "天猫"
```

The shared worker double is defined before the controller implementation:

```python
from queue import SimpleQueue


class FakeWorker:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []
        self.events: SimpleQueue[dict[str, object]] = SimpleQueue()

    def send(self, command: dict[str, object]) -> None:
        self.commands.append(command)

    def emit(self, event: dict[str, object]) -> None:
        self.events.put(event)
```

`tests/conftest.py` returns `FakeWorker()` from `fake_worker` and a `valid_selection` containing three temporary `.xlsx` paths, August 2026, and a temporary output directory.

- [ ] **Step 2: Run tests and verify controller imports fail**

Run: `python -m pytest tests/unit/test_controller.py tests/unit/test_summary_consistency.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement explicit controller/view states**

```python
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ControllerState(StrEnum):
    IDLE = "idle"
    PRECHECK = "precheck"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_LOGIN = "waiting_for_login"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FATAL_ERROR = "fatal_error"


@dataclass(slots=True)
class AppViewState:
    state: ControllerState = ControllerState.IDLE
    total: int = 0
    completed: int = 0
    partial: int = 0
    failed: int = 0
    remaining: int = 0
    current_brand: str = ""
    current_model: str = ""
    current_site: str = ""
    message: str = ""
    quote_path: Path | None = None
    report_path: Path | None = None
```

All UI counts must come from the same `RunSummary` object used by the Excel report. The controller cannot calculate separate totals.

- [ ] **Step 4: Run state-transition and summary-consistency tests**

Run: `python -m pytest tests/unit/test_controller.py tests/unit/test_summary_consistency.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit the orchestration controller**

```bash
git add src/quote_app/orchestration/events.py src/quote_app/orchestration/controller.py tests/factories/fake_worker.py tests/conftest.py tests/unit/test_controller.py tests/unit/test_summary_consistency.py
git commit -m "feat: add desktop workflow controller"
```

## Task 3: Non-Technical Desktop Window

**Files:**
- Create: `src/quote_app/ui/main_window.py`
- Create: `src/quote_app/ui/file_section.py`
- Create: `src/quote_app/ui/progress_section.py`
- Create: `src/quote_app/ui/current_task_section.py`
- Create: `src/quote_app/ui/result_section.py`
- Create: `src/quote_app/ui/dialogs.py`
- Create: `tests/smoke/test_app_import.py`

**Interfaces:**
- Consumes: `AppController` and `AppViewState`.
- Produces: `MainWindow.run()` and callbacks for file selection, start/pause/resume/stop, login confirmation, open outputs, and retry failures.

- [ ] **Step 1: Write a headless import/construction smoke test**

```python
from quote_app.ui.main_window import MainWindow


def test_main_window_class_is_importable_without_starting_event_loop() -> None:
    assert MainWindow.__name__ == "MainWindow"
```

- [ ] **Step 2: Run the smoke test and verify UI import failure**

Run: `python -m pytest tests/smoke/test_app_import.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Build the four-area window and bind controller actions**

The window must display:

1. quotation month, three source-file pickers, and output-folder picker;
2. precheck and total/completed/partial/failed/remaining status;
3. current brand, model, site, and stage;
4. start, pause, continue, stop, open quote, open report, and retry-failures actions.

Use standard `ttk` controls and Chinese labels. Quote month initializes from `QuoteMonth.current()`. File dialogs filter to `.xlsx` and `.xlsm`. Result buttons remain disabled until paths exist.

```python
class MainWindow:
    POLL_INTERVAL_MS = 200

    def __init__(self, root, controller: AppController) -> None:
        self.root = root
        self.controller = controller
        self.root.title("福建移动铺货报价助手")
        self.root.minsize(920, 640)
        self._build_sections()
        self.root.after(self.POLL_INTERVAL_MS, self._poll)

    def run(self) -> None:
        self.root.mainloop()
```

- [ ] **Step 4: Run import test and a manual UI checklist**

Run: `python -m pytest tests/smoke/test_app_import.py -v`

Expected: PASS.

Manual checklist: all Chinese labels fit at 100%, 125%, and 150% Windows scaling; tab order is logical; fatal errors are red; login prompt is prominent; no console window appears.

- [ ] **Step 5: Commit the desktop window**

```bash
git add src/quote_app/ui tests/smoke/test_app_import.py
git commit -m "feat: add non-technical desktop interface"
```

## Task 4: Worker Process and Full Pipeline Integration

**Files:**
- Create: `src/quote_app/orchestration/worker_process.py`
- Create: `src/quote_app/orchestration/full_pipeline.py`
- Create: `src/quote_app/app.py`
- Create: `tests/factories/full_run.py`
- Create: `tests/integration/test_full_pipeline_fixture_sites.py`
- Create: `tests/integration/test_restart_resume.py`
- Create: `tests/integration/test_output_versioning.py`

**Interfaces:**
- Consumes: selected files/month/output directory, core pipeline, web pipeline, task repository, browser/evidence runtime.
- Produces: a responsive end-to-end application and event stream.

- [ ] **Step 1: Write failing fixture-site, resume, and output-version tests**

```python
from tests.factories.full_run import FullFixtureRun, RestartableFixtureRun


def test_full_pipeline_generates_two_workbooks_with_fixture_sites(tmp_path) -> None:
    full_fixture_run = FullFixtureRun.create(tmp_path, row_count=7)
    result = full_fixture_run.complete()
    assert result.quote_path.name == "2026年8月终端供货价报价表.xlsx"
    assert result.report_path.name == "2026年8月报价执行报告.xlsx"
    assert result.summary.total_rows == 7


def test_restart_resumes_pending_without_repeating_success(tmp_path) -> None:
    restartable_run = RestartableFixtureRun.create(tmp_path, row_count=7)
    restartable_run.complete_tasks(5)
    first_success_ids = restartable_run.success_ids()
    resumed = restartable_run.restart()
    resumed.complete()
    assert first_success_ids <= resumed.success_ids()
    assert resumed.attempt_counts_for(first_success_ids) == {task_id: 1 for task_id in first_success_ids}
```

`FullFixtureRun` uses the synthetic source-workbook factory and local four-state site fixtures from Plans 1–3. `RestartableFixtureRun` uses the same SQLite path across two worker instances and records attempt counts per task.

- [ ] **Step 2: Run integration tests and verify full orchestrator is missing**

Run: `python -m pytest tests/integration/test_full_pipeline_fixture_sites.py tests/integration/test_restart_resume.py tests/integration/test_output_versioning.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement the worker command/event protocol**

```python
from dataclasses import dataclass
from enum import StrEnum


class WorkerCommandType(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    LOGIN_COMPLETED = "login_completed"
    RETRY_FAILURES = "retry_failures"


@dataclass(frozen=True, slots=True)
class WorkerCommand:
    kind: WorkerCommandType
    payload: dict[str, object]
```

The worker process must:

- run precheck before launching a browser;
- stop and emit all fatal issues together;
- associate and persist Base-driven rows;
- generate and sort website tasks;
- process or resume pending tasks;
- emit progress after every saved task;
- write partial outputs on user stop or unrecoverable row/channel errors;
- write final quote/report when the task reaches a terminal state;
- close Playwright cleanly after final output.

`src/quote_app/app.py` configures paths/logging, starts worker/controller/UI, and calls `multiprocessing.freeze_support()` for Windows packaging.

- [ ] **Step 4: Run full fixture-site and restart suites**

Run: `python -m pytest tests/integration/test_full_pipeline_fixture_sites.py tests/integration/test_restart_resume.py tests/integration/test_output_versioning.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit end-to-end orchestration**

```bash
git add src/quote_app/orchestration src/quote_app/app.py tests/factories/full_run.py tests/integration/test_full_pipeline_fixture_sites.py tests/integration/test_restart_resume.py tests/integration/test_output_versioning.py
git commit -m "feat: integrate complete desktop quotation workflow"
```

## Task 5: Mac `.app` Pilot Package

**Files:**
- Create: `packaging/quotation_app.spec`
- Create: `packaging/macos/build.sh`
- Create: `packaging/macos/entitlements.plist`
- Create: `docs/testing/mac-package-acceptance.md`
- Create: `tests/smoke/test_package_spec.py`

**Interfaces:**
- Consumes: complete application, resources, current Mac Python environment.
- Produces: `dist/福建移动铺货报价助手.app`.

- [ ] **Step 1: Add a package-content smoke assertion**

```python
from pathlib import Path


def test_required_resources_are_declared_in_pyinstaller_spec() -> None:
    text = Path("packaging/quotation_app.spec").read_text(encoding="utf-8")
    assert "resources/templates/quote_template.xlsx" in text
    assert "resources/sites/catalog.json" in text
    assert "collect_data_files('playwright')" in text
```

- [ ] **Step 2: Run the smoke assertion and verify the spec is missing**

Run: `python -m pytest tests/smoke/test_package_spec.py -v`

Expected: FAIL with file-not-found.

- [ ] **Step 3: Create the PyInstaller spec and Mac build script**

The spec must include the template, catalog, Playwright Python assets, Tcl/Tk assets, application icon, and no console window.

`packaging/macos/build.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
python -m pytest -q
python -m PyInstaller --noconfirm --clean packaging/quotation_app.spec
codesign --force --deep --sign - "dist/福建移动铺货报价助手.app"
```

The entitlements file requests no network server, camera, microphone, contacts, or location access. Screen recording permission is requested by the operating system when capture is first used.

- [ ] **Step 4: Build and run the Mac pilot checklist**

Run: `bash packaging/macos/build.sh`

Expected: `.app` exists and launches by double-click without a terminal.

Verify:

- default month is current natural month;
- source/output pickers work;
- precheck errors display in Chinese;
- Chrome profile persists;
- screen-recording permission guidance is understandable;
- provided sample produces both outputs;
- Mac evidence includes browser tabs/address bar, menu-bar time, and Dock;
- close/reopen resume works.

- [ ] **Step 5: Record and commit Mac package acceptance**

```bash
git add packaging/quotation_app.spec packaging/macos docs/testing/mac-package-acceptance.md tests/smoke/test_package_spec.py
git commit -m "build: package Mac quotation pilot"
```

## Task 6: Windows Portable Package and Strict Screenshot Checks

**Files:**
- Create: `packaging/windows/build.ps1`
- Create: `packaging/windows/launch.cmd`
- Create: `docs/testing/windows-package-acceptance.md`
- Create: `tests/unit/test_windows_capture_environment.py`
- Modify: `src/quote_app/evidence/windows.py`

**Interfaces:**
- Consumes: complete application on a clean Windows build machine.
- Produces: `dist/福建移动铺货报价助手/` portable directory and a ZIP archive.

- [ ] **Step 1: Add Windows environment-check tests**

```python
from quote_app.evidence.windows import WindowsCaptureEnvironment


def test_capture_environment_requires_visible_taskbar_and_supported_browser() -> None:
    environment = WindowsCaptureEnvironment(
        browser_found=True,
        taskbar_visible=True,
        primary_width=1920,
        primary_height=1080,
    )
    assert environment.is_ready is True
```

- [ ] **Step 2: Run the Windows-specific unit test before implementation**

Run on Windows: `py -m pytest tests/unit/test_windows_capture_environment.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement Windows environment check and portable build script**

`WindowsCaptureEnvironment.is_ready` requires:

- Chrome or Edge executable found;
- primary screen at least 1280×720;
- taskbar visible during capture;
- browser zoom forced to 100%;
- maximized, non-F11 browser window.

`packaging/windows/build.ps1`:

```powershell
$ErrorActionPreference = "Stop"
py -m pytest -q
py -m PyInstaller --noconfirm --clean packaging/quotation_app.spec
Compress-Archive `
  -Path "dist\福建移动铺货报价助手\*" `
  -DestinationPath "dist\福建移动铺货报价助手-Windows-x64.zip" `
  -Force
```

`launch.cmd` starts the windowed executable from its own directory and requires no administrator elevation.

- [ ] **Step 4: Build on Windows and perform strict evidence acceptance**

Run: `powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1`

Expected: portable folder and ZIP exist.

On a clean non-admin Windows account, verify:

- app launches after unzip;
- Chrome is preferred and Edge fallback works;
- no Python installation is required;
- first login persists after app restart;
- each normal screenshot shows tab bar, address bar/URL, page, selected capacity/color, price, taskbar, date, and time;
- each valid-“无” screenshot has the approved red frame;
- no screenshot uses a browser-only capture;
- Excel and WPS show all AL:AN images.

- [ ] **Step 5: Record and commit Windows acceptance**

```bash
git add packaging/windows src/quote_app/evidence/windows.py tests/unit/test_windows_capture_environment.py docs/testing/windows-package-acceptance.md
git commit -m "build: package Windows portable quotation app"
```

## Task 7: 300-Row Stress Test, User Guide, and Release Candidate

**Files:**
- Create: `scripts/generate_stress_workbooks.py`
- Create: `docs/testing/stress-300-row.md`
- Create: `docs/user-guide.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: complete Mac and Windows builds.
- Produces: stress-test evidence, user-facing instructions, and V1 release candidate.

- [ ] **Step 1: Build a deterministic 300-row synthetic workload**

```python
SUPPORTED_BRANDS = ("小米", "HONOR", "华为", "维沃", "欧珀", "苹果", "ZTE中兴")


def material_code(index: int) -> str:
    return f"9102{index:011d}"


def brand_for(index: int) -> str:
    return SUPPORTED_BRANDS[index % len(SUPPORTED_BRANDS)]
```

The generator creates 300 Base rows in fixed order, matching Marketing/BOP records, March/July history for an August quote, intentional BOP misses, five Marketing misses, three unsupported-brand rows, repeated model/configuration keys for cache testing, and all four fixture-site business outcomes.

- [ ] **Step 2: Run the stress generator and verify row counts**

Run: `python scripts/generate_stress_workbooks.py --output /tmp/quotation-stress`

Expected: three workbooks with exactly 300 Base codes and a `manifest.json` containing expected status totals.

- [ ] **Step 3: Execute full stress, interruption, and failure-only rerun**

Run the packaged app against fixture sites:

1. start the 300-row task;
2. close after at least 100 website tasks;
3. reopen and resume;
4. allow completion;
5. inject ten technical failures;
6. run failure-only retry;
7. compare quote/report totals with `manifest.json`;
8. verify successful attempt counts did not increase during failure-only rerun.

Record wall-clock time, peak memory, task DB size, screenshot cache size, quote workbook size, and Excel/WPS open time in `docs/testing/stress-300-row.md`.

- [ ] **Step 4: Write the concise Chinese user guide**

`docs/user-guide.md` must cover:

- choosing the current quotation month;
- selecting the three source files and output folder;
- interpreting fatal precheck versus row warnings;
- first login and login-state reuse;
- unattended-run computer usage;
- pause, close, resume, and retry failures;
- opening the quotation and execution report;
- meaning of completed, partial, failed, unsupported, and legal “无”;
- Mac screen-recording permission;
- Windows portable unzip/run/update process;
- local data and browser profile locations;
- how to report a website-layout failure without sharing passwords or cookies.

- [ ] **Step 5: Run release verification and commit the release candidate**

Run:

```bash
python -m pytest -v
python -m ruff check src tests scripts
python -m mypy src/quote_app
```

Expected: all tests PASS, lint clean, type check clean.

```bash
git add scripts/generate_stress_workbooks.py docs/testing/stress-300-row.md docs/user-guide.md README.md
git commit -m "docs: prepare quotation app release candidate"
```

## Plan 4 Completion Gate

The first release candidate is ready only when:

- Mac `.app` acceptance is complete;
- Windows portable acceptance is complete on a non-admin account;
- strict Windows evidence matches the approved examples;
- 300-row stress and restart/resume tests pass;
- Excel and WPS both open the final workbook with AL:AN images;
- report overview totals equal detail status totals;
- no input workbook is modified;
- no network traffic occurs except target commerce/official sites;
- updating program files leaves task history and browser login state intact;
- the Chinese user guide is complete and matches the delivered UI.

Create a local version tag only after all evidence is present:

```bash
git tag -a v1.0.0-rc1 -m "福建移动铺货报价助手 v1.0.0-rc1"
```

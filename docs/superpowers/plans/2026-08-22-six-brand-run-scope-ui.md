# Six-Brand Run Scope and Compact UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace acceptance-only run modes with six-brand all-channel scopes, make month selection a dropdown, and reduce the default desktop window height without touching validated site adapters.

**Architecture:** Keep `RunModeScope` as the UI-to-pipeline boundary and map “全品牌” to `selected_brand=None`, while every listed scope maps to `selected_channels=None` so all three channels run. Deactivate ZTE at the task/report support boundary so its rows remain in all-brand output but generate no website tasks and are reported unsupported. Limit layout changes to `QuoteApp._build`.

**Tech Stack:** Python 3.12, Tkinter/ttk, pytest, PyInstaller, existing macOS signing scripts.

**Spec:** `docs/superpowers/specs/2026-08-22-six-brand-run-scope-ui.md`

## Global Constraints

- Base commit is the user-accepted `.118` commit `2366da5`.
- All selected brands run official website, JD, and Tmall channels.
- Supported UI brands are exactly HONOR, Xiaomi, OPPO, vivo, Huawei, and Apple; ZTE is inactive.
- Do not modify site adapter, price extraction, evidence capture, or Excel column-writing logic.
- Use TDD: each production behavior must first have a failing test.

---

### Task 1: Six-brand run scope mapping

**Files:**
- Modify: `tests/unit/test_app.py`
- Modify: `src/quote_app/app.py`

**Interfaces:**
- Consumes: `QuoteApp._selected_run_mode_scope()` and `FullPipelineRequest.selected_brand` / `selected_channels`.
- Produces: `_RUN_MODE_OPTIONS == ("全品牌", "荣耀", "小米", "OPPO", "vivo", "华为", "苹果")`; `RunModeScope.selected_brand: str | None`; all modes return `selected_channels=None`.

- [ ] **Step 1: Write failing mapping and default-selection tests**

  Update `test_gui_official_acceptance_modes_select_one_brand_and_official_channel` into a parameterized all-channel test covering literal expected mappings, add “全品牌” expecting `None`, and assert a normally constructed app defaults to “全品牌”.

- [ ] **Step 2: Run the targeted tests and verify RED**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_app.py -k 'run_mode or approved_mode or default_run' -q`

  Expected: FAIL because the old acceptance labels, official-only channel set, and HONOR default are still present.

- [ ] **Step 3: Implement the minimal mapping change**

  Change `RunModeScope.selected_brand` to `str | None`, replace `_RUN_MODE_SCOPES` with the seven approved labels in the approved order, map every scope to `selected_channels=None`, default `brand_mode_var` to “全品牌”, and update `_selected_brand_from_mode()` to return `str | None`.

- [ ] **Step 4: Run targeted tests and verify GREEN**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_app.py -k 'run_mode or approved_mode or default_run' -q`

  Expected: PASS.

### Task 2: Keep ZTE rows but deactivate ZTE website work

**Files:**
- Modify: `tests/unit/test_task_builder.py`
- Modify: `tests/integration/test_web_to_excel.py`
- Modify: `src/quote_app/tasks/builder.py`
- Modify: `src/quote_app/services/web_binding_validation.py`

**Interfaces:**
- Consumes: normalized row brand `ZTE中兴`.
- Produces: no website tasks for ZTE; `UNSUPPORTED_BRAND` issue; report support set excludes ZTE so its retained output row is marked unsupported.

- [ ] **Step 1: Write failing ZTE-inactive tests**

  Add a task-builder test with a literal ZTE row that expects zero tasks and one `UNSUPPORTED_BRAND` issue. Extend the workbook report fixture to assert a retained ZTE row is marked unsupported across all channels.

- [ ] **Step 2: Run the targeted tests and verify RED**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_task_builder.py tests/integration/test_web_to_excel.py -q`

  Expected: FAIL because ZTE is currently in both active support sets.

- [ ] **Step 3: Implement the minimal active-support change**

  Remove only `ZTE中兴` from `quote_app.tasks.builder.SUPPORTED_BRANDS` and `quote_app.services.web_binding_validation.SUPPORTED_WEB_BRANDS`. Leave site catalog and adapters untouched as dormant compatibility code.

- [ ] **Step 4: Run targeted tests and verify GREEN**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_task_builder.py tests/integration/test_web_to_excel.py -q`

  Expected: PASS.

### Task 3: Compact window and month dropdown

**Files:**
- Modify: `tests/unit/test_app.py`
- Modify: `src/quote_app/app.py`

**Interfaces:**
- Consumes: current natural month from `QuoteMonth.current()`.
- Produces: root geometry `900x620`; readonly month combobox values `("1", ..., "12")`; run-mode combobox values in approved order; status height `8`.

- [ ] **Step 1: Write failing layout behavior tests**

  Extend the existing widget harness to record root geometry, both combobox configurations, and `ScrolledText` options. Assert the month selector is readonly with literal month values, the run selector is readonly with the seven labels, the root receives `900x620`, and status height is `8`.

- [ ] **Step 2: Run the layout test and verify RED**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_app.py::test_gui_build_uses_compact_window_and_readonly_selectors -q`

  Expected: FAIL because month is an entry, no geometry is set, and status height is 13.

- [ ] **Step 3: Implement minimal Tk layout changes**

  Call `self.root.geometry("900x620")`; replace the month entry with a readonly `ttk.Combobox` containing strings `1` through `12`; keep the year entry; set run-mode combobox width to the longest short label; set `ScrolledText(..., height=8)`; preserve resize weights and button behavior.

- [ ] **Step 4: Run layout and full app tests and verify GREEN**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_app.py -q`

  Expected: PASS.

### Task 4: Release label, regression verification, and macOS package

**Files:**
- Modify: `tests/unit/test_app.py`
- Modify: `src/quote_app/app.py`
- Create: `dist-six-brand-ui-119/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: validated `.118` package configuration.
- Produces: `.119` signed Mac test package with the six-brand UI change.

- [ ] **Step 1: Write the failing build-label expectation**

  Set the literal expected label to `六品牌全站运行范围与紧凑界面版（六品牌官网冻结）2026.08.22.119`.

- [ ] **Step 2: Run the label test and verify RED**

  Run: `../core-excel/.venv/bin/pytest tests/unit/test_app.py::test_app_build_label_identifies_six_brand_full_site_scope -q`

  Expected: FAIL with the `.118` label.

- [ ] **Step 3: Update label and user notice**

  Update `APP_BUILD_LABEL` and make `BETA_NOTICE` state that all selected brands execute official, JD, and Tmall while existing login/verification continuation guidance remains.

- [ ] **Step 4: Run complete verification**

  Run: `../core-excel/.venv/bin/pytest -q`

  Run the single local-port persistence test outside the restricted sandbox if necessary.

  Run: `../core-excel/.venv/bin/ruff check src tests`

  Run: `../core-excel/.venv/bin/mypy src`

  Expected: all tests pass; Ruff and Mypy exit 0.

- [ ] **Step 5: Build and validate the Mac package**

  Reuse the existing packaging entry point to build into `dist-six-brand-ui-119`, then run the repository's strict macOS signing validation against the generated `.app`.

  Expected: build exits 0 and signing validation exits 0.

- [ ] **Step 6: Commit the reviewed implementation**

  Stage only the spec, plan, tests, and source changes; do not stage generated `dist-*` artifacts if they are ignored.

  Commit message: `feat: add six-brand full-site run scope UI`

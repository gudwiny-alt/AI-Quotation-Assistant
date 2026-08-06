# Tmall Detail Early 80% Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an approved Tmall product detail page enter and remain at 80% before SKU selection and price verification, then keep that scale through formal capture.

**Architecture:** Keep the change inside `TmallAdapter`. The detail observation owns the initial 80% transition and restores it on observation failure; formal capture reuses the existing scale for detail outcomes, while the unchanged search-result `NO_MODEL` path still applies 80% during capture preparation. Existing runtime cleanup remains responsible for restoration after successful capture.

**Tech Stack:** Python 3.12, Playwright page abstraction, pytest contract tests, PyInstaller, macOS ad-hoc codesign.

## Global Constraints

- Only Tmall detail-page scale timing and lifecycle may change.
- Do not change JD, official-site, Excel, price-selection, seller, model, memory, color, or price validation.
- Tmall search-result `NO_MODEL` behavior remains unchanged.
- Stock and delivery region remain irrelevant to quotation proof.
- One attempt applies detail scale once; observation, positioning, and capture reuse it.
- Any failure after detail scaling restores the prior scale exactly once.
- Build a new `.45` app without overwriting `.44` or any older package.

---

### Task 1: Move Tmall detail scaling before SKU and price observation

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `src/quote_app/sites/tmall.py`

**Interfaces:**
- Consumes: `apply_capture_scale(page, scale=0.8)`, `restore_capture_scale(page)`, `TmallAdapter._observe_detail(...)`, `TmallAdapter.prepare_capture_view(...)`
- Produces: one 80% scale lifecycle spanning detail observation through formal screenshot

- [ ] **Step 1: Add a fixture event for the real scale side effect**

In `_FixturePage.evaluate`, when `value is not None`, append `"scale:0.8"` to
`option_events` after recording the scale. Keep restoration counters unchanged.

- [ ] **Step 2: Write failing tests for early order and observation cleanup**

Add tests equivalent to:

```python
def test_tmall_detail_scales_before_sku_selection_and_reuses_scale_for_capture() -> None:
    page = _FixturePage()
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_events == [
        "scale:0.8",
        "scroll:capacity",
        "click:capacity",
        "scroll:color",
        "click:color",
    ]
    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 0

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.capture_scales == [0.8]
    assert page.capture_view_positions == ["capacity"]
    adapter.restore_capture_view(task, cast(Any, page), observation.semantic_state)
    assert page.capture_scale_restore_count == 1
```

```python
def test_tmall_detail_observation_failure_restores_early_scale_once() -> None:
    page = _FixturePage(
        price_snapshots=(("¥4299",), ("¥4399",), ("¥4299",), ("¥4399",)),
    )
    adapter = TmallAdapter(_xiaomi_spec())

    with pytest.raises(
        LayoutRecognitionError,
        match="price did not reach a verified stable state",
    ):
        adapter.observe(_task(), cast(Any, page))

    assert page.capture_scales == [0.8]
    assert page.capture_scale_restore_count == 1
    assert page.capture_scale == 1.0
```

Also extend the existing Tmall `NO_MODEL` capture test with:

```python
assert page.capture_scales == []  # immediately after observe
```

This proves the search-result path has not moved.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_tmall_adapter.py::test_tmall_detail_scales_before_sku_selection_and_reuses_scale_for_capture \
  tests/contract/test_tmall_adapter.py::test_tmall_detail_observation_failure_restores_early_scale_once \
  tests/contract/test_tmall_adapter.py::test_tmall_no_model_prepares_a_result_view_with_readable_card_names -q
```

Expected: the detail tests fail because old code first scales in
`prepare_capture_view`; the no-model assertion passes.

- [ ] **Step 4: Apply 80% once inside the validated detail observation**

In `_observe_detail`, retain navigation, blocking checks, exact URL validation,
and `_wait_for_detail_layout` before scaling. Immediately after those checks,
wrap the existing SKU/price logic in one scale lifecycle:

```python
        try:
            apply_capture_scale(browser_page, scale=0.8)
            # existing capacity, color, configuration and price logic
            return observation
        except BaseException:
            try:
                restore_capture_scale(browser_page)
            except Exception:
                pass
            raise
```

Successful detail observations deliberately keep 80%. Early detail business
outcomes also retain the same scale until their capture finishes.

- [ ] **Step 5: Reuse early scale in capture preparation**

Change `prepare_capture_view` so only `BusinessOutcome.NO_MODEL` applies 80%
there. Detail outcomes call `_prepare_capture_view_at_scale` without applying a
second scale. Preserve the existing exception handler that restores scale when
capture preparation fails:

```python
        try:
            if expected.outcome is BusinessOutcome.NO_MODEL:
                apply_capture_scale(browser_page, scale=0.8)
            self._prepare_capture_view_at_scale(task, browser_page, expected)
        except BaseException:
            try:
                restore_capture_scale(browser_page)
            except Exception:
                pass
            raise
```

- [ ] **Step 6: Run GREEN tests and the full Tmall contract suite**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py -q
.venv/bin/ruff check src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
```

Expected: all Tmall contract tests pass and Ruff reports no issues.

- [ ] **Step 7: Commit the isolated Tmall change**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: scale Tmall detail before offer verification"
```

---

### Task 2: Freeze regressions and build the independent `.45` Mac app

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-tmall-early-scale-45/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: Task 1 Tmall behavior
- Produces: signed `.45` test application containing current app/JD/Tmall code

- [ ] **Step 1: Update the build-label test first and verify RED**

Set the expected label to:

```python
"天猫详情页提前80%缩放版（全部荣耀行）2026.08.06.45"
```

Run:

```bash
.venv/bin/pytest tests/unit/test_app.py -q
```

Expected: one failure because production still reports `.44`.

- [ ] **Step 2: Change only `APP_BUILD_LABEL` and verify GREEN**

Set `APP_BUILD_LABEL` to the exact `.45` value above, then run:

```bash
.venv/bin/pytest tests/unit/test_app.py -q
```

Expected: all app tests pass.

- [ ] **Step 3: Run the frozen regression matrix**

```bash
.venv/bin/pytest \
  tests/contract/test_tmall_adapter.py \
  tests/contract/test_jd_adapter.py \
  tests/regression/test_honor_official_baseline.py \
  tests/integration/test_web_to_excel.py \
  tests/unit/test_macos_capture_runtime.py -q
```

Expected: all tests pass, covering Tmall detail and no-model, JD, official,
Excel incremental writes, and scale restoration after capture.

- [ ] **Step 4: Run the complete suite and Ruff**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

Expected: all tests pass. Run the persistent-browser localhost test outside
the sandbox only if the sandbox alone blocks `127.0.0.1` binding.

- [ ] **Step 5: Commit the label and build a new app**

```bash
git add src/quote_app/app.py tests/unit/test_app.py
git commit -m "chore: label early Tmall scale build"
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-tmall-early-scale-45 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-tmall-early-scale-45 \
  --workpath build-tmall-early-scale-45 \
  packaging/quotation_app.spec
codesign --force --deep --sign - \
  'dist-tmall-early-scale-45/福建移动铺货报价助手.app'
codesign --verify --deep --strict --verbose=2 \
  'dist-tmall-early-scale-45/福建移动铺货报价助手.app'
```

- [ ] **Step 6: Verify packaged bytecode freshness**

Extract `quote_app.app`, `quote_app.sites.tmall`, and `quote_app.sites.jd` from
the app's PyInstaller `CArchive`/`PYZ.pyz`; recursively normalize nested
`co_filename`, then compare marshal SHA-256 values with current source compiled
using `optimize=0`.

Expected:

```text
quote_app.app match=True
quote_app.sites.tmall match=True
quote_app.sites.jd match=True
ALL_MATCH=True
```

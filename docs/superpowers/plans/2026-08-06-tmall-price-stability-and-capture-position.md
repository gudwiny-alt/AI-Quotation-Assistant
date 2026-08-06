# Tmall Price Stability and Capture Position Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an approved Tmall detail page proceed when the selected quotation price becomes stable within five seconds, then perform exactly one targeted detail-positioning scroll before formal capture.

**Architecture:** Keep the existing Tmall adapter and capture-view boundary. Change only Tmall's condition-based price poll so configuration/title remain fail-closed while equality is based on the policy-selected quotation price rather than every auxiliary price node; retain the existing one-shot capacity-centered capture positioning and lock its timing with contract tests.

**Tech Stack:** Python 3.12, pytest, Playwright-compatible page protocol, PyInstaller, macOS ad-hoc codesign.

## Global Constraints

- Modify only Tmall detail price stability and Tmall formal detail capture positioning contracts.
- Do not modify JD, official-site, Excel generation/writing, Tmall search, login/risk control, seller/model/configuration validation, or price policy.
- Keep 80% scale active before SKU selection and restore it on success or failure.
- Wait at most 5 seconds; return immediately after the same valid policy-selected quotation price is observed twice consecutively.
- After price confirmation, perform one existing capacity-centered targeted scroll and require title, quotation price, capacity, and color in the same viewport.
- Build a new independent `.46` macOS app; do not overwrite `.45`.

---

### Task 1: Stabilize the selected Tmall quotation price

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `src/quote_app/sites/tmall.py`

**Interfaces:**
- Consumes: `TmallAdapter._visible_configuration_evidence(page, task) -> TmallVisibleConfigurationEvidence` and `choose_price(candidates, policy) -> Decimal | None`.
- Produces: `TmallAdapter._stable_visible_price(page, task, configuration) -> Decimal`, with a five-second bounded poll based on two consecutive equal selected quotation prices.

- [ ] **Step 1: Write the failing auxiliary-price-churn test**

Add a contract test using the real `TmallAdapter.observe` path. Feed successive price snapshots whose auxiliary candidate changes while the policy-selected quotation remains `4399`, then assert:

```python
def test_changing_auxiliary_prices_do_not_block_a_stable_selected_quotation() -> None:
    observation = _observe(
        price_snapshots=(
            ("¥4,099", "¥4,399", "¥9,999"),
            ("¥4,199", "¥4,399", "¥9,999"),
            ("¥4,299", "¥4,399", "¥9,999"),
            ("¥4,099", "¥4,399", "¥9,999"),
            ("¥4,199", "¥4,399", "¥9,999"),
        ),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4399")
```

This catches the current bug: comparing the complete `TmallVisibleConfigurationEvidence` snapshots rejects a stable selected quotation when an irrelevant price node changes.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py::test_changing_auxiliary_prices_do_not_block_a_stable_selected_quotation -q
```

Expected: FAIL with `Tmall selected variant price did not reach a verified stable state`.

- [ ] **Step 3: Extend the timeout-failure contract before production changes**

Update `test_price_snapshot_must_stabilize_after_sku_identity` so the selected quotation changes on every one of 21 polls for the full five-second window. Assert that `LayoutRecognitionError` is still raised and that the fixture records exactly 20 waits of `250` milliseconds. The test must use the real adapter and fixture page; no production method mocking.

- [ ] **Step 4: Implement the minimal condition-based poll**

In `src/quote_app/sites/tmall.py`:

```python
_PRICE_STABILITY_TIMEOUT_MS = 5_000
_PRICE_POLL_INTERVAL_MS = 250
_MAX_PRICE_POLLS = (
    _PRICE_STABILITY_TIMEOUT_MS // _PRICE_POLL_INTERVAL_MS
) + 1
```

Refactor `_stable_visible_price` to perform at most `_MAX_PRICE_POLLS` evidence samples. Each poll still proves the title and selected capacity/color are unchanged, calculates `selected = choose_price(...)`, and returns only when `selected is not None and selected == previous_selected`. Wait `_PRICE_POLL_INTERVAL_MS` only between polls, producing 20 waits and a 5-second maximum; after the final poll, raise the existing stable-state error. Do not compare the full price-candidate tuple for equality.

- [ ] **Step 5: Verify GREEN and the existing Tmall contract**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_tmall_adapter.py::test_changing_auxiliary_prices_do_not_block_a_stable_selected_quotation \
  tests/contract/test_tmall_adapter.py::test_price_snapshot_must_stabilize_after_sku_identity -q
.venv/bin/pytest tests/contract/test_tmall_adapter.py -q
.venv/bin/ruff check src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
```

Expected: all selected tests pass, the full Tmall contract passes, and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit the focused Tmall change**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: wait for stable Tmall quotation price"
```

---

### Task 2: Lock one-shot capture positioning and label `.46`

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `tests/unit/test_app.py`
- Modify: `src/quote_app/app.py`

**Interfaces:**
- Consumes: `TmallAdapter.prepare_capture_view(...)` and `position_detail_for_capture(...)`.
- Produces: one capacity-centered capture position after successful observation, plus `APP_BUILD_LABEL = "天猫报价稳定等待与定向截图版（全部荣耀行）2026.08.06.46"`.

- [ ] **Step 1: Strengthen the existing capture-position test**

Update `test_tmall_detail_scales_before_selection_and_positions_only_for_capture` to use the auxiliary-price-churn fixture from Task 1. Before `prepare_capture_view`, assert `capture_view_positions == []`; afterward assert exactly:

```python
assert page.capture_view_positions == ["capacity"]
```

This proves the scroll occurs once and only after a stable quotation observation has completed.

- [ ] **Step 2: Run the capture-position test**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py::test_tmall_detail_scales_before_selection_and_positions_only_for_capture -q
```

Expected: PASS, confirming the existing one-shot capture helper already satisfies the approved scroll design without changing shared JD/official capture code.

- [ ] **Step 3: Write the `.46` label expectation and verify RED**

Change only the expected label in `tests/unit/test_app.py`:

```python
assert APP_BUILD_LABEL == "天猫报价稳定等待与定向截图版（全部荣耀行）2026.08.06.46"
```

Run:

```bash
.venv/bin/pytest tests/unit/test_app.py::test_app_build_label_identifies_honor_three_site_test_scope -q
```

Expected: FAIL because production still reports `.45`.

- [ ] **Step 4: Update only the application label and verify GREEN**

In `src/quote_app/app.py`, set:

```python
APP_BUILD_LABEL = "天猫报价稳定等待与定向截图版（全部荣耀行）2026.08.06.46"
```

Run the same unit test and expect PASS.

- [ ] **Step 5: Run frozen and full regression gates**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_tmall_adapter.py \
  tests/contract/test_jd_adapter.py \
  tests/regression/test_honor_official_baseline.py \
  tests/integration/test_web_to_excel.py \
  tests/unit/test_macos_capture_runtime.py -q
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

If the persistent-browser test cannot bind `127.0.0.1` in the sandbox, rerun the full suite with the approved local-loopback permission and require a clean pass.

- [ ] **Step 6: Commit the `.46` label and capture contract**

```bash
git add src/quote_app/app.py tests/unit/test_app.py tests/contract/test_tmall_adapter.py
git commit -m "chore: label Tmall quotation stability build"
```

---

### Task 3: Build, sign, and verify the independent `.46` app

**Files:**
- Create: `dist-tmall-price-stability-46/福建移动铺货报价助手.app`
- Create: `build-tmall-price-stability-46/` (PyInstaller intermediate output)

**Interfaces:**
- Consumes: committed `.46` source, `packaging/quotation_app.spec`, and the project `.venv`.
- Produces: a signed arm64 macOS app whose embedded `quote_app.app`, `quote_app.sites.tmall`, `quote_app.sites.jd`, and `quote_app.sites.official` bytecode matches current source.

- [ ] **Step 1: Build into a new directory**

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-tmall-price-stability-46 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-tmall-price-stability-46 \
  --workpath build-tmall-price-stability-46 \
  packaging/quotation_app.spec
```

Expected: successful build without overwriting `.45`.

- [ ] **Step 2: Sign and strictly verify**

```bash
codesign --force --deep --sign - \
  'dist-tmall-price-stability-46/福建移动铺货报价助手.app'
codesign --verify --deep --strict --verbose=2 \
  'dist-tmall-price-stability-46/福建移动铺货报价助手.app'
```

Expected: `valid on disk` and `satisfies its Designated Requirement`.

- [ ] **Step 3: Verify packaged bytecode freshness**

Open the app executable with PyInstaller `CArchiveReader`, open `PYZ.pyz`, recursively normalize nested code-object `co_filename`, and compare `sha256(marshal.dumps(...))` with current source compiled using `optimize=0` for:

```text
quote_app.app
quote_app.sites.tmall
quote_app.sites.jd
quote_app.sites.official
```

Expected: all four source/package hashes match and `ALL_MATCH=True`.

- [ ] **Step 4: Deliver the package for live Tmall Power2 validation**

Report the automated test, signing, and freshness evidence. State explicitly that the real Tmall Power2 result remains pending the user's live-site run; do not claim the website issue is fixed before that run.

# OPPO Selected Option and QR Occlusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent OPPO A5m from failing when its already-selected capacity is covered by the fixed QR panel, and make one safe positioning attempt when that panel occludes formal screenshot evidence.

**Architecture:** Keep all production behavior inside `OppoOfficialAdapter`. Add an idempotent option-selection helper that skips clicks for an already unique selected target. Strengthen the existing four-proof viewport check with hit-test occlusion detection and replace the current broad positioning with one capacity-anchored, bounded vertical movement followed by fresh proof lookup.

**Tech Stack:** Python 3.12, Playwright-compatible synchronous page protocol, pytest contract/integration fixtures, Ruff, Mypy, PyInstaller, macOS ad-hoc codesign.

## Global Constraints

- Only OPPO official-site production behavior may change.
- Do not modify HONOR, Xiaomi, JD, Tmall, Excel, or other official brand adapters.
- Do not weaken exact model, capacity, color, price, or four-proof screenshot semantics.
- Do not hide or mutate the OPPO QR overlay.
- Formal capture may perform at most one targeted positioning action.
- Preserve `.67`; build `.68` in independent build/dist directories.

---

### Task 1: Lock Already-Selected Configuration Behavior

**Files:**
- Modify: `tests/contract/test_official_oppo_live.py`
- Modify: `src/quote_app/sites/official_brands/oppo.py`

**Interfaces:**
- Consumes: `_exact_option(page, kind, task)`, `_require_unique_selected_option(page, kind, task)`, `_is_selected(locator)`.
- Produces: `_select_exact_option(page: Any, kind: str, task: WebsiteTask) -> None`.

- [x] **Step 1: Add a fixture click blocker and failing A5m test**

Extend the OPPO locator fixture so a selected capacity can raise on click, modeling the QR overlay. Add a test using `OPPO A5m 5G / 8GB+256GB / 钻石白` where capacity and color start selected; assert `PRICE_FOUND` and `option_clicks == []`.

- [x] **Step 2: Run the focused test and verify RED**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py -k 'already_selected_target'
```

Expected: failure because current production unconditionally clicks capacity and color.

- [x] **Step 3: Implement the minimal idempotent selector**

Add `_select_exact_option`. It must first obtain the exact target, preserve existing absent/disabled behavior in the caller, return immediately only when `_require_unique_selected_option` proves the exact target is already uniquely selected, and otherwise click once then wait for selected stability.

- [x] **Step 4: Use the helper for capacity and color**

Replace unconditional `click()` plus `_wait_for_selected_option()` in `_observe_loaded_detail`; keep capacity revalidation after color selection.

- [x] **Step 5: Run OPPO contracts and verify GREEN**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py
```

Expected: all OPPO contracts pass, including old unselected-click tests and new already-selected tests.

---

### Task 2: Detect QR Occlusion and Position Once

**Files:**
- Modify: `tests/contract/test_official_oppo_live.py`
- Modify: `src/quote_app/sites/official_brands/oppo.py`

**Interfaces:**
- Consumes: `_capture_proof_locators`, `_proof_group_fits_current_viewport`, `_scroll_proof_group_into_view`.
- Produces: occlusion-aware `_proof_group_fits_current_viewport` and one bounded positioning attempt.

- [x] **Step 1: Extend the fixture with proof-occlusion states**

Model `capacity_occluded`, `capacity_occluded_after_position`, and record `position_attempts`. The viewport evaluation must return false when the capacity center hit-test resolves to the QR overlay even though all four rectangles are inside the viewport.

- [x] **Step 2: Add RED contracts**

Add one test where capacity is initially occluded and becomes clear after one positioning attempt; expect capture preparation success and exactly one attempt. Add another where it remains occluded; expect `LayoutRecognitionError`, exactly one attempt, and restored scale.

- [x] **Step 3: Run focused tests and verify RED**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py -k 'qr_occlusion'
```

Expected: tests fail because current fit check ignores element occlusion.

- [x] **Step 4: Add hit-test occlusion validation**

Within the existing page-evaluated proof check, require each proof center to resolve through `document.elementsFromPoint`. Accept only when the first pointer-relevant hit is the proof itself or its descendant/ancestor relationship; reject a fixed overlay above it.

- [x] **Step 5: Make the single positioning action capacity-aware**

Keep one `window.scrollTo` call. Compute a small vertical adjustment that moves the proof union upward only as far as needed while maintaining the union inside the viewport. Wait for layout stabilization and let the caller reacquire all proof locators.

- [x] **Step 6: Run OPPO contracts and integration tests**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py tests/integration/test_oppo_official_pipeline.py tests/unit/test_oppo_search_diagnostics.py tests/contract/test_official_adapters.py
```

Expected: all selected tests pass.

---

### Task 3: Cross-Site Regression and Independent Review

**Files:**
- Test only; production files outside OPPO must remain unchanged.

**Interfaces:**
- Consumes: completed OPPO patch.
- Produces: fresh regression and review evidence.

- [x] **Step 1: Run frozen-site representative regression**

```bash
.venv/bin/pytest -q tests/contract/test_official_honor_live.py tests/contract/test_official_xiaomi_live.py tests/integration/test_xiaomi_official_pipeline.py tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/integration/test_core_pipeline.py tests/integration/test_full_pipeline_fixture_sites.py
```

- [x] **Step 2: Run static verification**

```bash
.venv/bin/ruff check src/quote_app/sites/official_brands/oppo.py tests/contract/test_official_oppo_live.py
.venv/bin/mypy src/quote_app/sites/official_brands/oppo.py
```

- [x] **Step 3: Request focused independent review**

Reject any change outside OPPO production code, any weakening of exact configuration or price rules, overlay DOM mutation, more than one positioning action, or regression of the 80% scale lifecycle.

---

### Task 4: Commit and Build `.68`

**Files:**
- Commit: OPPO production code, OPPO contracts, this plan.
- Create (untracked): `build-official-oppo-68/`
- Create (untracked): `dist-official-oppo-68/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: reviewed source at a fixed commit.
- Produces: signed `.68` Mac test bundle.

- [ ] **Step 1: Protect `.67` and ensure new paths do not exist**

```bash
test -d dist-official-oppo-67/福建移动铺货报价助手.app
test ! -e build-official-oppo-68
test ! -e dist-official-oppo-68
```

- [ ] **Step 2: Commit only scoped source/test/plan files**

```bash
git add docs/superpowers/plans/2026-08-12-oppo-selected-option-qr-occlusion.md src/quote_app/sites/official_brands/oppo.py tests/contract/test_official_oppo_live.py
git commit -m "fix: avoid OPPO QR occlusion"
```

- [ ] **Step 3: Build independently**

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-oppo-68 .venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist-official-oppo-68 --workpath build-official-oppo-68 packaging/quotation_app.spec
```

- [ ] **Step 4: Sign and verify**

```bash
codesign --force --deep --sign - dist-official-oppo-68/福建移动铺货报价助手.app
codesign --verify --deep --strict --verbose=2 dist-official-oppo-68/福建移动铺货报价助手.app
```

- [ ] **Step 5: Run packaging smoke and confirm `.67` remains**

```bash
.venv/bin/pytest -q tests/smoke/test_packaging_spec.py
du -sh dist-official-oppo-68/福建移动铺货报价助手.app
test -d dist-official-oppo-67/福建移动铺货报价助手.app
```

Expected: build, signing, strict verification, and smoke test pass; `.67` remains unchanged.

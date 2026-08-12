# OPPO Detail Early 80% Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every verified OPPO product-detail page is idempotently set to 80% before title, configuration, price, and screenshot processing, so A5m and A6t can produce complete evidence.

**Architecture:** Keep all behavior inside `OppoOfficialAdapter`. Introduce one private detail-view preparation method used immediately after first detail navigation and checkpoint detail recovery. Restore the prior zoom when business observation finishes, then have formal capture preparation idempotently establish 80% before reading the four proof locators; permit at most one existing targeted scroll.

**Tech Stack:** Python 3.12, Playwright-compatible synchronous page protocol, pytest contract/integration fixtures, Ruff, Mypy, PyInstaller, macOS ad-hoc codesign.

## Global Constraints

- Only OPPO official-site production behavior may change.
- Do not modify HONOR, Xiaomi, JD, Tmall, Excel, or other official brand adapters.
- Search-card matching, sold-out handling, 5G normalization, exact configuration matching, and price semantics remain unchanged.
- The formal screenshot still requires title, current price, target capacity, and target color in the same viewport.
- Detail scaling is idempotent: if already at 80%, do not rescale.
- At most one targeted scroll is allowed during formal capture preparation.
- The new macOS bundle must be produced in `dist-official-oppo-67/` and must not overwrite `.66`.

---

### Task 1: Lock Early Detail Scaling With Failing Contracts

**Files:**
- Modify: `tests/contract/test_official_oppo_live.py`

**Interfaces:**
- Consumes: `_OppoFixturePage`, `_set_result_cards`, `_set_detail_product`, `OppoOfficialAdapter.observe`, `OppoOfficialAdapter.resume`.
- Produces: contracts proving scale ordering, idempotence, and A5m/A6t behavior.

- [x] **Step 1: Extend the fixture to record first detail reads**

Record `capture_scale` whenever the detail title, capacity, color, or price node is first read. The literal expected first detail business scale is `0.8`.

- [x] **Step 2: Add the first-observe A5m contract**

Use `OPPO A5m 5G / 8GB+256GB / 钻石白`; assert `PRICE_FOUND`, the first detail read is at `0.8`, and the transition occurs once.

- [x] **Step 3: Add checkpoint-resume and already-80% contracts**

Resume a detail checkpoint from 100% and assert the first semantic read occurs at `0.8`. Start another at `0.8` and assert no redundant scale write.

- [x] **Step 4: Run RED tests**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py -k 'early_scale or scale_before or already_eighty'
```

Expected: new observe and resume cases fail because production currently scales only in `prepare_capture_view`.

---

### Task 2: Scale Immediately After OPPO Detail Navigation

**Files:**
- Modify: `src/quote_app/sites/official_brands/oppo.py`
- Test: `tests/contract/test_official_oppo_live.py`

**Interfaces:**
- Consumes: `ensure_capture_scale(page, scale=0.8)`, `_is_detail_url`, `_observe_loaded_detail`.
- Produces: `_prepare_detail_view(page: Any) -> None`, called before any detail semantic read.

- [x] **Step 1: Add the minimal detail preparation method**

Validate the current URL is a product detail and call the existing idempotent `ensure_capture_scale(page, scale=0.8)` utility. Do not add a second scale state.

- [x] **Step 2: Call it on first navigation and checkpoint recovery**

In `_observe_validated`, call it after the detail URL check and before `_observe_loaded_detail`. In `_resume_validated`, call it after rejecting non-detail recovery URLs and before `_observe_loaded_detail`.

- [x] **Step 3: Run GREEN contracts**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py
```

Expected: every OPPO contract passes, including A5m, A6t, sold-out, early scale, resume, and idempotence.

---

### Task 3: Reapply 80% During Capture and Limit Scrolling

**Files:**
- Modify: `src/quote_app/sites/official_brands/oppo.py` only if RED proves a mismatch
- Modify: `tests/contract/test_official_oppo_live.py`

**Interfaces:**
- Consumes: `_capture_proof_locators`, `_proof_group_fits_current_viewport`, `_scroll_proof_group_into_view`.
- Produces: zero-scroll capture when proofs fit and at most one targeted scroll otherwise.

- [x] **Step 1: Add the proofs-already-fit contract**

Observe A5m and assert observation restores the prior scale. Prepare capture and assert all formal proof reads occur at `0.8` and `position_attempts == 0`.

- [x] **Step 2: Add one-scroll and fail-closed contracts**

When proofs fit only after targeted positioning, assert exactly one position attempt. When they still do not fit, assert `LayoutRecognitionError` and exactly one attempt.

- [x] **Step 3: Make only the change demanded by RED**

Restore after observation, then establish 80% at the start of formal capture before reading any proof. Retain four-proof identity, viewport verification, and one targeted scroll.

- [x] **Step 4: Run capture contracts**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py -k 'capture or scale'
```

Expected: all selected contracts pass.

---

### Task 4: Cross-Site Regression and Review

**Files:**
- Test only; no production files outside OPPO.

**Interfaces:**
- Consumes: completed OPPO changes.
- Produces: regression and review evidence.

- [ ] **Step 1: Run OPPO and general official tests**

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py tests/integration/test_oppo_official_pipeline.py tests/unit/test_oppo_search_diagnostics.py tests/contract/test_official_adapters.py
```

- [ ] **Step 2: Run frozen-site representative regression**

```bash
.venv/bin/pytest -q tests/contract/test_official_honor_live.py tests/contract/test_official_xiaomi_live.py tests/integration/test_xiaomi_official_pipeline.py tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/integration/test_core_pipeline.py tests/integration/test_full_pipeline_fixture_sites.py
```

- [ ] **Step 3: Run static verification**

```bash
.venv/bin/ruff check src/quote_app/sites/official_brands/oppo.py tests/contract/test_official_oppo_live.py
.venv/bin/mypy src/quote_app/sites/official_brands/oppo.py
```

- [ ] **Step 4: Request focused review**

Reject any change outside OPPO production code, any weakening of exact model/configuration/price rules, or more than one targeted scroll.

---

### Task 5: Build and Verify `.67`

**Files:**
- Create (untracked): `build-official-oppo-67/`
- Create (untracked): `dist-official-oppo-67/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: verified source and `packaging/quotation_app.spec`.
- Produces: independently signed `.67` bundle.

- [ ] **Step 1: Protect existing bundles**

```bash
test ! -e build-official-oppo-67
test ! -e dist-official-oppo-67
test -d dist-official-oppo-66/福建移动铺货报价助手.app
```

- [ ] **Step 2: Build**

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-oppo-67 .venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist-official-oppo-67 --workpath build-official-oppo-67 packaging/quotation_app.spec
```

- [ ] **Step 3: Sign and verify**

```bash
codesign --force --deep --sign - dist-official-oppo-67/福建移动铺货报价助手.app
codesign --verify --deep --strict --verbose=2 dist-official-oppo-67/福建移动铺货报价助手.app
```

- [ ] **Step 4: Run packaging smoke and verify `.66` remains**

```bash
.venv/bin/pytest -q tests/smoke/test_packaging_spec.py
du -sh dist-official-oppo-67/福建移动铺货报价助手.app
test -d dist-official-oppo-66/福建移动铺货报价助手.app
```

Expected: build, signing, strict verification, and smoke test pass; `.66` remains unchanged.

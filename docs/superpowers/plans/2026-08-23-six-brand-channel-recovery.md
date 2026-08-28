# Six-Brand Channel Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the six supported brands across official, Tmall, and JD channels without regressing already verified brand flows.

**Architecture:** Preserve brand-specific official adapters and add only targeted model/template fallbacks for the three failing official brands. Introduce approved brand-specific marketplace page profiles for store identity and search entry while retaining the shared detail/price/capture engines. Separate semantic capture acceptance from native macOS window correlation.

**Tech Stack:** Python 3.12, Playwright, SQLite checkpoints, pytest, PyInstaller, macOS native capture bridge.

**Spec:** `docs/superpowers/specs/2026-08-23-six-brand-channel-recovery-design.md`

## Global Constraints

- Supported brands remain exactly HONOR, 小米, OPPO/欧珀, vivo/维沃, 华为, 苹果; ZTE remains unsupported.
- Do not modify verified HONOR, vivo, or Apple official behavior while recovering other official brands.
- Execute official and Tmall before JD; JD remains the final isolated browser phase.
- Preserve incremental task checkpoints and the two-file Excel output contract.
- Every behavior change starts with a failing regression test and ends with focused plus full verification.

---

### Task 1: Freeze the six-brand official regression matrix

**Files:**
- Modify: `tests/regression/test_official_brand_isolation.py`
- Modify: `tests/contract/test_official_oppo_live.py`
- Modify: `tests/contract/test_official_xiaomi_live.py`
- Modify: `tests/contract/test_official_huawei_live.py`
- Create: `tests/fixtures/sites/official_live/oppo/a6_no_exact_model.html`
- Create: `tests/fixtures/sites/official_live/xiaomi/r70_search_results.html`
- Create: `tests/fixtures/sites/official_live/huawei/home_new_search.html`

**Interfaces:**
- Consumes: `create_official_adapter(spec)`, `WebsiteTask`.
- Produces: regression contracts for current real-page states.

- [ ] Add failing tests proving OPPO A6 no-model evidence uses the visible query, Xiaomi R70 search selects an exact product card, and Huawei accepts the current homepage search control.
- [ ] Run the three focused contract files and confirm the new tests fail for the diagnosed reasons.
- [ ] Implement the smallest changes inside only `official_brands/oppo.py`, `official_brands/xiaomi.py`, and `official_brands/huawei.py`.
- [ ] Run all official contract, integration, and regression tests.
- [ ] Commit the official recovery checkpoint.

### Task 2: Add marketplace entry/search profiles

**Files:**
- Create: `src/quote_app/sites/marketplace_profiles.py`
- Modify: `src/quote_app/sites/jd.py`
- Modify: `src/quote_app/sites/tmall.py`
- Modify: `src/quote_app/sites/locators.py`
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `tests/contract/test_tmall_adapter.py`
- Create: `tests/fixtures/sites/jd/store_templates.html`
- Create: `tests/fixtures/sites/tmall/store_templates.html`

**Interfaces:**
- Produces: `MarketplacePageProfile` and `marketplace_profile_for(brand, channel)`.
- Consumers: `JDAdapter._require_approved_store`, `JDAdapter._wait_for_store_search_controls`, `TmallAdapter._require_approved_store`, `TmallAdapter._wait_for_store_search_controls`.

- [ ] Add failing parameterized tests for all six approved store names and observed store/search templates.
- [ ] Confirm HONOR legacy fixtures still pass before implementation.
- [ ] Implement normalized, host-bound store identity and profile-specific search selectors.
- [ ] Verify all six profiles submit the exact task model once and never accept an unapproved store.
- [ ] Run the full JD/Tmall contract suites and commit the marketplace entry checkpoint.

### Task 3: Recover Tmall detail and capture flow

**Files:**
- Modify: `src/quote_app/sites/tmall.py`
- Modify: `tests/contract/test_tmall_adapter.py`
- Add fixtures under: `tests/fixtures/sites/tmall/`

**Interfaces:**
- Consumes: brand-specific entry/search profiles from Task 2.
- Produces: shared detail observation supporting the six brands.

- [ ] Add failing tests for Xiaomi no-model evidence, OPPO/vivo/Huawei search-to-detail navigation, and Apple approved-store/detail identity.
- [ ] Implement the minimum profile-aware result-card and detail-title matching changes.
- [ ] Preserve HONOR price and screenshot behavior with frozen regression assertions.
- [ ] Run Tmall contract, scheduler, checkpoint, and Excel integration tests.
- [ ] Commit the Tmall recovery checkpoint.

### Task 4: Recover JD detail/capture and macOS window correlation

**Files:**
- Modify: `src/quote_app/sites/jd.py`
- Modify: `src/quote_app/evidence/macos_binding.py`
- Modify: `src/quote_app/evidence/macos_runtime.py`
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `tests/unit/test_macos_binding.py`
- Modify: `tests/integration/test_macos_runner_capture.py`

**Interfaces:**
- Consumes: brand-specific entry/search profiles from Task 2.
- Produces: six-brand JD observation and a unique-active-Chrome-window capture binding.

- [ ] Add failing tests for HONOR semantic-success/window-title mismatch, Xiaomi result navigation, Apple modern SKU options, and OPPO/vivo/Huawei approved store search.
- [ ] Implement the narrow semantic-to-native-window correlation fallback without weakening approved host/store checks.
- [ ] Verify no-model screenshots include the visible query and complete result grid.
- [ ] Run JD, macOS capture, checkpoint, and Excel integration tests.
- [ ] Commit the JD recovery checkpoint.

### Task 5: Stage diagnostics, full verification, and package

**Files:**
- Modify: `src/quote_app/services/web_run.py`
- Modify: `src/quote_app/tasks/runner.py`
- Modify: execution report tests as required.
- Package with: `packaging/macos/build.sh`

**Interfaces:**
- Produces: stage-specific failure messages and the next numbered macOS acceptance package.

- [ ] Add tests that preserve entry/search/result/detail/configuration/price/capture stage names in the execution report.
- [ ] Run the complete pytest suite, Ruff, Mypy, packaging smoke tests, and macOS signing validation.
- [ ] Build the next numbered `.app` without overwriting the previous accepted package.
- [ ] Record the Git commit and package path for rollback.

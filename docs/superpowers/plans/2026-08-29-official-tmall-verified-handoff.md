# Official and Tmall Verified Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Huawei official, OPPO official no-model evidence, Tmall Huawei/HONOR capture, and Tmall Apple option selection complete from already verified visible state without changing the frozen JD behavior.

**Architecture:** Keep initial site observation responsible for discovering and stabilizing the business result. Hand that verified result to formal capture, whose reader performs only a bounded snapshot comparison of URL, title, selected configuration, locked price, or legal-no search evidence; it must not repeat the full discovery loop. Apple official-store provenance is recorded only when an exact item URL is selected from the approved store's controlled result page.

**Tech Stack:** Python 3.12, synchronous Playwright-compatible page protocol, pytest contract/integration fixtures, Ruff, Mypy, PyInstaller, macOS ad-hoc codesign.

**Spec:** `docs/superpowers/specs/2026-08-29-official-tmall-verified-handoff-design.md`

## Global Constraints

- Start from commit `3b85f91`, whose code baseline remains `45d158e` plus the approved design document.
- Drop the uncommitted `.147` experiment before implementing; do not carry its JD, app-label, or failed threshold changes forward.
- Do not modify `src/quote_app/sites/jd.py` or `tests/contract/test_jd_adapter.py`.
- Do not overwrite `.145` or `.147`; the next package is `.148`.
- Preserve Tmall Xiaomi, OPPO, and vivo behavior and all Excel/task-scheduling behavior.
- Every production change begins with a focused failing test and ends with a focused commit.
- A real URL, title, selected capacity, selected colour, or authoritative price change must still fail closed.

## File Map

- `src/quote_app/sites/official_brands/oppo.py`: OPPO legal-no preparation, bounded verified reader, and cached capture rectangles.
- `src/quote_app/sites/official_brands/huawei.py`: VMALL authoritative price-node selection and bounded capture-state reread.
- `src/quote_app/sites/tmall.py`: Tmall authoritative price evidence, verified handoff, and Apple controlled-result provenance.
- `tests/contract/test_official_oppo_live.py`: OPPO legal-no handoff contract tests.
- `tests/contract/test_official_huawei_live.py`: Huawei price-node and capture-reader contract tests.
- `tests/contract/test_tmall_adapter.py`: Huawei/HONOR price-evidence, Apple provenance, and successful-brand regressions.
- `src/quote_app/app.py`: `.148` user-visible build label only after all behavior tests pass.
- `tests/unit/test_app.py`: exact `.148` label assertion.
- `docs/superpowers/plans/2026-08-29-official-tmall-verified-handoff-build.md`: final verification, package, signature, rollback, and hash record.

---

### Task 1: Remove the failed `.147` experiment and assert the frozen boundary

**Files:**
- Restore to `HEAD`: `src/quote_app/app.py`
- Restore to `HEAD`: `src/quote_app/sites/jd.py`
- Restore to `HEAD`: `src/quote_app/sites/official_brands/huawei.py`
- Restore to `HEAD`: `src/quote_app/sites/official_brands/oppo.py`
- Restore to `HEAD`: `src/quote_app/sites/tmall.py`
- Restore to `HEAD`: `tests/contract/test_jd_adapter.py`
- Restore to `HEAD`: `tests/contract/test_official_huawei_live.py`
- Restore to `HEAD`: `tests/contract/test_official_oppo_live.py`
- Restore to `HEAD`: `tests/contract/test_tmall_adapter.py`
- Restore to `HEAD`: `tests/unit/test_app.py`

**Interfaces:**
- Consumes: approved code baseline `45d158e` and design-only commit `3b85f91`.
- Produces: a clean implementation starting point with no `.147` behavior and a byte-for-byte frozen JD source/test pair.

- [ ] **Step 1: Save the `.147` diff as diagnostic evidence**

Run:

```bash
git diff -- src/quote_app/app.py src/quote_app/sites/jd.py \
  src/quote_app/sites/official_brands/huawei.py \
  src/quote_app/sites/official_brands/oppo.py src/quote_app/sites/tmall.py \
  tests/contract/test_jd_adapter.py \
  tests/contract/test_official_huawei_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_tmall_adapter.py tests/unit/test_app.py
```

Expected: only the known `.147` threshold, seller-alias, OPPO no-model, JD, and label changes appear.

- [ ] **Step 2: Apply the inverse patches with `apply_patch`**

Restore each hunk shown by Step 1 to `git show HEAD:<path>`. Do not run `git reset`, `git checkout --`, or delete user files.

- [ ] **Step 3: Verify the cleanup and frozen JD boundary**

Run:

```bash
git diff --exit-code HEAD -- src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git diff --exit-code HEAD -- src/quote_app/app.py tests/unit/test_app.py
git diff --check
```

Expected: both `git diff --exit-code` commands return zero; `git diff --check` prints nothing.

- [ ] **Step 4: Run the untouched JD contract suite**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_jd_adapter.py
```

Expected: PASS with the same test count as `45d158e`.

---

### Task 2: Give OPPO no-model capture a bounded verified reader

**Files:**
- Modify: `src/quote_app/sites/official_brands/oppo.py`
- Test: `tests/contract/test_official_oppo_live.py`

**Interfaces:**
- Consumes: `VerifiedSemanticState`, `_no_model_capture_proof_locators`, `_preferred_exact_result_link`, and `_prepared_rectangles`.
- Produces: `OppoOfficialAdapter.verified_state_reader(task, page, expected) -> Callable[[], VerifiedSemanticState]`, with a NO_MODEL-specific path and the inherited reader for all other outcomes.

- [ ] **Step 1: Write failing tests for the legal-no handoff**

Add fixture support that can raise if a complete business-state reread occurs after capture preparation, then add:

```python
def test_oppo_no_model_capture_reader_does_not_repeat_full_business_discovery() -> None:
    page = _OppoNoModelPageWithVisibleQuery()
    adapter = _adapter()
    task = _task()
    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    page.reject_business_state_reread = True

    reader = adapter.verified_state_reader(
        task, page, observation.semantic_state
    )

    assert reader() == observation.semantic_state


def test_oppo_no_model_capture_reader_rejects_exact_model_appearing() -> None:
    page = _OppoNoModelPageWithVisibleQuery()
    adapter = _adapter()
    task = _task()
    observation = adapter.observe(task, page)
    adapter.prepare_capture_view(task, page, observation.semantic_state)
    page.exact_result_appeared = True

    with pytest.raises(LayoutRecognitionError, match="exact model appeared"):
        adapter.verified_state_reader(task, page, observation.semantic_state)()
```

- [ ] **Step 2: Run the focused tests and confirm red**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py \
  -k 'no_model_capture_reader'
```

Expected: FAIL because the inherited reader calls `_read_business_state` after preparation.

- [ ] **Step 3: Implement OPPO-only NO_MODEL snapshot verification**

Add `Callable` and implement this shape:

```python
def verified_state_reader(
    self,
    task: WebsiteTask,
    page: BrowserPage,
    expected: VerifiedSemanticState,
) -> Callable[[], VerifiedSemanticState]:
    self._validate_task(task)
    self._validate_expected_state(task, expected)
    if expected.outcome is not BusinessOutcome.NO_MODEL:
        return super().verified_state_reader(task, page, expected)
    browser = _playwright_page(page)
    key = (id(browser), task.task_id, expected.current_sku)
    if key not in self._prepared_rectangles:
        raise LayoutRecognitionError("OPPO no-model capture view is not prepared")

    def read() -> VerifiedSemanticState:
        self.raise_if_manual_action(browser)
        if self.require_approved_url(browser.url) != expected.canonical_url:
            raise LayoutRecognitionError("OPPO no-model capture URL changed")
        self._no_model_capture_proof_locators(task, browser)
        if self._preferred_exact_result_link(browser, task) is not None:
            raise LayoutRecognitionError(
                "OPPO exact model appeared before no-model capture"
            )
        return expected

    return read
```

In `prepare_capture_view`, cache the search keyword/result-region rectangles after bounded proof checks; do not call `_read_business_state` for `BusinessOutcome.NO_MODEL`.

- [ ] **Step 4: Run OPPO tests**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_official_oppo_live.py
```

Expected: PASS, including price-found and other legal-no outcomes that continue using inherited behavior.

- [ ] **Step 5: Commit the OPPO handoff**

```bash
git add src/quote_app/sites/official_brands/oppo.py \
  tests/contract/test_official_oppo_live.py
git commit -m "fix: hand off verified OPPO no-model evidence"
```

---

### Task 3: Bind Huawei capture to one authoritative current-price node

**Files:**
- Modify: `src/quote_app/sites/official_brands/huawei.py`
- Test: `tests/contract/test_official_huawei_live.py`

**Interfaces:**
- Consumes: `_PRICE_CANDIDATE`, `_PRICE_STYLE`, `_price_context_rejected`, `OfficialOfferSnapshot`, and `_require_selected`.
- Produces: `_authoritative_current_price(page) -> tuple[Decimal, Any]` and a Huawei-specific `verified_state_reader` that compares one bounded offer snapshot with the expected state.

- [ ] **Step 1: Write failing tests for dynamic auxiliary price nodes**

Add fixture price nodes that share the detail root but expose an explicit primary-current marker versus changing auxiliary values:

```python
def test_huawei_stabilizes_the_authoritative_price_while_auxiliary_values_change() -> None:
    page = _HuaweiPage(
        primary_price_snapshots=("¥5,499", "¥5,499"),
        auxiliary_price_snapshots=("¥5,299", "¥5,399", "¥5,199"),
    )

    observation = _adapter().observe(_task(), page)

    assert observation.price == Decimal("5499")


def test_huawei_capture_reader_rejects_authoritative_price_change() -> None:
    page = _HuaweiPage()
    adapter = _adapter()
    task = _task()
    observation = adapter.observe(task, page)
    page.primary_price_snapshots = ("¥5,599",)

    with pytest.raises(LayoutRecognitionError, match="price changed"):
        adapter.verified_state_reader(task, page, observation.semantic_state)()
```

Also retain an ambiguity test where two authoritative primary nodes contain different values and must fail closed.

- [ ] **Step 2: Run focused tests and confirm red**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_official_huawei_live.py \
  -k 'authoritative_price or capture_reader_rejects'
```

Expected: FAIL because `_current_price` currently takes the minimum of every approved node and the inherited reader rebuilds the full business state.

- [ ] **Step 3: Implement authoritative price-node resolution**

Refactor price reading so a candidate must be in the primary detail root, not line-through, free of rejected context, and carry the primary current-price role from `_PRICE_STYLE`. Parse exactly one amount per node. Deduplicate identical rendered copies; if distinct authoritative values remain, raise:

```python
if len(amounts) != 1:
    raise LayoutRecognitionError("VMALL authoritative current price is ambiguous")
```

Use two identical reads of the complete `OfficialOfferSnapshot` to finish initial observation. Auxiliary nodes must not participate in this pair.

- [ ] **Step 4: Implement bounded Huawei snapshot reread**

Override `verified_state_reader` for `PRICE_FOUND`. Its `read()` must verify approved URL/detail identity, matching title, unique selected capacity and colour, and one authoritative current price equal to `expected.price`. Return `expected` after these checks; use the inherited reader for legal-no outcomes.

Update `prepare_capture_view` and `capture_rectangles_for_capture` to call the same bounded snapshot helper instead of `_read_business_state` for `PRICE_FOUND`. Keep the four-proof geometry requirement unchanged.

- [ ] **Step 5: Run Huawei focused and pipeline tests**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_official_huawei_live.py \
  tests/integration/test_huawei_official_pipeline.py
```

Expected: PASS; price, title, colour, capacity, URL, and detail-identity drift tests remain fail-closed.

- [ ] **Step 6: Commit the Huawei handoff**

```bash
git add src/quote_app/sites/official_brands/huawei.py \
  tests/contract/test_official_huawei_live.py
git commit -m "fix: bind Huawei capture to authoritative offer"
```

---

### Task 4: Stabilize Tmall Huawei/HONOR on deterministic price evidence

**Files:**
- Modify: `src/quote_app/sites/tmall.py`
- Test: `tests/contract/test_tmall_adapter.py`

**Interfaces:**
- Consumes: `PriceCandidate`, `SellingPriceEvidence`, `filter_valid_prices`, `_visible_configuration_evidence`, and `VerifiedSemanticState`.
- Produces: `_AuthoritativeTmallPrice(amount: Decimal, evidence: SellingPriceEvidence)`, `_authoritative_price_evidence(...)`, and `_snapshot_price_for_capture(...)`.

- [ ] **Step 1: Write failing Huawei/HONOR price-evidence tests**

Extend `_FixturePage` with a `pre_discount_snapshots` sequence that is applied only to `_TMALL_CURRENT_PRE_DISCOUNT_PRICE_VALUES`. Keep `price_snapshots` bound to highlighted current-price nodes. Add this helper with the existing fixture builders:

```python
def _target_tmall_case(brand: str) -> tuple[SiteSpec, WebsiteTask, str, str]:
    if brand == "华为":
        return (
            _huawei_spec(),
            _task(
                brand="华为",
                model_name="华为畅享 90 Pro Max",
                ram="8GB",
                storage="256GB",
                color="白色",
            ),
            _huawei_storage_only_html(),
            "https://huaweistore.tmall.com/?q=华为畅享+90+Pro+Max&type=p&search=y",
        )
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Power2").replace("小米15", "荣耀Power2")
    return (
        _honor_spec(),
        _task(brand="HONOR", model_name="荣耀Power2"),
        html,
        _honor_power2_result_url(),
    )
```

Import `filter_valid_prices` in production only after the red test is confirmed. Add:

```python
@pytest.mark.parametrize("brand", ["华为", "HONOR"])
def test_tmall_target_brand_ignores_changing_auxiliary_price_when_locked_price_is_stable(
    brand: str,
) -> None:
    spec, task, html, result_url = _target_tmall_case(brand)
    page = _FixturePage(
        html=html,
        after_search_url=result_url,
        price_snapshots=(("¥4,299",), ("¥4,199",)),
        pre_discount_snapshots=(("优惠前 ¥4,999",), ("优惠前 ¥4,999",)),
    )

    observation = TmallAdapter(spec).observe(task, cast(Any, page))

    assert observation.price == Decimal("4999")


def test_tmall_target_brand_conflicting_pre_discount_prices_fail_closed() -> None:
    spec, task, html, result_url = _target_tmall_case("华为")
    page = _FixturePage(
        html=html,
        after_search_url=result_url,
        pre_discount_snapshots=(
            ("优惠前 ¥4,999", "优惠前 ¥5,199"),
        ),
    )

    with pytest.raises(LayoutRecognitionError, match="authoritative price"):
        TmallAdapter(spec).observe(task, cast(Any, page))
```

Add a formal-reader test that succeeds with a stable locked price while an auxiliary value changes, and fails when the locked price changes.

- [ ] **Step 2: Run focused tests and confirm red**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py \
  -k 'target_brand_ignores or conflicting_pre_discount or locked_price'
```

Expected: FAIL because the current code recalculates `choose_price(..., HIGHEST)` across all valid values every poll and again during formal capture.

- [ ] **Step 3: Add deterministic evidence selection for Huawei/HONOR only**

Add:

```python
@dataclass(frozen=True, slots=True)
class _AuthoritativeTmallPrice:
    amount: Decimal
    evidence: SellingPriceEvidence
```

For normalized brands `华为` and `荣耀`, filter candidates first. Prefer the unique value whose evidence is `VERIFIED_CURRENT_SKU_PRE_DISCOUNT_PRICE`; if absent, use the unique value from `VERIFIED_CURRENT_SKU_SELLING_NODE`. Identical duplicate nodes collapse to one amount. Distinct values in the chosen evidence class raise `LayoutRecognitionError`.

Normalize catalog brand `HONOR` to target identity `荣耀`. For Xiaomi, OPPO, vivo, and Apple, preserve the existing `choose_price` behavior.

- [ ] **Step 4: Split observation stability from formal-capture snapshot**

During initial target-brand observation, require two matching `_AuthoritativeTmallPrice` reads with unchanged title and selected configuration. During formal capture, perform one `_AuthoritativeTmallPrice` read and compare its amount to `expected.price`; do not call `_stable_visible_price` again for Huawei/HONOR.

Keep the existing formal reader unchanged for all other Tmall brands.

- [ ] **Step 5: Run target and successful-brand regression suites**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py \
  -k 'huawei or honor or xiaomi or oppo or vivo or price'
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py
```

Expected: PASS. Existing Xiaomi, OPPO, and vivo scenarios retain their former expected prices and capture behavior.

- [ ] **Step 6: Commit deterministic Tmall price evidence**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: lock Tmall target brands to authoritative price"
```

---

### Task 5: Let trusted Tmall Apple results reach SKU selection

**Files:**
- Modify: `src/quote_app/sites/tmall.py`
- Test: `tests/contract/test_tmall_adapter.py`

**Interfaces:**
- Consumes: `_exact_product_detail_url`, `_approved_item_url`, `_is_named_store_marker`, and `_tmall_detail_seller_name_matches`.
- Produces: per-adapter `_approved_detail_sources: set[tuple[int, str, str]]` keyed by page identity, task ID, and canonical item URL, plus provenance-aware seller validation.

- [ ] **Step 1: Write failing Apple provenance tests**

Add `set_detail_seller_marker(self, value: str | None) -> None` to `_FixturePage`; it must alter only seller-marker nodes inside the `data-screen="product"` subtree. Add this fixture builder:

```python
def _apple_controlled_page(seller_name: str | None) -> _FixturePage:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "Apple Store官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "apple.tmall.com")
    html = html.replace("小米 15", "Apple iPhone 17").replace("小米15", "iPhone 17")
    html = html.replace("12GB + 256GB", "256GB")
    page = _FixturePage(
        html=html,
        after_search_url="https://apple.tmall.com/?q=iPhone+17&type=p&search=y",
    )
    page.set_detail_seller_marker(seller_name)
    return page
```

Then add:

```python
def test_apple_controlled_official_result_without_detail_seller_marker_selects_sku() -> None:
    page = _apple_controlled_page(None)
    task = _task(
        brand="苹果",
        model_name="iPhone 17",
        ram="8GB",
        storage="256GB",
        color="黑色",
    )

    observation = TmallAdapter(_apple_spec()).observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.option_click_counts == {"color": 1, "capacity": 1}


def test_apple_controlled_result_rejects_explicit_conflicting_detail_seller() -> None:
    page = _apple_controlled_page("其他数码专营店")
    task = _task(
        brand="苹果",
        model_name="iPhone 17",
        ram="8GB",
        storage="256GB",
        color="黑色",
    )

    with pytest.raises(LayoutRecognitionError, match="approved store"):
        TmallAdapter(_apple_spec()).observe(task, cast(Any, page))


def test_apple_direct_detail_without_seller_or_provenance_still_fails() -> None:
    page = _apple_controlled_page(None)
    page._url = "https://detail.tmall.com/item.htm?id=974619066443"
    page.activate("product")
    task = _task(
        brand="苹果",
        model_name="iPhone 17",
        ram="8GB",
        storage="256GB",
        color="黑色",
    )

    with pytest.raises(LayoutRecognitionError, match="approved store"):
        TmallAdapter(_apple_spec())._observe_detail(
            task,
            cast(Any, page),
            page.url,
        )
```

- [ ] **Step 2: Run Apple tests and confirm red**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py \
  -k 'apple_controlled_official_result or apple_direct_detail_without'
```

Expected: the first test FAILS before any option click; the conflicting/direct-detail tests remain fail-closed.

- [ ] **Step 3: Record only controlled official-result provenance**

Initialize the set in `TmallAdapter.__init__`. Immediately after `_exact_product_detail_url` returns from cards on an approved store result page, add:

```python
self._approved_detail_sources.add(
    (id(browser_page), task.task_id, _approved_item_url(detail_url, base_url=detail_url))
)
```

Do not add provenance for direct detail entry, checkpoint recovery, arbitrary current tabs, or a URL that was not selected from the current approved result region.

- [ ] **Step 4: Make seller validation provenance-aware**

Pass `task` and canonical item URL into `_require_approved_detail_seller`. Accept when at least one visible marker matches the approved seller. Reject any visible named marker that explicitly conflicts. Only for Apple, accept zero/non-named markers when the exact `(page, task, item URL)` provenance key exists.

Call the updated validator from both `_wait_for_detail_layout` and `verified_state_reader`. Once this passes, continue through the existing colour-then-capacity selection path without any Apple-specific click shortcut.

- [ ] **Step 5: Run full Tmall contracts**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_tmall_adapter.py
```

Expected: PASS, including seller-spoof, URL-change, direct-detail, option-selection, price, and capture tests.

- [ ] **Step 6: Commit Apple provenance**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: preserve trusted Tmall Apple result provenance"
```

---

### Task 6: Freeze regressions, label `.148`, and build the signed app

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `docs/superpowers/plans/2026-08-29-official-tmall-verified-handoff-build.md`
- Generate: `build-official-tmall-verified-handoff-148/`
- Generate: `dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: all prior focused commits and `packaging/quotation_app.spec`.
- Produces: user-visible `.148` package, strict signature verification, SHA-256, test record, and rollback reference.

- [ ] **Step 1: Assert JD remains frozen before changing the label**

Run:

```bash
git diff --exit-code 45d158e -- src/quote_app/sites/jd.py \
  tests/contract/test_jd_adapter.py
.venv/bin/pytest -q tests/contract/test_jd_adapter.py
```

Expected: zero source diff and all JD contract tests pass.

- [ ] **Step 2: Update the exact build label and its test**

Change only the `.147`/`.145` display suffix required by the current baseline to `.148` in `src/quote_app/app.py`, and update the exact assertion in `tests/unit/test_app.py`. Do not rename product files or change run-scope behavior.

- [ ] **Step 3: Run the focused regression matrix**

Run:

```bash
.venv/bin/pytest -q \
  tests/contract/test_official_huawei_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_tmall_adapter.py \
  tests/contract/test_jd_adapter.py \
  tests/integration/test_huawei_official_pipeline.py \
  tests/integration/test_oppo_official_pipeline.py \
  tests/unit/test_runner_registry_observation.py \
  tests/unit/test_macos_capture_runtime.py \
  tests/unit/test_app.py
```

Expected: PASS.

- [ ] **Step 4: Run complete engineering verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

Expected: all tests pass; Ruff prints `All checks passed!`; Mypy reports no issues.

- [ ] **Step 5: Build without overwriting earlier packages**

Run:

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-148 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-official-tmall-verified-handoff-148 \
  --workpath build-official-tmall-verified-handoff-148 \
  packaging/quotation_app.spec
codesign --force --deep --sign - \
  'dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app'
codesign --verify --deep --strict --verbose=2 \
  'dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app'
.venv/bin/pytest -q tests/smoke/test_packaging_spec.py
```

Expected: PyInstaller succeeds, strict codesign reports a valid app, and packaging smoke tests pass.

- [ ] **Step 6: Record evidence and hash**

Write the build record with exact commands, test counts, commit IDs, package path, rollback checkpoint `45d158e`, design checkpoint `3b85f91`, and:

```bash
shasum -a 256 \
  'dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app/Contents/MacOS/福建移动铺货报价助手'
```

- [ ] **Step 7: Commit release metadata**

```bash
git add src/quote_app/app.py tests/unit/test_app.py \
  docs/superpowers/plans/2026-08-29-official-tmall-verified-handoff-build.md
git commit -m "build: package official Tmall handoff 148"
```

- [ ] **Step 8: Final diff audit**

Run:

```bash
git status --short
git diff --exit-code 45d158e -- src/quote_app/sites/jd.py \
  tests/contract/test_jd_adapter.py
git log --oneline --decorate -8
```

Expected: only intentionally untracked build outputs may remain; JD diff is empty; the log contains the design and focused implementation commits.

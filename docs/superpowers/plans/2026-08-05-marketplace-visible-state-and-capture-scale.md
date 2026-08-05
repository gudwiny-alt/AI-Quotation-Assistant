# Marketplace Visible State and Capture Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Honor Power2 succeed on JD and Tmall from visible model/capacity/color/price evidence, and make the JD 畅玩80 no-model screenshot prove an actual 80% capture view containing both the search keyword and result product names.

**Architecture:** Keep the official-site and Excel pipelines untouched. Replace marketplace-only hidden-SKU/exclusive-flat-option assumptions with stable visible configuration snapshots, then strengthen the shared capture-scale helper so the adapters prove 80% twice before positioning. Keep observation, capture preparation, and capture restoration separate so a screenshot retry never repeats a search.

**Tech Stack:** Python 3.12, synchronous Playwright adapter surface, pytest contract/unit/integration tests, existing macOS native evidence runtime, PyInstaller app packaging.

## Global Constraints

- Do not modify `src/quote_app/sites/official.py` or Honor official override behavior.
- JD and Tmall must require an exact model, one exact selected capacity, one exact selected colour, and two identical visible-state price samples.
- Package, service, warranty, and official-standard controls are not colour controls.
- Stock availability does not prevent selecting a visible capacity/colour for quotation evidence.
- The 80% scale is used only during final judgement and formal capture.
- The scale must remain 80% until the screenshot file has been validated and published; it must be restored after success or final failure.
- A capture-preparation retry must not resubmit search or reopen a detail page.
- Preserve a successfully saved price or legal-no result when only its screenshot fails.

---

## File Structure

- Modify `src/quote_app/sites/jd.py`: JD visible configuration validation and no-model capture preparation.
- Modify `src/quote_app/sites/tmall.py`: Tmall visible configuration identity and stable visible price sampling.
- Modify `src/quote_app/sites/detail_capture_view.py`: verified 80% scale proof used by both marketplace adapters.
- Modify `src/quote_app/evidence/macos_runtime.py`: preserve the failing capture-preparation stage in the final error while retaining existing cleanup.
- Modify `tests/contract/test_jd_adapter.py`: real Power2 extra-selected-package and JD no-model scale/retry contracts.
- Modify `tests/contract/test_tmall_adapter.py`: modern Tmall visible configuration without hidden SKU attributes.
- Modify `tests/unit/test_detail_capture_view.py`: two-sample scale verification and restoration timing.
- Modify `tests/unit/test_macos_capture_runtime.py`: preparation failure detail and restoration boundary.
- Verify `tests/integration/test_web_to_excel.py`: partial-channel output continues to preserve completed channels.

---

### Task 1: JD Power2 visible configuration and struck-through price

**Files:**
- Modify: `tests/contract/test_jd_adapter.py:1480-1605`
- Modify: `src/quote_app/sites/jd.py:1596-1645`
- Test: `tests/contract/test_jd_adapter.py`

**Interfaces:**
- Consumes: `_modern_configuration_snapshot(page) -> tuple[tuple[str, bool], ...]`, `capacity_matches(...)`, `_jd_color_matches(...)`.
- Produces: `_require_exact_modern_configuration(task, snapshot) -> None` that validates only matching capacity and colour selections and ignores unrelated selected controls.

- [ ] **Step 1: Add a failing Power2 contract with an extra selected package**

Extend `_honor_power2_modern_html()` with one selected non-capacity control:

```python
html = html.replace(
    '      </section>\n'
    '      <div class="product-price-panel">',
    '        <div class="specification-item-sku '
    'specification-item-sku--selected">官方标配</div>\n'
    '      </section>\n'
    '      <div class="product-price-panel">',
)
```

Add a test that observes the page with the target `12GB+256GB` and `幻夜黑`, then asserts:

```python
assert observation.outcome is BusinessOutcome.PRICE_FOUND
assert observation.price == Decimal("2999")
```

- [ ] **Step 2: Run the new test and verify the current flat classification fails**

Run:

```bash
.venv/bin/pytest tests/contract/test_jd_adapter.py::test_modern_power2_ignores_selected_official_package_when_binding_visible_configuration -q
```

Expected: FAIL with `JD modern exact configuration did not reach an exclusive stable state`.

- [ ] **Step 3: Add ambiguity guards before implementation**

Add separate tests showing that two selected matching capacity controls or two selected matching colour controls still fail with `LayoutRecognitionError`. These tests prevent the fix from becoming “accept any selected option.”

- [ ] **Step 4: Implement matching-selection validation**

Replace the current “every non-capacity selection is a colour” rule with:

```python
selected_capacities = tuple(
    label for label, selected in snapshot
    if selected and capacity_matches(label, task.ram, task.storage)
)
selected_colours = tuple(
    label for label, selected in snapshot
    if selected and _jd_color_matches(task.color, label)
)
if len(selected_capacities) != 1 or len(selected_colours) != 1:
    raise LayoutRecognitionError(
        "JD modern selected target configuration is missing or ambiguous"
    )
```

Keep the two-identical-atomic-snapshot and transition checks in `_modern_selected_price()` unchanged.

- [ ] **Step 5: Run JD focused regression tests**

Run:

```bash
.venv/bin/pytest tests/contract/test_jd_adapter.py -q
```

Expected: all JD adapter tests pass, including the existing stale-price and transient-selection rejection tests.

- [ ] **Step 6: Commit the JD change**

```bash
git add src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git commit -m "fix: validate JD Power2 from visible target options"
```

---

### Task 2: Tmall visible configuration identity without hidden SKU attributes

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py:816-875, 2160-2265`
- Modify: `src/quote_app/sites/tmall.py:359-462, 1209-1450`
- Test: `tests/contract/test_tmall_adapter.py`

**Interfaces:**
- Produces: `_selected_configuration_snapshot(page, task) -> tuple[str, str]`, returning normalized visible `(capacity_label, colour_label)`.
- Produces: `_stable_visible_price(page, task, configuration) -> tuple[Decimal, TmallStockSample]`.
- Consumes: `_unique_selected_option(...)`, `_matching_detail_titles(...)`, `_price_candidates(...)`, `choose_price(...)`.

- [ ] **Step 1: Add a failing Tmall Power2 fixture without hidden SKU binding attributes**

Build the existing promotional Power2 detail fixture, remove `data-sku`, `data-context-sku`, and `data-current-sku` from the capacity, colour, price, stock, and marker nodes, while retaining the visible selected classes and text.

Use an explicit fixture helper:

```python
def _without_hidden_sku_bindings(html: str) -> str:
    return re.sub(
        r'\sdata-(?:sku|context-sku|current-sku)="[^"]*"',
        "",
        html,
    )
```

Assert:

```python
observation = adapter.observe(task, cast(Any, page))
assert observation.outcome is BusinessOutcome.PRICE_FOUND
assert observation.price is not None
```

- [ ] **Step 2: Run the new test and verify the hidden binding failure**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py::test_honor_power2_uses_stable_visible_configuration_without_hidden_sku_attributes -q
```

Expected: FAIL with `Tmall selected capacity SKU binding is missing or invalid`.

- [ ] **Step 3: Add visible-state instability tests**

Add one test where the selected capacity changes between the two price samples and one where the selected colour becomes ambiguous. Both must fail rather than returning a price.

- [ ] **Step 4: Implement visible configuration snapshots**

Add:

```python
def _selected_configuration_snapshot(
    self, page: Any, task: WebsiteTask
) -> tuple[str, str]:
    capacity = self._unique_selected_option(
        self._sku_option_group(page, "存储容量"),
        TMALL_SKU_VALUES,
        lambda label: capacity_matches(label, task.ram, task.storage),
        semantic_name="capacity",
    )
    colour = self._unique_selected_option(
        self._sku_option_group(page, "机身颜色"),
        TMALL_SKU_VALUES,
        lambda label: _tmall_color_matches(task.color, label),
        semantic_name="color",
    )
    return (
        normalize_product_text(capacity.inner_text()),
        normalize_product_text(colour.inner_text()),
    )
```

Use this tuple as the selected identity for price polling and final revalidation. Generate the semantic-state `current_sku` from a deterministic visible identity such as `visible:<capacity>|<colour>`; do not require hidden numeric IDs.

- [ ] **Step 5: Make stock and price sampling visible-state based**

Read the single visible delivery region and stock text without requiring their `data-sku`. Read visible price candidates without requiring a numeric price-node SKU, continue excluding subsidy-marked prices, and require the same configuration, stock sample, and candidate tuple in two consecutive polls before returning.

The stock reader becomes:

```python
def _visible_stock_sample(self, page: Any) -> TmallStockSample:
    state = _unique_visible_locator(
        page, TMALL_STOCK_STATES, semantic_name="stock state"
    )
    region = _unique_visible_locator(
        page, TMALL_DELIVERY_REGIONS, semantic_name="delivery region"
    )
    return TmallStockSample(
        region=_normalized_region(region.inner_text()),
        state=normalize_product_text(state.inner_text()),
    )
```

The polling atom is exactly:

```python
snapshot = (
    self._selected_configuration_snapshot(page, task),
    self._visible_stock_sample(page),
    self._price_candidates(page),
)
```

Return only when the same atom appears twice consecutively and `choose_price()` returns a value.

- [ ] **Step 6: Preserve legal-no behavior separately**

Do not change search-page `NO_MODEL` detection. For unavailable capacity/colour detail states, validate the visible disabled state and exact option label without hidden SKU equality.

- [ ] **Step 7: Run Tmall focused regression tests**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py -q
```

Expected: all Tmall adapter tests pass, including login/risk-control, wrong-model, ambiguous-title, and price-change rejection tests.

- [ ] **Step 8: Commit the Tmall change**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: bind Tmall quotes to stable visible configuration"
```

---

### Task 3: Prove and preserve 80% capture scale for JD no-model evidence

**Files:**
- Modify: `tests/unit/test_detail_capture_view.py:1-220`
- Modify: `tests/contract/test_jd_adapter.py:2309-2430`
- Modify: `tests/unit/test_macos_capture_runtime.py:2440-2520`
- Modify: `src/quote_app/sites/detail_capture_view.py:1-90`
- Modify: `src/quote_app/sites/jd.py:897-1085`
- Modify: `src/quote_app/evidence/macos_runtime.py:263-440, 440-490`
- Test: the four files above.

**Interfaces:**
- Produces: `CaptureScaleProof(inline_zoom: float, computed_zoom: float, sample_count: int)`.
- Changes: `apply_capture_scale(page, *, scale: float) -> CaptureScaleProof`.
- Preserves: `restore_capture_scale(page) -> None` and the runtime-owned restoration after evidence capture.

- [ ] **Step 1: Add failing two-sample scale-proof tests**

Update the detail-capture fake page so scale application can report separate inline and computed values. Add tests asserting that `apply_capture_scale(page, scale=0.8)` rejects:

```python
[(0.8, 1.0), (0.8, 1.0)]
```

and accepts:

```python
[(0.8, 0.8), (0.8, 0.8)]
```

The accepted proof must expose `sample_count == 2`.

- [ ] **Step 2: Verify the new scale tests fail**

Run:

```bash
.venv/bin/pytest tests/unit/test_detail_capture_view.py -q
```

Expected: the new computed-scale and two-sample tests fail because the current helper checks only one inline style assignment.

- [ ] **Step 3: Implement the scale proof**

Add the frozen `CaptureScaleProof` dataclass. Make the browser script return both `document.documentElement.style.zoom` and `getComputedStyle(document.documentElement).zoom`; sample twice with the existing layout wait between samples. Raise `LayoutRecognitionError("capture scale 0.8 did not become visually stable")` unless both samples match the requested scale.

- [ ] **Step 4: Add a failing JD no-model preparation retry contract**

Extend the JD fixture page to fail the first post-scale result-card positioning sample and succeed on the second. Assert that:

```python
adapter.prepare_capture_view(task, cast(Any, page), state)
assert page.search_submit_count == 0
assert page.capture_scales == [0.8, 0.8]
```

and that the final rectangles contain roles `("search_keyword", "result_region")`.

- [ ] **Step 5: Implement one bounded preparation retry**

In JD `prepare_capture_view`, attempt only `_prepare_capture_view_at_scale()` twice. Between attempts restore the scale, wait 500 ms, and reapply/prove 0.8. Do not call `observe()`, `goto()`, search fill, or search submit. On the second failure, re-raise the exact layout message.

- [ ] **Step 6: Verify restoration occurs after capture, not context construction**

Add a macOS runtime test whose fake adapter records `prepare`, `reader`, `capture`, `restore`. The required order is:

```python
["prepare", "reader", "capture", "restore"]
```

Also assert final-failure order ends with exactly one `restore`.

- [ ] **Step 7: Preserve precise preparation failure details**

When capture-view preparation or rectangle rereading raises a `LayoutRecognitionError`, convert it to a retryable capture geometry error whose safe message includes the failing stage (`缩放验证`, `搜索框定位`, or `结果区域定位`). Do not classify unrelated permission, foreground, or filesystem errors as retryable.

- [ ] **Step 8: Run focused capture tests**

Run:

```bash
.venv/bin/pytest tests/unit/test_detail_capture_view.py tests/contract/test_jd_adapter.py tests/unit/test_macos_capture_runtime.py tests/unit/test_evidence_quality.py -q
```

Expected: all focused tests pass.

- [ ] **Step 9: Commit the capture change**

```bash
git add src/quote_app/sites/detail_capture_view.py src/quote_app/sites/jd.py src/quote_app/evidence/macos_runtime.py tests/unit/test_detail_capture_view.py tests/contract/test_jd_adapter.py tests/unit/test_macos_capture_runtime.py tests/unit/test_evidence_quality.py
git commit -m "fix: prove JD no-model capture scale before screenshot"
```

---

### Task 4: End-to-end regression, package, and handoff

**Files:**
- Verify: `tests/integration/test_web_to_excel.py`
- Verify: `tests/regression/test_honor_official_baseline.py`
- Verify: `pyproject.toml`
- Rebuild: `dist-marketplace-visible-state/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: completed JD, Tmall, and capture-scale changes.
- Produces: one signed Mac test bundle and a concise test matrix for the two Honor rows.

- [ ] **Step 1: Run marketplace and Excel integration tests**

```bash
.venv/bin/pytest tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/integration/test_web_to_excel.py tests/regression/test_honor_official_baseline.py -q
```

Expected: all pass; no official-site regression and partial screenshot failure still preserves an existing price/legal-no cell.

- [ ] **Step 2: Run the complete suite and lint**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

Expected: full suite passes and Ruff reports no errors.

- [ ] **Step 3: Build a clean Mac application bundle**

Run the clean build into new directories so the last tested `.42` bundle is not overwritten:

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-marketplace-visible-state \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-marketplace-visible-state \
  --workpath build-marketplace-visible-state \
  packaging/quotation_app.spec
codesign --force --deep --sign - \
  'dist-marketplace-visible-state/福建移动铺货报价助手.app'
```

Expected: PyInstaller exits 0 and produces only the new bundle in `dist-marketplace-visible-state`.

- [ ] **Step 4: Verify bundle identity and signature**

```bash
codesign --verify --deep --strict --verbose=2 'dist-marketplace-visible-state/福建移动铺货报价助手.app'
```

Expected: signature verification succeeds. Compare the packaged JD/Tmall modules to the fresh source build so the delivered app cannot contain stale adapter code.

- [ ] **Step 5: Commit build metadata only if source-controlled metadata changed**

```bash
git status --short
```

Do not commit generated `build-*` or `dist-*` directories.

- [ ] **Step 6: User acceptance matrix**

Ask the user to run the two-Honor input and verify exactly:

- Power2: JD price `2999` plus JD screenshot; Tmall price plus Tmall screenshot; official result unchanged.
- 畅玩80: JD `无` plus screenshot containing the search keyword and visible nonmatching result names; Tmall and official results unchanged.
- Browser visibly changes to 80% only for final judgment/capture, then restores after each evidence file is generated.

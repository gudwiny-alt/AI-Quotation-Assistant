# Huawei Official Live Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a brand-isolated Huawei VMALL adapter that searches exact Huawei phone models, selects the required capacity and color, writes the lowest valid current official price to `AK`, captures the four required proofs into `AN`, and preserves frozen site behavior.

**Architecture:** Add `HuaweiOfficialAdapter` as an independent `LiveOfficialAdapterBase` implementation and register only the canonical brand `华为`. Model VMALL search and both supported numeric product URL families with a dedicated contract harness; reuse only the existing mechanical checkpoint, formal-capture, scale, and scheduler boundaries. Keep all Huawei selectors, price semantics, storage fallback, and capture geometry in `huawei.py`, then exercise the real runner-to-checkpoint-to-Excel path in a separate integration suite.

**Tech Stack:** Python 3.12, Playwright-compatible `BrowserPage` protocol, pytest, SQLite task checkpoints, Pillow test evidence, openpyxl pipeline verification, Ruff, mypy, PyInstaller packaging.

## Global Constraints

- Use `https://www.vmall.com/` as the official entry.
- Accept only approved VMALL HTTPS hosts and the numeric detail routes `/product/<digits>.html` or `/product/comdetail/index.html?...prdId=<digits>`.
- Match the exact base model; reject accessories and different Pro/Pro+/Plus/Ultra/Max/青春版/优享版 variants.
- Sold-out or arrival-notification state does not block detail navigation, configuration selection, price extraction, or capture.
- Prefer exact `RAM+storage`; fall back to exact storage only when the page offers storage-only configuration.
- Select the exact target color after capacity.
- Use the lowest valid current price for the selected configuration; reject reference prices, coupons, subsidies, trade-in, installments, insurance, services, accessories, recommendations, and prices for other configurations.
- Normal capture has exactly four web-business proofs: exact product title, adopted price, target capacity, and target color.
- If all four proofs already fit and are unobscured, capture immediately. Otherwise ensure 80% idempotently, then perform at most one geometry-derived scroll of at most 160 CSS pixels.
- Persist price and detail URL before formal capture. Capture failure must not clear `AK`.
- Process all Huawei input rows in source order, saving each row before continuing.
- Do not change frozen HONOR, Xiaomi, OPPO, vivo, JD, Tmall, Excel mapping, or their accepted behavior.
- Do not package over an earlier acceptance build; use a new Huawei-specific output directory.

---

## File Structure

- Create `src/quote_app/sites/official_brands/huawei.py`: VMALL-only navigation, matching, configuration, price, checkpoint recovery, and capture preparation.
- Modify `src/quote_app/sites/official_brands/factory.py`: replace only the Huawei placeholder mapping with the live adapter.
- Modify `src/quote_app/sites/official_brands/__init__.py`: export the live Huawei class from its own module.
- Create `tests/fixtures/sites/official_live/huawei/search_results.html`: sanitized VMALL search-result structure including exact, derived, accessory, invalid-link, and sold-out cards.
- Create `tests/fixtures/sites/official_live/huawei/detail_normal.html`: sanitized current VMALL purchase summary with complete RAM+storage, colors, current/reference/promotional prices, and capture rectangles.
- Create `tests/fixtures/sites/official_live/huawei/detail_storage_only.html`: detail variant whose only capacity dimension is storage.
- Create `tests/fixtures/sites/official_live/huawei/detail_missing_capacity.html`: complete loaded version group without the target.
- Create `tests/fixtures/sites/official_live/huawei/detail_missing_color.html`: complete loaded color group without the target.
- Create `tests/contract/test_official_huawei_live.py`: deterministic true-structure contract and slow-network/capture harness.
- Create `tests/integration/test_huawei_official_pipeline.py`: real registry → runner → SQLite checkpoint → formal capture request → full pipeline → `AK`/`AN` and report tests.
- Modify `tests/unit/test_official_brand_factory.py`: assert Huawei resolves to the new class.
- Modify `tests/regression/test_official_brand_isolation.py`: freeze Huawei dispatch without changing other official/JD/Tmall classes.
- Modify `tests/regression/test_official_capture_reader_isolation.py` only if the existing generic live-official assertion does not already cover Huawei; do not alter reader behavior.
- Modify `README.md`, `docs/testing/live-site-matrix.md`, and `docs/testing/macos-evidence-smoke.md`: record Huawei acceptance scope and manual test sequence after code review passes.
- Modify `packaging/quotation_app.spec` only if package discovery does not automatically include `official_brands.huawei`; add one precise hidden import in that case.

---

### Task 1: Define the VMALL True-Structure Contract

**Files:**
- Create: `tests/fixtures/sites/official_live/huawei/search_results.html`
- Create: `tests/fixtures/sites/official_live/huawei/detail_normal.html`
- Create: `tests/fixtures/sites/official_live/huawei/detail_storage_only.html`
- Create: `tests/fixtures/sites/official_live/huawei/detail_missing_capacity.html`
- Create: `tests/fixtures/sites/official_live/huawei/detail_missing_color.html`
- Create: `tests/contract/test_official_huawei_live.py`
- Create: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-1-report.md`

**Interfaces:**
- Consumes: `WebsiteTask`, `WebsiteObservationCheckpoint`, `BusinessOutcome`, `_OfficialDocumentParser`, `_OfficialFixturePage`, `_OfficialLocator`, and the approved VMALL structures documented in the design.
- Produces: `_HuaweiPage`, `_HuaweiLocator`, `_set_huawei_cards(page, ...)`, formal selector specifications for title/price/capacity/color, and RED contract tests imported by Task 3.

- [ ] **Step 1: Create separated home, results, detail, and blank DOM states**

Model the current VMALL detail hierarchy with distinct nodes for the purchase title, current-price candidates, reference price, version group, color group, and selected states. Keep results invisible until a real homepage search input receives `fill(model)` and `press("Enter")`.

```python
class _HuaweiPage(_OfficialFixturePage):
    def __init__(self, detail: str = "detail_normal.html") -> None:
        super().__init__((FIXTURES / "search_results.html").read_text(), entry_url=ENTRY)
        self.home_root = self._parse(
            "<html><body><input id='search-kw' value=''></body></html>"
        )
        self.results_root = self.root
        self.detail_root = self._parse((FIXTURES / detail).read_text())
        self.blank_root = self._parse("<html><body></body></html>")
        self.active = "blank"
        self.waits = 0
        self.capture_scale = 1.0
        self.scroll_offset = 0.0
```

- [ ] **Step 2: Write RED tests for search, exact-card selection, and approved URLs**

Cover actual search submission, results arriving at tick 39, an early empty signal followed by a late exact card, full 40-tick no-model completion, sold-out exact cards, first invalid exact URL followed by a valid exact URL, all invalid URLs, derived models, accessories, and both numeric detail routes.

```python
@pytest.mark.parametrize(
    "detail_url",
    [
        "https://www.vmall.com/product/10086259366534.html",
        "https://www.vmall.com/product/comdetail/index.html?prdId=10086259366534",
    ],
)
def test_huawei_accepts_only_numeric_vmall_detail_routes(detail_url: str) -> None:
    adapter = _adapter()
    page = _HuaweiPage()
    _set_huawei_cards(page, exact_href=detail_url)
    observed = adapter.observe(_task(), page)
    assert observed.checkpoint.detail_url == detail_url
```

- [ ] **Step 3: Write RED tests for RAM+storage, storage-only fallback, and legal no**

Require full `8GB+256GB` when the detail exposes full versions; allow `256GB` only in the storage-only fixture. Add target options arriving at tick 19, complete 20-tick missing capacity/color, preselected options that are not clicked again, and sold-out text that does not convert a selectable option into legal no.

```python
def test_huawei_never_uses_storage_only_when_full_versions_exist() -> None:
    task = _task(ram="8GB", storage="256GB")
    page = _HuaweiPage("detail_normal.html")
    page.remove_full_version("8GB+256GB")
    observed = _adapter().resume(task, page, _detail_checkpoint(task))
    assert observed.semantic_state.outcome is BusinessOutcome.CAPACITY_UNAVAILABLE
```

- [ ] **Step 4: Write RED tests for lowest valid current price and stability**

Use a purchase summary containing current prices `4999` and `5199`, reference `6499`, subsidy `750`, installment `208.29`, trade-in `1000`, and service `699`; expect `4999`. Cover current price appearing late inside the 5-second budget, three seconds of continuous stability, ambiguity/configuration/URL drift, and a current-price node containing more than one valid number.

```python
def test_huawei_uses_lowest_current_price_and_rejects_promotional_numbers() -> None:
    observed = _adapter().observe(_task(), _HuaweiPage())
    assert observed.semantic_state.price == Decimal("4999")
```

- [ ] **Step 5: Write RED tests for four-proof capture geometry**

The adapter must pass four real selectors/roles to `evaluate`; the harness must resolve those selectors inside the active detail DOM, verify the adopted price and uniquely selected options, and derive rectangles from those exact nodes. Cover immediate 100% fit, idempotent 80%, upward/downward movement, a fixed overlay over any proof, a persistent overlay, missing geometry, one-scroll maximum, and a 160-pixel bound.

```python
def test_huawei_capture_uses_only_four_business_proofs() -> None:
    task, page, expected = _price_found_case()
    adapter = _adapter()
    adapter.prepare_capture_view(task, page, expected)
    rects = adapter.capture_rectangles_for_capture(task, page, expected)
    assert [rect.role for rect in rects] == ["title", "price", "capacity", "color"]
```

- [ ] **Step 6: Run the contract and verify RED for the missing module**

Run:

```bash
.venv/bin/pytest -q tests/contract/test_official_huawei_live.py
.venv/bin/ruff check tests/contract/test_official_huawei_live.py
```

Expected: fixture self-checks pass; production behavior tests fail only with `ModuleNotFoundError: quote_app.sites.official_brands.huawei`; Ruff passes.

- [ ] **Step 7: Review and commit the contract**

Confirm the fixture does not invent `official-huawei-*` classes and that blank/home/results/detail nodes cannot leak across states.

```bash
git add tests/fixtures/sites/official_live/huawei tests/contract/test_official_huawei_live.py .superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-1-report.md
git commit -m "test: define Huawei official live contract"
```

---

### Task 2: Implement the Independent Huawei Adapter

**Files:**
- Create: `src/quote_app/sites/official_brands/huawei.py`
- Modify: `src/quote_app/sites/official_brands/factory.py`
- Modify: `src/quote_app/sites/official_brands/__init__.py`
- Modify: `tests/unit/test_official_brand_factory.py`
- Modify: `tests/regression/test_official_brand_isolation.py`
- Create: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-2-report.md`

**Interfaces:**
- Consumes: the Task 1 contract; `LiveOfficialAdapterBase`; `OfficialBusinessState`; `OfficialDetailIdentity`; `OfficialOfferSnapshot`; `CssRect`; `ensure_capture_scale(page, 0.8)` and `restore_capture_scale(page)`.
- Produces: `HuaweiOfficialAdapter(spec: SiteSpec)` implementing `observe`, `resume`, `verified_state_reader`, `prepare_capture_view`, `restore_capture_view`, and `capture_rectangles_for_capture` through the base protocol.

- [ ] **Step 1: Add Huawei validation and approved URL helpers**

```python
_ENTRY = "https://www.vmall.com/"
_HOSTS = (ApprovedHostFamily("www.vmall.com"),)

class HuaweiOfficialAdapter(LiveOfficialAdapterBase):
    approved_host_families = _HOSTS

    def _validate_task(self, task: WebsiteTask) -> None:
        if task.brand != "华为" or task.channel is not WebsiteChannel.OFFICIAL:
            raise ValueError("Huawei adapter only accepts 华为 OFFICIAL tasks")
```

Implement `_is_search_url`, `_is_detail_url`, and `_detail_identity` with parsed hosts/paths and numeric IDs. Validate checkpoint routes before any resume navigation.

- [ ] **Step 2: Implement complete-window search and exact model matching**

Use 40 ticks at 250 ms. Never use fixture-only classes or stop on an early empty signal. Normalize `华为` and `HUAWEI` prefixes, then reject accessory markers across the entire suffix and reject derived variants not present in the target.

```python
for _tick in range(40):
    exact = _first_approved_exact_card(page, task.model_name)
    if exact is not None:
        exact.click()
        return self._observe_detail(task, page)
    page.wait_for_timeout(250)
return self.build_observation(task, self._no_model_state(task, page))
```

- [ ] **Step 3: Implement capacity strategy and color selection**

Identify whether the active configuration group is full-version or storage-only. Wait 20 ticks at 250 ms for each group/target. Select only an exact enabled option, do not re-click a unique selected target, and keep product-level sold-out state outside availability decisions.

```python
def _target_capacity(task: WebsiteTask, *, storage_only: bool) -> str:
    return normalize_capacity(task.storage) if storage_only else normalize_capacity(
        f"{task.ram}+{task.storage}"
    )
```

- [ ] **Step 4: Implement lowest-current-price selection and stability**

Read current price candidates only from the active purchase summary and exclude reference/promotional/service contexts. Retry `_PriceUnavailable` inside one five-second budget; once a valid price exists, require the same `OfficialOfferSnapshot` for three seconds using lightweight immediate configuration rechecks.

```python
valid = tuple(
    value
    for candidate in _current_price_nodes(page)
    for value in _money_values(candidate.inner_text())
    if _is_valid_current_price_node(candidate)
)
if not valid:
    raise _PriceUnavailable("Huawei current price is unavailable")
return min(valid)
```

- [ ] **Step 5: Implement legal-no states and capture views**

Build no-model capture from the search query and concrete results container; no-capacity/no-color capture from product identity and the complete target option group. Normal capture must use the four selector specifications from Task 1. First test fit at current scale, ensure 0.8 only if needed, then calculate one signed scroll from real viewport/occlusion geometry and clamp its magnitude to 160.

```python
delta = _bounded_scroll_delta(geometry, max_abs=160.0)
if delta is not None:
    page.evaluate("window.scrollBy(0, arguments[0])", delta)
if not _proofs_fit(page, selectors):
    raise LayoutRecognitionError("Huawei four-proof capture view cannot be established")
```

- [ ] **Step 6: Replace only the Huawei factory placeholder**

```python
from quote_app.sites.official_brands.huawei import HuaweiOfficialAdapter

_BRAND_ADAPTER_FACTORIES = {
    "小米": XiaomiOfficialAdapter,
    "欧珀": OppoOfficialAdapter,
    "维沃": VivoOfficialAdapter,
    "华为": HuaweiOfficialAdapter,
    "苹果": AppleOfficialAdapter,
}
```

Do not change the class or mapping for any other brand.

- [ ] **Step 7: Run Huawei and frozen-site tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/contract/test_official_huawei_live.py \
  tests/unit/test_official_brand_factory.py \
  tests/regression/test_official_brand_isolation.py
.venv/bin/pytest -q \
  tests/contract/test_official_honor_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_vivo_live.py
.venv/bin/ruff check src/quote_app/sites/official_brands/huawei.py tests/contract/test_official_huawei_live.py
.venv/bin/mypy src/quote_app/sites/official_brands/huawei.py src/quote_app/sites/official_brands/factory.py
```

Expected: all selected tests, Ruff, and mypy pass.

- [ ] **Step 8: Review and commit the adapter**

```bash
git add src/quote_app/sites/official_brands/huawei.py src/quote_app/sites/official_brands/factory.py src/quote_app/sites/official_brands/__init__.py tests/unit/test_official_brand_factory.py tests/regression/test_official_brand_isolation.py .superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-2-report.md
git commit -m "feat: add Huawei official live adapter"
```

---

### Task 3: Verify the Real Runner, Checkpoint, Excel, and Report Pipeline

**Files:**
- Create: `tests/integration/test_huawei_official_pipeline.py`
- Modify: `tests/regression/test_official_capture_reader_isolation.py` only if Huawei is not already covered generically
- Create: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-3-report.md`

**Interfaces:**
- Consumes: Task 1 `_HuaweiPage`; Task 2 `HuaweiOfficialAdapter`; `AdapterRegistry`; `WebsiteTaskRunner`; `SQLiteTaskRepository`; `CaptureRequest`; `run_full_pipeline`.
- Produces: end-to-end proof that Huawei `PRICE_FOUND` and legal-no outcomes survive the production task boundary and become correct `AK`/`AN` cells and report counts.

- [ ] **Step 1: Build one- and two-row Huawei workbook fixtures**

Create base, marketing, and BOP workbooks with canonical brand `华为`, exact Huawei model, `8GB`, `256GB`, and `曜金黑`. Use two material codes to verify source ordering.

```python
_row(
    45,
    C="HUAWEI",
    E="HUAWEI Mate 70 Pro",
    I=material,
    AQ="8GB",
    AR="256GB",
    AS="曜金黑",
)
```

- [ ] **Step 2: Test the normal runner-to-Excel path**

Run the real adapter registry, repository, runner, formal capture request, and full pipeline. Assert a numeric Huawei detail URL in the checkpoint, `AK=4999`, a valid image in `AN`, and one completed row in the report.

```python
assert quote_sheet["AK2"].value == 4999
assert len(quote_sheet._images) == 1
assert capture.requests[0].state.price == Decimal("4999")
assert [r.role for r in capture.requests[0].css_rectangles] == [
    "title", "price", "capacity", "color"
]
```

- [ ] **Step 3: Test all three legal-no paths**

For no-model, no-capacity, and no-color, assert `AK="无"`, a formal evidence image in `AN`, the matching business outcome, and no technical-failure count.

- [ ] **Step 4: Test capture failure after price persistence**

Make formal capture fail after the runner saves the price checkpoint. Assert the repository still contains `4999` and the numeric detail URL, final `AK` remains `4999`, `AN` is empty, and the row/report is partial rather than falsely successful.

- [ ] **Step 5: Test two rows and resume without re-search**

Assert two Huawei rows preserve source order in `AK`/`AN` and report totals. Restart from a saved detail checkpoint and assert homepage fill/Enter counts remain zero.

- [ ] **Step 6: Run integration and frozen pipeline tests**

Run:

```bash
.venv/bin/pytest -q tests/integration/test_huawei_official_pipeline.py
.venv/bin/pytest -q \
  tests/integration/test_xiaomi_official_pipeline.py \
  tests/integration/test_oppo_official_pipeline.py \
  tests/integration/test_vivo_official_pipeline.py \
  tests/integration/test_web_to_excel.py \
  tests/unit/test_macos_capture_runtime.py
.venv/bin/ruff check tests/integration/test_huawei_official_pipeline.py
```

Expected: all selected tests and Ruff pass.

- [ ] **Step 7: Review and commit the pipeline coverage**

```bash
git add tests/integration/test_huawei_official_pipeline.py tests/regression/test_official_capture_reader_isolation.py .superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-3-report.md
git commit -m "test: cover Huawei official quotation pipeline"
```

---

### Task 4: Freeze Existing Sites and Prepare the Mac Acceptance Build

**Files:**
- Modify: `README.md`
- Modify: `docs/testing/live-site-matrix.md`
- Modify: `docs/testing/macos-evidence-smoke.md`
- Modify: `packaging/quotation_app.spec` only if the clean packaging smoke proves it necessary
- Create: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-4-report.md`

**Interfaces:**
- Consumes: accepted Task 2 adapter and Task 3 pipeline behavior.
- Produces: a clean Huawei-only acceptance application in a new output directory and a reproducible user test checklist.

- [ ] **Step 1: Run targeted and frozen regression matrices**

```bash
.venv/bin/pytest -q \
  tests/contract/test_official_huawei_live.py \
  tests/integration/test_huawei_official_pipeline.py \
  tests/unit/test_official_brand_factory.py \
  tests/regression/test_official_brand_isolation.py \
  tests/regression/test_official_capture_reader_isolation.py
.venv/bin/pytest -q \
  tests/contract/test_official_honor_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_vivo_live.py \
  tests/contract/test_jd_adapter.py \
  tests/contract/test_tmall_adapter.py \
  tests/integration/test_web_to_excel.py \
  tests/unit/test_macos_capture_runtime.py
```

Expected: all selected tests pass; no frozen-site baseline changes.

- [ ] **Step 2: Run broad quality verification**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src/quote_app/sites/official_brands src/quote_app/tasks/runner.py
```

If an environment-only test cannot run, record the exact command and failure in the task report; do not claim it passed.

- [ ] **Step 3: Update the acceptance checklist**

Document the two-step Mac test:

1. Existing row `华为畅享 90 Pro Max / 8GB+256GB / 曜金黑` must produce either a valid normal result or a correctly evidenced no-model result.
2. One currently sold VMALL phone must produce exact configuration, lowest valid current price, clear four-proof screenshot, `AK`, `AN`, and matching report totals.

- [ ] **Step 4: Verify packaging discovery**

```bash
.venv/bin/pytest -q tests/smoke/test_packaging_spec.py
```

Only if this test shows PyInstaller cannot discover Huawei, add:

```python
hiddenimports += ["quote_app.sites.official_brands.huawei"]
```

Then rerun the smoke test.

- [ ] **Step 5: Build a new signed Huawei acceptance package**

Use the repository’s existing packaging command and set new build/dist roots ending in `official-huawei-73`; do not overwrite `.72` or any earlier accepted package. Verify the app signature with the existing signing validation command and confirm the build label states “华为官网验收（仅官网）”.

- [ ] **Step 6: Commit documentation and any proven packaging change**

```bash
git add README.md docs/testing/live-site-matrix.md docs/testing/macos-evidence-smoke.md packaging/quotation_app.spec .superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-4-report.md
git commit -m "chore: prepare Huawei official acceptance build"
```

Omit `packaging/quotation_app.spec` from `git add` when no change was required.

---

### Task 5: Final Specification and Quality Review

**Files:**
- Create: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-5-review.md`
- Modify: only files required to close independently reproduced review findings

**Interfaces:**
- Consumes: Tasks 1–4 commits and reports.
- Produces: an approval decision with reproducible evidence and no unresolved Critical or Important issue before the user receives the package.

- [ ] **Step 1: Perform a specification review**

Review the implementation against every section of `docs/superpowers/specs/2026-08-12-huawei-official-live-adapter-design.md`. Explicitly verify URL routes, complete wait windows, capacity fallback boundary, price exclusions, price persistence before capture, four-proof-only gate, idempotent 80%, one-scroll limit, and frozen-site scope.

- [ ] **Step 2: Perform a clean-tree quality review**

Create a clean detached worktree at the candidate commit and rerun the Task 4 targeted matrix, Ruff, mypy, and packaging smoke. This detects uncommitted dependencies that a dirty development tree can hide.

- [ ] **Step 3: Fix findings with TDD**

For every reproduced Critical or Important finding, first add a failing contract or integration test, then apply the smallest Huawei-scoped fix and rerun the targeted/frozen matrix. Do not weaken exact matching, URL identity, price filtering, wait windows, or four-proof validation to make a test pass.

- [ ] **Step 4: Record the final decision**

Write `APPROVED` only if the clean candidate has zero unresolved Critical/Important findings and every claimed command has fresh output. Otherwise write `NOT APPROVED`, list exact files/lines and failing commands, and do not hand the package to the user.

- [ ] **Step 5: Commit final review evidence**

```bash
git add .superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-5-review.md
git commit -m "docs: record Huawei official final review"
```

---

## Plan Self-Review

- **Spec coverage:** Tasks 1–2 cover approved routes, search, exact models, sold-out handling, capacity fallback, color, lowest valid current price, stability, legal no, four-proof capture, scaling, scrolling, and checkpoint recovery. Task 3 covers real runner/SQLite/formal capture/Excel/report behavior. Task 4 covers frozen regressions and the Mac acceptance build. Task 5 requires independent clean-tree approval.
- **Placeholder scan:** No `TBD`, `TODO`, “implement later,” or unspecified error-handling step remains.
- **Type consistency:** The plan consistently uses `HuaweiOfficialAdapter`, `WebsiteTask`, `AdapterObservation`, `WebsiteObservationCheckpoint`, `OfficialBusinessState`, `OfficialOfferSnapshot`, `VerifiedSemanticState`, `CssRect`, and `CaptureRequest` from the existing codebase.
- **Scope:** Huawei is one independently testable subsystem. Apple remains outside this plan and starts only after Huawei user acceptance.

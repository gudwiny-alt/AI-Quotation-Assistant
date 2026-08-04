# Marketplace Three Regression Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore correct JD Power2 price extraction, allow the verified Tmall Power2 detail title, and restore JD Honor Play 80 no-model evidence without changing the Honor official-site baseline.

**Architecture:** Keep channel behavior inside the existing JD and Tmall adapters. Change JD SKU handling from pre-click stock classification to exact-selection verification, use the existing Tmall-specific base-model matcher for bounded detail titles, and add an explicit search-first positioning option to the shared result-card capture helper so only JD changes its final no-model framing.

**Tech Stack:** Python 3.12, pytest, Playwright synchronous adapter surface, PyInstaller macOS packaging, macOS codesign.

## Global Constraints

- Do not modify `src/quote_app/sites/official.py` or Honor official-site overrides.
- JD and Tmall remain at normal scale during navigation and switch to 80% only during final verification/capture.
- JD must never fall back from `12GB+256GB`/`幻夜黑` to another capacity or color.
- A selectable exact JD option remains quotable even when the page or option displays shortage wording.
- An exact option that cannot reach selected state is a technical failure, not a legal “无”.
- Existing Excel columns, output order, and per-channel checkpoint behavior remain unchanged.

---

### Task 1: JD Exact Selection Before Stock Classification

**Files:**
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `src/quote_app/sites/jd.py:700-780`

**Interfaces:**
- Consumes: `JDAdapter._prepare_exact_option(locator)`, `JDAdapter._wait_for_modern_selected(page, matcher, semantic_name)`.
- Produces: `JDAdapter._observe_modern_detail(...) -> AdapterObservation` that either returns `PRICE_FOUND` for the exact selected configuration or raises `LayoutRecognitionError` when selection cannot stabilize.

- [ ] **Step 1: Replace the masked regression test with an initially unselected shortage option**

```python
def test_modern_exact_shortage_capacity_is_clicked_before_price_decision() -> None:
    html = _honor_power2_modern_html()
    html = html.replace(
        "specification-item-sku specification-item-sku--selected'>12GB+256GB",
        "specification-item-sku specification-item-sku--lack'>12GB+256GB 无货",
        1,
    )
    page = _FixturePage(html=html)

    observation = JDAdapter(_honor_spec()).observe(
        _task(brand="HONOR", model_name="荣耀Power2", ram="12GB", storage="256GB", color="幻夜黑"),
        cast(Any, page),
    )

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("2999")
    assert "click:modern-capacity" in page.option_events
```

- [ ] **Step 2: Run the new test and verify RED**

Run: `.venv/bin/python -m pytest tests/contract/test_jd_adapter.py::test_modern_exact_shortage_capacity_is_clicked_before_price_decision -q`

Expected: FAIL because the old flow returns `BusinessOutcome.CAPACITY_UNAVAILABLE` before clicking the capacity.

- [ ] **Step 3: Implement selection-first handling**

In `_observe_modern_detail`, remove the pre-click `CAPACITY_UNAVAILABLE` and `COLOR_UNAVAILABLE` returns. For each exact option:

```python
self._prepare_exact_option(capacity)
capacity = self._wait_for_modern_selected(page, capacity_matcher, "capacity")
```

Apply the same sequence to color. Keep the existing final selected-label check and `_modern_selected_price(page)` call. Do not catch the selection timeout as a business outcome.

- [ ] **Step 4: Verify GREEN and run the JD contract suite**

Run: `.venv/bin/python -m pytest tests/contract/test_jd_adapter.py -q`

Expected: the new test and all JD contract tests pass. Update obsolete tests that expected a visible shortage class alone to produce `CAPACITY_UNAVAILABLE`; retain tests for genuinely missing/ambiguous options and non-stabilizing selection.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git commit -m "fix: select exact JD configuration before quoting"
```

### Task 2: Tmall Promotional Detail Title Matching

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `src/quote_app/sites/tmall.py:1020-1040`

**Interfaces:**
- Consumes: `_tmall_result_card_matches(model_name: str, card_text: str) -> bool` and the bounded `TMALL_DETAIL_TITLES` selector family.
- Produces: `TmallAdapter._matching_detail_titles(page, task) -> tuple[Any, ...]` accepting the requested base model in a long Tmall title while rejecting model variants.

- [ ] **Step 1: Add a failing Honor Power2 marketing-title test**

```python
def test_honor_power2_marketing_detail_title_reaches_price_and_capture_stage() -> None:
    html = _live_observed_html()
    html = html.replace("小米官方旗舰店", "荣耀官方旗舰店")
    html = html.replace("xiaomi.tmall.com", "hihonor.tmall.com")
    html = html.replace("小米 15", "荣耀Power2")
    html = html.replace("小米15", "荣耀Power2")
    html = html.replace(
        '<h1 class="ItemTitle--fixture">荣耀Power2</h1>',
        '<h1 class="ItemTitle--fixture">【政府补贴15%】HONOR/荣耀Power2智能手机10080mAh官方旗舰店</h1>',
    )
    task = _task(brand="HONOR", model_name="荣耀Power2")
    page = _FixturePage(html=html, after_search_url=_honor_power2_result_url())
    adapter = TmallAdapter(_honor_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.capture_scales == [0.8]
```

Add a second assertion/test proving `荣耀Power2 Pro` does not match a Power2 task.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `.venv/bin/python -m pytest tests/contract/test_tmall_adapter.py -k 'power2_marketing_detail_title or power2_variant_detail_title' -q`

Expected: the marketing-title test fails with `Tmall product detail model does not match`; the variant rejection remains green.

- [ ] **Step 3: Use the Tmall-specific matcher for bounded detail titles**

In `_matching_detail_titles`, keep the same selectors and visibility checks but replace:

```python
model_matches(task.model_name, title.inner_text())
```

with:

```python
_tmall_result_card_matches(task.model_name, title.inner_text())
```

Do not add a body-text fallback or new broad selector.

- [ ] **Step 4: Verify GREEN and run the Tmall contract suite**

Run: `.venv/bin/python -m pytest tests/contract/test_tmall_adapter.py -q`

Expected: all Tmall contract tests pass and the new Power2 test records `capture_scales == [0.8]`.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: recognize Tmall promotional detail titles"
```

### Task 3: JD Search-First No-Model Capture Framing

**Files:**
- Modify: `tests/unit/test_detail_capture_view.py`
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `src/quote_app/sites/detail_capture_view.py:118-160`
- Modify: `src/quote_app/sites/jd.py:960-1010`

**Interfaces:**
- Consumes: `position_result_cards_for_capture(page, *, search_input, product_name, product_card, site_name, prefer_search_anchor=False)`.
- Produces: an optional JD-only search-first path; default behavior remains unchanged for Tmall.

- [ ] **Step 1: Add failing helper and JD adapter tests**

```python
def test_result_capture_can_anchor_search_before_validating_card_name() -> None:
    position_result_cards_for_capture(
        page,
        search_input=search,
        product_name=title,
        product_card=card,
        site_name="JD",
        prefer_search_anchor=True,
    )
    assert page.scroll_targets == ["search"]
```

In the JD contract test, assert the no-model capture uses the search input as the only scroll anchor, retains `capture_scales == [0.8]`, and then returns rectangle roles `("search_keyword", "result_region")`.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_detail_capture_view.py tests/contract/test_jd_adapter.py -k 'search_anchor or no_model_prepares' -q`

Expected: FAIL because the helper has no `prefer_search_anchor` argument and currently scrolls the product card.

- [ ] **Step 3: Implement the optional search-first path**

Add a search alignment script using `scrollIntoView({block: 'start', inline: 'nearest'})`. In `position_result_cards_for_capture`:

```python
if prefer_search_anchor and search_input is not None:
    search_input.evaluate(_ALIGN_SEARCH_TO_VIEWPORT_TOP)
else:
    product_card.evaluate(_ALIGN_RESULT_CARD_TO_VIEWPORT_BOTTOM)
```

After the one positioning action, keep the existing checks that search input and product name are both inside the viewport. In `JDAdapter._prepare_capture_view_at_scale`, pass `prefer_search_anchor=True`. Leave the Tmall call unchanged.

- [ ] **Step 4: Verify GREEN and cross-channel capture regressions**

Run: `.venv/bin/python -m pytest tests/unit/test_detail_capture_view.py tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/unit/test_macos_capture_runtime.py -q`

Expected: all tests pass; JD uses search-first framing while Tmall retains its existing result-card framing.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/quote_app/sites/detail_capture_view.py src/quote_app/sites/jd.py tests/unit/test_detail_capture_view.py tests/contract/test_jd_adapter.py
git commit -m "fix: frame JD no-model evidence from search input"
```

### Task 4: Version, Regression Gate, and macOS Package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Build output: `dist-marketplace-three-fixes/福建移动铺货报价助手.app`

**Interfaces:**
- Produces: application label `京东天猫三项回归修复版（全部荣耀行）2026.08.04.42` and a signed macOS test application.

- [ ] **Step 1: Update the expected build label test first**

Set the expectation in `tests/unit/test_app.py` to:

```python
assert APP_BUILD_LABEL == "京东天猫三项回归修复版（全部荣耀行）2026.08.04.42"
```

- [ ] **Step 2: Verify RED, then update `APP_BUILD_LABEL`**

Run: `.venv/bin/python -m pytest tests/unit/test_app.py -q`

Expected before production change: FAIL with `.41 != .42`; after updating `src/quote_app/app.py`: PASS.

- [ ] **Step 3: Run regression gates**

```bash
.venv/bin/python -m pytest tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/regression/test_honor_official_baseline.py tests/unit/test_detail_capture_view.py tests/unit/test_macos_capture_runtime.py tests/unit/test_app.py -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m pytest -q
```

Expected: all commands exit 0; the Honor official baseline remains unchanged.

- [ ] **Step 4: Commit the version label**

```bash
git add src/quote_app/app.py tests/unit/test_app.py
git commit -m "chore: label marketplace regression test build"
```

- [ ] **Step 5: Build and verify the Mac application**

Run `packaging/macos/build.sh`, copy the signed bundle to `dist-marketplace-three-fixes/福建移动铺货报价助手.app`, then run:

```bash
codesign --verify --deep --strict --verbose=2 dist-marketplace-three-fixes/福建移动铺货报价助手.app
```

Expected: build exits 0 and codesign reports `valid on disk` and `satisfies its Designated Requirement`.

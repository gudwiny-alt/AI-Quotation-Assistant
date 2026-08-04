# Marketplace 80 Percent Final Capture and JD Stock-Independent Quote Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make JD and Tmall use a reversible 80% scale only during final verification/capture, quote JD Power2 `12GB+256GB`/幻夜黑 at its verified struck-through price `2699` even when the page is sold out, and capture the JD no-model search evidence.

**Architecture:** Keep the existing browser search, navigation, login, model matching, and HONOR official adapter unchanged. Adjust JD modern-option classification so a selected exact option remains valid despite a shortage class, then reuse the existing price policy to choose the verified struck-through price. Reuse the existing reversible capture-scale lifecycle for JD and add the same lifecycle to Tmall, changing the fixed scale to `0.8`.

**Tech Stack:** Python 3.12, Playwright sync API, pytest, PyInstaller, macOS codesign.

## Global Constraints

- Apply 80% only during final state verification and formal screenshot preparation.
- Do not change HONOR official search, price, screenshot, or Excel behavior.
- JD must keep the exact requested model, capacity, and color; do not fall back to another configuration.
- A selected exact JD option remains quotable even when the page says “暂时售完／到货通知” or carries a shortage class.
- The JD Power2 `12GB+256GB`/幻夜黑 quote is the verified struck-through price `2699`, not the subsidy price.
- Restore the original scale after successful capture, failed capture, context-build failure, or cancellation.
- Preserve the `.40` output and report behavior except for the explicitly changed JD/Tmall outcomes.

---

### Task 1: Keep Selected JD Exact Configuration Valid When Sold Out

**Files:**
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `src/quote_app/sites/jd.py`

**Interfaces:**
- Consumes: `JDAdapter._observe_modern_detail(task, page, detail_url) -> AdapterObservation`, `_is_modern_selected(locator) -> bool`, `_is_modern_unavailable(locator) -> bool`.
- Produces: the existing `AdapterObservation` with `BusinessOutcome.PRICE_FOUND`, `price=Decimal("2699")`, and the exact task capacity/color semantic state.

- [ ] **Step 1: Write the failing sold-out exact-configuration regression test**

Add a contract test that makes the exact `12GB+256GB` option both selected and shortage-marked, exposes the subsidy price and a struck-through `2699`, and asserts the exact configuration is still quoted:

```python
def test_modern_selected_exact_capacity_is_quoted_when_page_is_sold_out() -> None:
    html = (FIXTURES / "modern_detail_capacity_unavailable.html").read_text("utf-8")
    html = html.replace("小米京东自营旗舰店", "荣耀京东自营旗舰店")
    html = html.replace("小米 15", "荣耀Power2").replace("小米15", "荣耀Power2")
    html = html.replace(
        "specification-item-sku specification-item-sku--selected\">12GB+256GB",
        "specification-item-sku specification-item-sku--selected "
        "specification-item-sku--lack\">12GB+256GB",
        1,
    )
    html = html.replace(
        '<div class="product-price-panel"><span class="product-price--main">¥4,299</span></div>',
        '<div class="product-price-panel">'
        '<span class="product-price--main">¥2,166.65</span>国补领后价'
        '<span class="product-price--gray-line-through" '
        'style="text-decoration:line-through">¥2,699</span>'
        '</div>',
        1,
    )
    task = _task(
        brand="HONOR",
        model_name="荣耀Power2",
        ram="12GB",
        storage="256GB",
        color="黑色",
    )
    page = _FixturePage(
        html=html,
        after_search_url=(
            "https://mall.jd.com/view_search-1000000904-99-1-24-1.html"
            "?keyword=%E8%8D%A3%E8%80%80Power2"
        ),
    )

    observation = JDAdapter(_honor_spec()).observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("2699")
    assert observation.semantic_state.capacity == "12GB+256GB"
    assert observation.semantic_state.color == "黑色"
```

- [ ] **Step 2: Run the test and verify the current code fails for the expected reason**

Run:

```bash
.venv/bin/python -m pytest tests/contract/test_jd_adapter.py::test_modern_selected_exact_capacity_is_quoted_when_page_is_sold_out -q
```

Expected: FAIL because `_observe_modern_detail` returns `CAPACITY_UNAVAILABLE` before reading the price.

- [ ] **Step 3: Implement selected-state precedence over shortage styling**

In `JDAdapter._observe_modern_detail`, change both capacity and color legal-no gates so an already selected exact option is not rejected:

```python
if _is_modern_unavailable(capacity) and not _is_modern_selected(capacity):
    return self._legal_no(
        task,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        page,
        (_css_rect(capacity, "capacity"),),
    )
```

Apply the equivalent condition to the exact color option. Keep exact model/configuration matching and the existing struck-through price candidate logic unchanged.

- [ ] **Step 4: Run JD contract tests**

Run:

```bash
.venv/bin/python -m pytest tests/contract/test_jd_adapter.py -q
```

Expected: the new `2699` regression test and all existing JD contract tests pass, including the test where an unselected shortage option remains `CAPACITY_UNAVAILABLE`.

- [ ] **Step 5: Commit the JD stock-independent quote behavior**

```bash
git add src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git commit -m "fix: quote selected JD configuration when sold out"
```

---

### Task 2: Fix JD Final Capture at 80 Percent

**Files:**
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `src/quote_app/sites/jd.py`

**Interfaces:**
- Consumes: `apply_capture_scale(page, *, scale: float) -> None`, `restore_capture_scale(page) -> None`, `position_result_cards_for_capture(...) -> None`, `position_detail_for_capture(...) -> None`.
- Produces: `JDAdapter.prepare_capture_view(...)` applying exactly `0.8` once for both no-model and price-found final capture paths.

- [ ] **Step 1: Change JD capture tests to require 80%**

Update the no-model and modern-detail capture assertions:

```python
assert page.capture_scales == [0.8]
```

Add or retain assertions that the no-model path positions one result card, checks the search input, and re-reads `search_keyword` plus `result_region` rectangles after positioning.

- [ ] **Step 2: Run both focused tests and verify they fail against 90%**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contract/test_jd_adapter.py::test_jd_no_model_prepares_a_result_view_with_readable_card_names \
  tests/contract/test_jd_adapter.py::test_jd_modern_positions_the_selected_detail_only_when_formal_capture_is_prepared -q
```

Expected: FAIL with `[0.9] != [0.8]`.

- [ ] **Step 3: Change the JD final capture scale to 80%**

In `JDAdapter.prepare_capture_view`, change only the fixed final-capture value:

```python
apply_capture_scale(browser_page, scale=0.8)
```

Keep the existing try/restore behavior and single-positioning logic unchanged.

- [ ] **Step 4: Run JD capture and scale lifecycle tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contract/test_jd_adapter.py \
  tests/unit/test_detail_capture_view.py \
  tests/unit/test_macos_capture_runtime.py -q
```

Expected: all tests pass; success, failure, and context-build paths still restore the scale.

- [ ] **Step 5: Commit the JD 80% final capture**

```bash
git add src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git commit -m "fix: capture JD final evidence at 80 percent"
```

---

### Task 3: Add the Same Reversible 80 Percent Final Capture to Tmall

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `src/quote_app/sites/tmall.py`

**Interfaces:**
- Consumes: `apply_capture_scale(page, *, scale: float) -> None`, `restore_capture_scale(page) -> None`, and the existing runtime optional `restore_capture_view(task, page, expected) -> None` hook.
- Produces: `TmallAdapter.prepare_capture_view(...)` applying `0.8` only during final preparation and `TmallAdapter.restore_capture_view(...)` restoring the original scale.

- [ ] **Step 1: Add scale recording to the Tmall fixture page**

Extend `_FixturePage` with:

```python
self.capture_scales: list[float] = []
self.scale_restored = False
```

Make its page-level `evaluate` recognize the existing reversible scale scripts exactly as the JD fixture does:

```python
def evaluate(self, script: str, value: float | None = None) -> bool | None:
    if "quotation-capture-scale" in script:
        if "root.removeAttribute" in script:
            self.scale_restored = True
            return True
        assert value is not None
        self.capture_scales.append(value)
        return True
    if script == "() => window.scrollBy(0, -120)":
        self.window_scroll_offsets.append(-120)
    return None
```

- [ ] **Step 2: Write failing Tmall detail and no-model scale tests**

Extend the existing tests with:

```python
assert page.capture_scales == [0.8]
adapter.restore_capture_view(task, cast(Any, page), observation.semantic_state)
assert page.scale_restored is True
```

Apply these assertions once to the price-found final view and once to the no-model final view.

- [ ] **Step 3: Run the focused Tmall tests and verify missing behavior**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contract/test_tmall_adapter.py::test_tmall_no_model_prepares_a_result_view_with_readable_card_names \
  tests/contract/test_tmall_adapter.py::test_tmall_positions_the_selected_detail_only_when_formal_capture_is_prepared -q
```

Expected: FAIL because no 80% scale is applied and `restore_capture_view` does not exist.

- [ ] **Step 4: Implement Tmall scale apply/restore around final preparation**

Import the existing helpers:

```python
from quote_app.sites.detail_capture_view import (
    apply_capture_scale,
    position_detail_for_capture,
    position_result_cards_for_capture,
    restore_capture_scale,
)
```

Wrap the existing body without changing its matching or price logic:

```python
def prepare_capture_view(self, task, page, expected) -> None:
    self._validate_task(task)
    browser_page = _playwright_page(page)
    apply_capture_scale(browser_page, scale=0.8)
    try:
        self._prepare_capture_view_at_scale(task, browser_page, expected)
    except BaseException:
        try:
            restore_capture_scale(browser_page)
        except Exception:
            pass
        raise

def restore_capture_view(self, task, page, expected) -> None:
    self._validate_task(task)
    if not isinstance(expected, VerifiedSemanticState):
        raise LayoutRecognitionError(
            "Tmall capture state is unavailable for restoration"
        )
    restore_capture_scale(_playwright_page(page))
```

Move the previous `prepare_capture_view` body into `_prepare_capture_view_at_scale` unchanged.

- [ ] **Step 5: Run the complete Tmall contract and runtime lifecycle suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contract/test_tmall_adapter.py \
  tests/unit/test_macos_capture_runtime.py -q
```

Expected: all tests pass; the runtime discovers and calls the Tmall restore hook through the existing generic adapter protocol.

- [ ] **Step 6: Commit the Tmall 80% final capture**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "feat: capture Tmall final evidence at 80 percent"
```

---

### Task 4: Version, Full Regression, Build, and Signed Test Package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Build output: `dist-marketplace-80-final/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: `APP_BUILD_LABEL: str`, `packaging/macos/build.sh`.
- Produces: a signed Mac test app labeled `京东天猫80%最终截图版（全部荣耀行）2026.08.04.41`.

- [ ] **Step 1: Write the failing build-label assertion**

```python
assert APP_BUILD_LABEL == "京东天猫80%最终截图版（全部荣耀行）2026.08.04.41"
```

- [ ] **Step 2: Run the focused label test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_app.py -q
```

Expected: FAIL because the implementation still reports `.40`.

- [ ] **Step 3: Update only the application build label**

```python
APP_BUILD_LABEL = "京东天猫80%最终截图版（全部荣耀行）2026.08.04.41"
```

- [ ] **Step 4: Run focused cross-site regression and lint**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contract/test_jd_adapter.py \
  tests/contract/test_tmall_adapter.py \
  tests/regression/test_honor_official_baseline.py \
  tests/unit/test_detail_capture_view.py \
  tests/unit/test_macos_capture_runtime.py \
  tests/unit/test_app.py -q
.venv/bin/python -m ruff check src tests
```

Expected: all focused tests and lint pass; the HONOR official regression remains unchanged.

- [ ] **Step 5: Run the full suite and build the Mac app**

Run outside the filesystem/network sandbox because the persistent-browser integration test binds a local port:

```bash
packaging/macos/build.sh
```

Expected: the full pytest suite passes and `dist/福建移动铺货报价助手.app` is produced.

- [ ] **Step 6: Copy and verify the signed acceptance package**

```bash
mkdir -p dist-marketplace-80-final
ditto "dist/福建移动铺货报价助手.app" \
  "dist-marketplace-80-final/福建移动铺货报价助手.app"
codesign --verify --deep --strict --verbose=2 \
  "dist-marketplace-80-final/福建移动铺货报价助手.app"
```

Expected: codesign reports the app is valid on disk and satisfies its designated requirement.

- [ ] **Step 7: Commit the versioned release change**

```bash
git add src/quote_app/app.py tests/unit/test_app.py
git commit -m "chore: label marketplace 80 percent test build"
```

- [ ] **Step 8: Hand off the two-row HONOR acceptance checklist**

Verify manually with `1.基础表（2 条荣耀）.xlsx`:

1. JD Power2 writes `2699` and captures title, struck-through price, 幻夜黑, and `12GB+256GB`.
2. JD 畅玩80 writes “无” and captures the search term plus non-matching card names.
3. Tmall final pages use 80% and write/capture according to the existing matching rules.
4. HONOR official output matches the `.40` baseline.

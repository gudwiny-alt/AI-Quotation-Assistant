# Tmall Pre-discount Price Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Tmall HONOR Power2 detail page quote the explicit `优惠前 ¥2699` amount while continuing to reject the `平台补贴后 ¥2549` amount.

**Architecture:** Extend only the Tmall current-SKU price collector with a container-scoped pre-discount node path. Represent that candidate with explicit verified pre-discount evidence, then reuse the existing price selection and stability pipeline without changing JD, official-site, Excel, search, or capture behavior.

**Tech Stack:** Python 3.12, pytest, Playwright-compatible locators, PyInstaller, macOS ad-hoc codesign.

## Global Constraints

- Only Tmall current-detail price candidate recognition may change.
- `平台补贴后`, `百亿补贴`, and other subsidy prices remain excluded.
- An explicit current-container `优惠前` amount is equivalent to a verified pre-discount/list price.
- JD, official-site, Excel, Tmall search, selection, stability timing, and capture logic remain unchanged.

---

### Task 1: Recognize a verified Tmall pre-discount price

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `src/quote_app/sites/tmall.py`
- Modify: `src/quote_app/sites/prices.py`

**Interfaces:**
- Consumes: the unique visible Tmall current-SKU price container and its visible price descendants.
- Produces: a `PriceCandidate` carrying `VERIFIED_CURRENT_SKU_PRE_DISCOUNT_PRICE` evidence for an exact `优惠前` amount.

- [ ] **Step 1: Write the failing Power2 price-area contract**

Add a real adapter test whose current container contains `平台补贴后 ¥2549` and `优惠前 ¥2699`, then assert `observation.price == Decimal("2699")`.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py::test_honor_power2_uses_explicit_pre_discount_price_beside_subsidy_price -q
```

Expected: FAIL with `Tmall selected variant price did not reach a verified stable state`.

- [ ] **Step 3: Implement the minimum container-scoped recognition**

Add a Tmall selector for `[class^="subPrice--"]`, require the visible node text to match one explicit `优惠前` RMB amount, and emit a candidate with verified pre-discount evidence. Keep the existing skip for subsidy-after candidates and leave the generic exclusion list unchanged.

- [ ] **Step 4: Verify GREEN and frozen contracts**

Run the focused test, the full Tmall contract, price unit tests, JD contract, HONOR official baseline, and Excel integration tests. Then run Ruff on the modified Python files.

### Task 2: Label, build, sign, and verify the new Mac package

**Files:**
- Modify: `tests/unit/test_app.py`
- Modify: `src/quote_app/app.py`
- Create: `dist-tmall-pre-discount-47/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: the verified source tree and existing PyInstaller specification.
- Produces: a separately named `.47` macOS app with source/package bytecode freshness evidence.

- [ ] **Step 1: Add the failing build-label test**

Expect `APP_BUILD_LABEL` to identify the Tmall pre-discount-price build `.47`, run the focused unit test, and verify it fails against `.46`.

- [ ] **Step 2: Update only the label and verify GREEN**

Change the label, rerun the focused test, then run the full test suite.

- [ ] **Step 3: Build and sign independently**

Build with the existing PyInstaller spec into `dist-tmall-pre-discount-47`, ad-hoc sign the app, run strict `codesign` verification, and compare packaged bytecode hashes for the modified modules against current source.

# 荣耀候选商品详情跳转 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 荣耀官网搜索后，以可验证的基础机型详情链接作为进入详情页的唯一业务前置条件，而非以 URL、搜索框或结果容器作为阻塞条件。

**Architecture:** 点击搜索并在同一标签页轮询商品卡；搜索结果可包含多个型号。程序仅筛选与基础机型精确匹配的卡片，排除 Plus/Pro 等变体，验证其荣耀数字商品页链接并按链接去重。URL、搜索框和 `#mainSaleList` 仅作为优先定位或诊断信息。详情页 SKU、价格、截图和 Excel 流程不改。

**Tech Stack:** Python 3.12、pytest、Playwright 适配器契约测试。

## Global Constraints

- 仅修改 HONOR 官网“搜索→候选详情链接”入口。
- 允许多个搜索结果和多个显示卡片；不要求结果列表只有一张卡。
- 相同的正式详情 URL 视为一个候选；多个不同、均精确匹配的正式详情 URL 才报歧义。
- 不改详情页验证、SKU、价格、截图、Excel、浏览器窗口、京东和天猫。
- 不使用坐标点击或固定睡眠；只用现有有界状态轮询。

---

### Task 1: 锁定候选链接驱动的搜索行为

**Files:**
- Modify: `tests/contract/test_official_honor_live.py`

**Interfaces:**
- Consumes: `OfficialSiteAdapter.observe(task, page)`、荣耀搜索页面夹具及 `HonorOfficialOverride.card_matches_model`。
- Produces: 对“URL 不变但目标商品卡已显示”和“畅玩80同时出现 Plus/Pro”的回归契约。

- [ ] **Step 1: Write the failing URL-independent candidate test**

```python
def test_honor_live_enters_detail_when_target_card_renders_without_search_url(...):
    # 搜索动作切换出商品卡，但保留首页 URL。
    observation = adapter.observe(task, page)
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/contract/test_official_honor_live.py -k target_card_renders_without_search_url`

Expected: `HONOR_SEARCH_RESULTS_MISSING`，证明旧流程错误地把搜索 URL 当成硬前置条件。

- [ ] **Step 3: Write the variant-result test**

```python
def test_honor_live_ignores_plus_and_pro_result_cards(...):
    # 畅玩80、畅玩80 Plus、畅玩80 Pro 同时显示。
    observation = adapter.observe(task, page)
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")
```

- [ ] **Step 4: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/contract/test_official_honor_live.py -k ignores_plus_and_pro_result_cards`

Expected: the current URL gate prevents observation before candidate selection.

### Task 2: 以正式详情链接筛选并去重候选

**Files:**
- Modify: `src/quote_app/sites/official.py`
- Test: `tests/contract/test_official_honor_live.py`

**Interfaces:**
- Produces: `_wait_for_honor_candidate_detail_urls(page, task) -> tuple[str, ...]`。
- Consumes: `li.grid-items`、`a.thumb`、`HonorOfficialOverride.card_matches_model`、`_approved_product_url`。

- [ ] **Step 1: Replace URL-first wait with candidate-link wait**

After search click, poll visible product cards first in `#mainSaleList li.grid-items`, then fall back to page-level `li.grid-items`. For each exact base-model card, accept only one visible thumbnail link that resolves through `_approved_product_url`. Deduplicate approved URLs while preserving page order.

- [ ] **Step 2: Keep one Enter fallback and explicit ambiguity handling**

If no candidate URL appears in the first bounded polling period, press Enter once and poll again. Zero URLs reports `HONOR_PRODUCT_MATCH_MISSING`; more than one distinct URL reports `HONOR_PRODUCT_MATCH_AMBIGUOUS`; one URL navigates using the existing detail flow unchanged.

- [ ] **Step 3: Verify GREEN and HONOR regression**

Run: `.venv/bin/python -m pytest -q tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py tests/unit/test_honor_search_diagnostics.py`

Expected: all pass.

### Task 3: Build focused Mac verification package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-honor-candidate-navigation/`

**Interfaces:**
- Produces: clearly labelled “荣耀候选详情跳转修复版” macOS test package.

- [ ] **Step 1: Write and run failing label test**

Update the build-label unit test to expect the candidate-link scope and user notice.

- [ ] **Step 2: Update only the label/notice**

Set a new build label and state that the package enters details from verified target cards even when the site does not provide the expected URL shape.

- [ ] **Step 3: Build and verify**

Run `.venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist-honor-candidate-navigation --workpath build-honor-candidate-navigation packaging/quotation_app.spec`, ad-hoc sign the app, and verify with `codesign --verify --deep --strict`.

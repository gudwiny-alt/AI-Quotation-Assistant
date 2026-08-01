# 荣耀官网详情页进入修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让荣耀基础机型能够从搜索结果稳定进入唯一的数字商品详情页，并在确定性匹配失败时停止重试。

**Architecture:** 继续使用 `HonorOfficialOverride` 作为荣耀网页语义边界。只调整其卡片标题匹配规则，并由 `OfficialSiteAdapter._observe_honor_live` 将零/多匹配转换为可报告的不可重试错误；详情页和证据链不改动。

**Tech Stack:** Python 3.12、pytest、Playwright 适配器契约测试。

## Global Constraints

- 不修改京东、天猫、Excel、截图或 Mac 窗口代码。
- 不改变荣耀详情页 SKU、价格、库存校验。
- 单标签页导航；不读取个人 Chrome 数据或登录资料。

---

### Task 1: 锁定荣耀卡片匹配和失败语义

**Files:**
- Modify: `tests/contract/test_official_honor_live.py`
- Modify: `tests/regression/test_honor_official_baseline.py`

**Interfaces:**
- Consumes: `OfficialSiteAdapter.observe(task, page)` 与 `HonorOfficialOverride.card_matches_model()`。
- Produces: Power2 容量卡片进入详情页、变体拒绝、零/多卡片不可重试的回归约束。

- [ ] **Step 1: Write the failing tests**

```python
def test_honor_live_accepts_capacity_after_a_space_in_the_card_title(...):
    observation = adapter.observe(power2_task, page)
    assert observation.url.endswith('/product/<numeric>.html')

def test_honor_live_stops_once_when_no_unique_card_matches(...):
    with pytest.raises(NonRetryableTechnicalError, match='HONOR_PRODUCT_MATCH_MISSING'):
        adapter.observe(task, page)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py -k 'capacity_after or stops_once'`

- [ ] **Step 3: Update baseline guard scope**

Keep the approved detail-chain hash guard and replace the mutable card-matching hash assertion with explicit behavioral assertions.

### Task 2: Implement the narrow navigation repair

**Files:**
- Modify: `src/quote_app/sites/official_overrides/honor.py`
- Modify: `src/quote_app/sites/official.py`

**Interfaces:**
- Consumes: normalized task model and visible card text.
- Produces: one valid product link or a `NonRetryableTechnicalError` with a stable HONOR product-match code.

- [ ] **Step 1: Implement card suffix handling**

Permit only whitespace-separated capacity or official description after the exact base model. Keep unseparated ASCII continuations and known model variants rejected.

- [ ] **Step 2: Classify zero/multiple card match as non-retryable**

Raise `HONOR_PRODUCT_MATCH_MISSING` for zero matches and `HONOR_PRODUCT_MATCH_AMBIGUOUS` for more than one. Keep the browser on the search page.

- [ ] **Step 3: Run focused tests and type checking**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py` and `python -m mypy src/quote_app/sites/official.py src/quote_app/sites/official_overrides/honor.py`.

### Task 3: Build an isolated Mac verification package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-honor-detail-navigation/福建移动铺货报价助手.app`

- [ ] **Step 1: Test and increment the in-app build label**

Set a unique label identifying this package as the HONOR detail-navigation repair.

- [ ] **Step 2: Run relevant regression suite, build and verify signature**

Run focused tests, build to a new directory, ad-hoc sign, then verify with `codesign --verify --deep --strict`.

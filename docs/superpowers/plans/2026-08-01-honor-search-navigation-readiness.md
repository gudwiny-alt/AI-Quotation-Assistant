# 荣耀搜索页面跳转与就绪校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 仅在荣耀搜索页面完整跳转并完成渲染后，开始商品卡片匹配。

**Architecture:** 在 `OfficialSiteAdapter._observe_honor_live` 中将“搜索 URL 就绪”作为结果区域读取的前置条件；复用现有有界轮询常量和 `_validate_honor_search_url`，不触及详情页、价格或捕获链路。

**Tech Stack:** Python 3.12、pytest、Playwright 适配器契约测试。

## Global Constraints

- 只改 HONOR 官网搜索入口。
- 不改卡片匹配规则、详情页、SKU、价格、截图、Excel、窗口控制、京东或天猫。
- 不新增固定睡眠；只使用有界、可中断的状态轮询。

---

### Task 1: 锁定首页推荐卡片不得误判为搜索结果

**Files:**
- Modify: `tests/contract/test_official_honor_live.py`

**Interfaces:**
- Consumes: `OfficialSiteAdapter.observe(task, page)` 与测试页面的首页推荐卡片、需要 Enter 的搜索动作。
- Produces: 断言页面进入搜索 URL 前不会匹配首页卡片，且按一次 Enter 后才进入详情页。

- [ ] **Step 1: Write the failing test**

```python
def test_honor_live_waits_for_search_url_before_reading_homepage_cards(...):
    observation = adapter.observe(task, page)
    assert page.presses == ["Enter"]
    assert "/v/search?keyword=" in observation.url
```

- [ ] **Step 2: Run test to verify RED**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py -k search_url`

### Task 2: Implement URL-first state readiness

**Files:**
- Modify: `src/quote_app/sites/official.py`

**Interfaces:**
- Produces: `_wait_for_honor_search_url(page, task) -> bool` and URL-gated result-region matching.

- [ ] **Step 1: Implement bounded URL readiness**

Poll `_validate_honor_search_url` for the `/cn/shop/v/search?keyword=<基础机型>` shape within the existing maximum interval. The comparison removes only a leading HONOR/荣耀 label and display whitespace, so `荣耀畅玩80` matches `畅玩 80`; it does not relax card-title variant matching. If the first submit did not transition, use exactly one Enter fallback then repeat the same wait.

- [ ] **Step 2: Require input keyword and cards after URL readiness**

Keep result input text and visible card checks after the URL gate, then retain the existing card-to-detail navigation exactly.

- [ ] **Step 3: Run regression tests and type check**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py tests/unit/test_honor_search_diagnostics.py` and `python -m mypy src/quote_app/sites/official.py`.

### Task 3: Build narrow Mac test package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-honor-search-readiness/`

- [ ] **Step 1: Test and update the user-visible build label**

Identify the package as “荣耀搜索就绪修复版”; the notice states it waits for actual search-page navigation.

- [ ] **Step 2: Build and validate signature**

Create a new output directory, ad-hoc sign the package, verify `codesign --verify --deep --strict` and preserve all earlier build directories.

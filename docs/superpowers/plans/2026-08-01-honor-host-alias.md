# 荣耀官网域名别名修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许荣耀官网在其受控的 `www.honor.com` 与 `honor.com` 之间跳转后，继续从搜索结果进入唯一商品详情页。

**Architecture:** 仅调整荣耀搜索 URL 的同站主机校验，将受控主机集合限定为根域名和 `www` 别名。路径、HTTPS、端口、凭据、唯一 `keyword` 参数及商品详情页、SKU、价格、截图、Excel 逻辑保持原样。

**Tech Stack:** Python 3.12、pytest、Playwright 适配器契约测试。

## Global Constraints

- 只改 HONOR 官网搜索 URL 的主机别名校验。
- 不改商品卡匹配、详情页、SKU、价格、截图、Excel、窗口控制、京东或天猫。
- 仅接受 `honor.com` 和 `www.honor.com`；不接受子域名、非 HTTPS、端口、凭据、额外参数或其他路径。

---

### Task 1: 锁定无 `www` 跳转仍可进入详情页

**Files:**
- Modify: `tests/contract/test_official_honor_live.py`

**Interfaces:**
- Consumes: `OfficialSiteAdapter.observe(task, page)` 和模拟搜索页跳转。
- Produces: 当搜索页 URL 为 `https://honor.com/cn/shop/v/search?...` 时，观察流程进入受控数字商品详情页的回归契约。

- [ ] **Step 1: Write the failing test**

```python
def test_honor_live_accepts_root_host_search_redirect(...):
    # 模拟商城把 www.honor.com 重定向成 honor.com。
    observation = adapter.observe(task, page)
    assert observation.url.endswith(f"/product/{_PRODUCT_ID}.html")
```

- [ ] **Step 2: Run test to verify RED**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py -k root_host_search_redirect`

Expected: `HONOR_SEARCH_RESULTS_MISSING`，因为现有校验只接受配置中的 `www.honor.com`。

### Task 2: 仅放宽荣耀受控同站主机别名

**Files:**
- Modify: `src/quote_app/sites/official.py`
- Test: `tests/contract/test_official_honor_live.py`

**Interfaces:**
- Produces: `_validate_honor_search_url(...)` 对 `honor.com` 和 `www.honor.com` 的同站等价识别。

- [ ] **Step 1: Implement minimal host alias check**

在 `_validate_honor_search_url` 内，将“完全等于入口 host”替换为“入口 host 的根域名或其 `www.` 别名”，其余 URL 校验条件不变。

- [ ] **Step 2: Verify focused test turns GREEN**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py -k root_host_search_redirect`

Expected: PASS。

- [ ] **Step 3: Run HONOR regression and type checks**

Run: `python -m pytest -q tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py tests/unit/test_honor_search_diagnostics.py && python -m mypy src/quote_app/sites/official.py`

Expected: all pass.

### Task 3: Build narrow Mac verification package

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-honor-host-alias/`

**Interfaces:**
- Produces: clearly labelled “荣耀官网域名跳转修复版” macOS verification package.

- [ ] **Step 1: Update the package label and unit expectation**

The label identifies this exact scope and states that it supports the official site’s `www`/root-domain redirect.

- [ ] **Step 2: Build, sign and verify**

Create only `dist-honor-host-alias/`; ad-hoc sign it and run `codesign --verify --deep --strict`.

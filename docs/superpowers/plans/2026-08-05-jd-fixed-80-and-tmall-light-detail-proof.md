# 京东固定 80% 与天猫轻量详情页证明 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让京东在观察、取景和截图重试期间稳定保持 80%，让任意匹配的可见京东搜索框可作为无报价证据，并允许天猫同一机型的多个标题文案通过轻量详情页证明。

**Architecture:** JD 适配器拥有京东页面比例生命周期，在每次真实导航后确保 80%，截图准备只校验和定位而不恢复重试。JD 搜索框解析从“必须唯一”改为“过滤所有可见候选后至少一个匹配”。Tmall 稳定状态以标准化基础机型身份代替完整标题字符串，同时保留 URL、卖家、配置和价格验证。

**Tech Stack:** Python 3.12、Playwright 同步 API、pytest、Ruff、PyInstaller。

## Global Constraints

- 不修改荣耀官网适配器、Excel 输出结构、渠道顺序和价格政策。
- 京东 Power2 继续取得划线价 `2699`，是否有货不影响截图。
- 京东畅玩80截图必须同时证明搜索词与不匹配商品名称。
- 天猫详情页至少保留一个匹配机型标题、受控 URL 和官方卖家校验。
- 所有生产代码必须由先失败的回归测试驱动。

---

### Task 1: 京东固定 80% 生命周期

**Files:**
- Modify: `src/quote_app/sites/jd.py`
- Test: `tests/contract/test_jd_adapter.py`
- Test: `tests/unit/test_macos_capture_runtime.py`

**Interfaces:**
- Consumes: `apply_capture_scale(page, scale=0.8)` 与现有 `prepare_capture_view` 调用协议。
- Produces: JD 观察／恢复导航后的固定 80% 页面，以及不会恢复比例的 JD 截图准备和清理行为。

- [ ] **Step 1: 写失败测试**

新增契约测试，模拟观察后连续两次 `prepare_capture_view`，断言页面始终为 `0.8`、`capture_scale_restore_count == 0`，并验证观察和恢复导航后均已应用 80%。

- [ ] **Step 2: 验证 RED**

Run: `.venv/bin/pytest tests/contract/test_jd_adapter.py -k 'fixed_scale or capture_retry_keeps_scale' -q`

Expected: FAIL，因为现有准备失败会恢复比例，正式截图结束也调用恢复钩子。

- [ ] **Step 3: 最小实现**

在 JD 已完成的每次真实导航后确保 `0.8`；删除 `prepare_capture_view` 中恢复后重缩放的内部循环；将 JD 恢复钩子改为保持当前 80% 的验证型清理，不回到 100%。

- [ ] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest tests/contract/test_jd_adapter.py -k 'fixed_scale or capture_retry_keeps_scale' -q`

Expected: PASS。

### Task 2: 京东多搜索框证据选择

**Files:**
- Modify: `src/quote_app/sites/locators.py`
- Modify: `src/quote_app/sites/jd.py`
- Test: `tests/contract/test_jd_adapter.py`

**Interfaces:**
- Consumes: `JD_SEARCH_INPUTS` 和 `normalize_product_text`。
- Produces: `_validated_result_search_input(page, model_name) -> Any | None`，从所有可见候选中返回第一个目标词匹配输入框。

- [ ] **Step 1: 写失败测试**

新增一个包含顶部空白搜索框和店内“荣耀畅玩80”搜索框的无报价夹具；断言观察成功、正式取景成功，并产生 `search_keyword` 与 `result_region` 两个证据矩形。

- [ ] **Step 2: 验证 RED**

Run: `.venv/bin/pytest tests/contract/test_jd_adapter.py -k 'multiple_search_inputs' -q`

Expected: FAIL，因为现有定位器只包含 `#key01`，且验证函数要求可见搜索框数量恰好为一。

- [ ] **Step 3: 最小实现**

扩展受控 JD 搜索输入选择器；逐个读取可见候选的 `input_value()`，选择归一化后与目标机型一致的第一个候选。存在不匹配候选不报错；所有候选均不匹配时返回 `None`。

- [ ] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest tests/contract/test_jd_adapter.py -k 'multiple_search_inputs or no_model' -q`

Expected: PASS。

### Task 3: 天猫轻量标题一致性

**Files:**
- Modify: `src/quote_app/sites/tmall.py`
- Test: `tests/contract/test_tmall_adapter.py`

**Interfaces:**
- Consumes: `_matching_detail_titles(page, task)` 的至少一个机型匹配标题证明。
- Produces: `_matching_detail_title_snapshot(page, task) -> str`，返回标准化基础机型身份，不再返回完整营销标题集合中的唯一值。

- [ ] **Step 1: 写失败测试**

新增 Power2 详情页夹具，同时放置“荣耀Power2”和“政府补贴 HONOR/荣耀Power2 智能手机”两个可见匹配标题；断言观察、价格稳定和截图准备成功。保留无匹配标题拒绝测试。

- [ ] **Step 2: 验证 RED**

Run: `.venv/bin/pytest tests/contract/test_tmall_adapter.py -k 'multiple_matching_detail_titles' -q`

Expected: FAIL，错误为 `Tmall matching detail title is missing or ambiguous`。

- [ ] **Step 3: 最小实现**

让标题快照先要求至少一个匹配标题，再返回 `normalize_product_text(task.model_name)`；URL、卖家、配置、价格和阻断页校验保持不变。

- [ ] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest tests/contract/test_tmall_adapter.py -k 'multiple_matching_detail_titles or marketing_detail_title or detail_model' -q`

Expected: PASS。

### Task 4: 回归验证与 Mac 测试包

**Files:**
- Modify: `src/quote_app/app.py`（仅更新测试包标签）
- Verify: `dist-marketplace-visible-state/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: Tasks 1—3 的通过实现。
- Produces: 可签名、可运行的 Mac 验收应用。

- [ ] **Step 1: 运行聚焦回归**

Run: `.venv/bin/pytest tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/unit/test_macos_capture_runtime.py tests/regression/test_honor_official_baseline.py -q`

Expected: PASS。

- [ ] **Step 2: 运行静态检查和全量测试**

Run: `.venv/bin/ruff check src tests`

Run: `.venv/bin/pytest -q`

Expected: PASS；若唯一失败是沙箱禁止 localhost，则在已批准的非沙箱环境单独复验该测试。

- [ ] **Step 3: 更新构建标签并重建应用**

只更新测试包说明文字，使用现有构建脚本重建 `dist-marketplace-visible-state/福建移动铺货报价助手.app`。

- [ ] **Step 4: 校验签名和包内源码新鲜度**

Run: `codesign --verify --deep --strict --verbose=2 'dist-marketplace-visible-state/福建移动铺货报价助手.app'`

Expected: `valid on disk` 且关键模块与当前源码一致。

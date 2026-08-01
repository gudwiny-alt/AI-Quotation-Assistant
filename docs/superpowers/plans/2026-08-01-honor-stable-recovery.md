# 荣耀官网稳定恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让全部荣耀官网行能以语义就绪节奏完成价格与截图的逐行写入；运行成功后只留下最终两份 Excel，且不改变已验证的详情、价格、截图和写入链路。

**Architecture:** 在荣耀官网适配器的首页搜索提交处增加一个受限的“点击后等结果、一次 Enter 补救、失败即停止”的入口兼容层。详情页的既有语义选择与价格读取保持原样。增量发布继续提供处理中检查点；最终文件对成功写入后才清除该对检查点。macOS 测试版继续保持人工窗口布局，只在截图所需元素语义就绪后做目标元素滚动和稳定性复核。

**Tech Stack:** Python、pytest、Playwright、Tkinter、openpyxl、macOS Chromium CDP/Accessibility。

## Global Constraints

- 仅对 `HONOR` 的 `WebsiteChannel.OFFICIAL` 验收路径改动；京东、天猫代码和任务范围不改。
- 荣耀详情 URL、型号匹配、SKU 选择、价格解析、截图语义验证和 Excel 写入逻辑不得重写或放宽。
- macOS 测试版不得自动最大化、缩放、移动或重新开启用户摆好的 Chrome 窗口。
- 每个行为变更必须先运行对应的失败测试，再写最小实现；每个任务单独提交。
- 只有最终报价表和最终报告均成功写入后才可删除本次运行的 `-处理中.xlsx` 对；异常时必须保留。

---

### Task 1: 为荣耀首页结果就绪建立可复现合同

**Files:**
- Modify: `tests/conftest.py:145-330`
- Modify: `tests/contract/test_official_honor_live.py:240-325`

**Interfaces:**
- Consumes: `_OfficialFixturePage` 和 `OfficialStoreAdapter.observe(task, page)`。
- Produces: fixture 可配置“点击搜索不显示结果，仅 `Enter` 后显示结果”，且记录 `press()` 调用；荣耀合同测试可验证入口行为而非内部实现。

- [ ] **Step 1: 写失败的 Enter 补救合同测试**

在 `tests/contract/test_official_honor_live.py` 增加：

```python
def test_honor_live_submits_enter_once_when_click_does_not_render_results(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            'data-action="search"',
            'data-action="search" data-requires-enter="true"',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.presses == ["Enter"]
    assert page.goto_calls.count(adapter.spec.entry_url) == 1
```

- [ ] **Step 2: 运行测试，确认因 fixture 或入口尚不支持而失败**

Run: `.venv/bin/pytest tests/contract/test_official_honor_live.py::test_honor_live_submits_enter_once_when_click_does_not_render_results -q`

Expected: FAIL；不是测试拼写或导入错误。

- [ ] **Step 3: 最小扩展 fixture，不修改生产代码**

为 `_OfficialFixturePage` 添加 `presses: list[str]` 和 `press(key)`；为 `_OfficialLocator.click()` 识别 `data-requires-enter`，仅记录“搜索待提交”而不激活结果；`press("Enter")` 仅在搜索待提交时激活结果。保持既有点击即出结果 fixture 行为。

- [ ] **Step 4: 重跑，确认仍由生产逻辑缺少 Enter 而失败**

Run: `.venv/bin/pytest tests/contract/test_official_honor_live.py::test_honor_live_submits_enter_once_when_click_does_not_render_results -q`

Expected: FAIL，断言 `BusinessOutcome.PRICE_FOUND` 不成立或抛出结果卡片缺失；证明测试能捕捉本次缺陷。

- [ ] **Step 5: 提交测试基础设施**

```bash
git add tests/conftest.py tests/contract/test_official_honor_live.py
git commit -m "test: reproduce delayed HONOR search results"
```

### Task 2: 仅修复荣耀首页搜索提交与有界等待

**Files:**
- Modify: `src/quote_app/sites/official.py:659-731,805-835`
- Modify: `tests/contract/test_official_honor_live.py`
- Modify: `tests/regression/test_honor_official_baseline.py`

**Interfaces:**
- Consumes: `page`, `HonorOfficialOverride.search_inputs`, `HonorOfficialOverride.result_regions` 和 `visible_locators()`。
- Produces: `_wait_for_honor_result_region(page) -> Any | None`，返回唯一且包含卡片的结果区域；点击和一次 `Enter` 各调用一次。`None` 被转换为明确的 `LayoutRecognitionError`，不触及详情逻辑。

- [ ] **Step 1: 为点击后延迟结果写失败测试**

在现有 `test_honor_live_waits_for_search_results_to_render` 基础上断言无 Enter；新增：

```python
def test_honor_live_waits_for_cards_before_opening_detail(official_case: Any) -> None:
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            '<ul id="mainSaleList">',
            '<ul id="mainSaleList" hidden data-show-after-waits="2">',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.presses == []
    assert len(page.wait_timeout_milliseconds) >= 2
```

- [ ] **Step 2: 运行两个合同测试，确认它们失败在现有入口时序**

Run: `.venv/bin/pytest tests/contract/test_official_honor_live.py -k 'search_results_to_render or submits_enter_once or waits_for_cards_before_opening_detail' -q`

Expected: FAIL，Enter 场景失败；延迟场景若意外已通过，记录其由既有 `_wait_for_honor_visible()` 覆盖，不将其作为新增回归证明。

- [ ] **Step 3: 实现最小入口兼容层**

在 `_observe_honor_live()` 中保留填充和点击，替换 `wait_for_load_state("domcontentloaded")` 后的直接结果读取：

```python
search_action.click()
result_region = self._wait_for_honor_result_region(page)
if result_region is None:
    search_input.press("Enter")
    result_region = self._wait_for_honor_result_region(page)
if result_region is None:
    raise LayoutRecognitionError(
        "Official HONOR search did not render product results after click and Enter"
    )
```

实现的等待函数每轮先执行 `_raise_if_blocked(page)`，要求唯一可见结果区域和至少一张 `li.grid-items`；未就绪时 `wait_for_timeout(_HONOR_RENDER_INTERVAL_MS)`，最多 `_HONOR_RENDER_POLLS + 1` 次。函数不得 `goto()`、不得新建页面、不得滚动。

之后复用该 `result_region` 进入现有卡片/精确型号/详情 URL 代码；不修改 `_observe_loaded_honor_detail()` 及其调用的 SKU、价格、截图读取函数。

- [ ] **Step 4: 更新冻结护栏为“入口可改、详情不可改”**

将 `test_honor_official_modules_match_the_frozen_baseline` 改为对 `official.py` 中 `_observe_loaded_honor_detail` 至该类后续详情辅助方法的源文本做 SHA-256 断言，`honor.py` 保持完整文件摘要；另断言入口兼容层的测试名称和错误文本存在。先用错误摘要运行以收集实际值，再固定值。

- [ ] **Step 5: 运行荣耀合同和护栏测试**

Run: `.venv/bin/pytest tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py -q`

Expected: PASS；点击即出结果、延迟结果、仅 Enter 结果和既有详情价格/SKU 场景均通过。

- [ ] **Step 6: 提交入口修复**

```bash
git add src/quote_app/sites/official.py tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py
git commit -m "fix: wait for HONOR search results before detail"
```

### Task 3: 在现有详情成功链路上增加截图前目标滚动与稳定复核

**Files:**
- Modify: `src/quote_app/sites/official_overrides/honor.py`
- Modify: `src/quote_app/sites/official.py`
- Modify: `tests/contract/test_official_honor_live.py`
- Modify: `tests/regression/test_honor_official_baseline.py`

**Interfaces:**
- Consumes: 已验证 `HonorStableOffer`、已选 SKU 选项和详情页价格定位器。
- Produces: `HonorOfficialOverride.prepare_capture_view(page, task, offer) -> None`，在截图上下文建立前将与已验证 SKU 对应的价格/配置滚入视图并在下一次语义读取中重核，失败继续按既有 `LayoutRecognitionError` 处理。

- [ ] **Step 1: 写失败的受控滚动合同测试**

为 fixture 的非选项 locator 记录 `scroll_into_view_if_needed()` 调用，新增测试：

```python
def test_honor_live_scrolls_selected_configuration_and_bound_price_before_capture(
    official_case: Any,
) -> None:
    adapter, task, page = _live_case(official_case, html=_live_honor_html())

    adapter.observe(task, cast(Any, page))

    assert page.capture_view_scrolls == ["honor-version", "honor-color", "honor-price"]
```

- [ ] **Step 2: 运行测试，确认失败且既有默认 SKU 路径没有价格区滚动记录**

Run: `.venv/bin/pytest tests/contract/test_official_honor_live.py::test_honor_live_scrolls_selected_configuration_and_bound_price_before_capture -q`

Expected: FAIL。

- [ ] **Step 3: 以最小实现准备截图视图**

在荣耀详情完成 SKU 选择、地址水合和稳定报价读取后，调用新 override 方法：只滚动当前选中的版本、颜色和已绑定价格元素；用一次 `wait_for_timeout(_HONOR_RENDER_INTERVAL_MS)` 后重新读取 `_stable_honor_live_offer()` 并与首次 `HonorStableOffer` 全字段比较。变化时抛出既有语义状态改变错误。不得改变窗口、缩放、页面 URL 或打开新标签。

- [ ] **Step 4: 运行荣耀合同回归**

Run: `.venv/bin/pytest tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py -q`

Expected: PASS；详情、SKU、价格语义均未回归。

- [ ] **Step 5: 提交截图准备层**

```bash
git add src/quote_app/sites/official.py src/quote_app/sites/official_overrides/honor.py tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py
git commit -m "feat: stabilize HONOR capture view"
```

### Task 4: 保持 Mac 人工窗口布局并向用户显示可执行的预检结果

**Files:**
- Modify: `src/quote_app/services/readiness.py`
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_precheck.py`
- Modify: `tests/unit/test_app.py`

**Interfaces:**
- Consumes: `check_runtime_readiness(app_paths)` 和现有“检查截图权限”按钮。
- Produces: 预检文本明确说明“请手动调整 Chrome 窗口后再次检查；自动报价不会改变窗口位置或大小”；刷新按钮每次重新读取权限/浏览器占用状态。窗口尺寸不作为启动硬性条件，因为浏览器尚未启动且不同官网页面高度不同。

- [ ] **Step 1: 写失败的界面文案/刷新测试**

在 `tests/unit/test_app.py` 增加：

```python
def test_readiness_refresh_explains_manual_chrome_layout(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.readiness_checker = _ready_check

    app.check_readiness()

    assert "手动调整 Chrome 窗口" in app.statuses[-1]
    assert "不会改变窗口位置或大小" in app.statuses[-1]
```

- [ ] **Step 2: 运行测试，确认当前预检文案没有该承诺**

Run: `.venv/bin/pytest tests/unit/test_app.py::test_readiness_refresh_explains_manual_chrome_layout -q`

Expected: FAIL。

- [ ] **Step 3: 最小修改预检文案与按钮标签**

把按钮文本改为“刷新运行前检查”，保留权限、辅助功能、程序专用 Chrome 占用的真实检查。`check_readiness()` 的状态末尾附加一条仅 Mac beta 使用的布局提示；不在 `MacFormalCaptureRuntime`、Chromium sampler 或浏览器 launch 参数中加入尺寸/位置操作。

- [ ] **Step 4: 运行界面和运行时回归**

Run: `.venv/bin/pytest tests/unit/test_app.py tests/unit/test_macos_capture_runtime.py tests/unit/test_web_run_service.py -q`

Expected: PASS，尤其 `browser_startup_preflight() is None` 和 beta 保持窗口的现有断言。

- [ ] **Step 5: 提交预检澄清**

```bash
git add src/quote_app/services/readiness.py src/quote_app/app.py tests/unit/test_precheck.py tests/unit/test_app.py
git commit -m "feat: clarify manual Chrome layout preflight"
```

### Task 5: 成功后清理本次运行的处理中检查点

**Files:**
- Modify: `src/quote_app/services/incremental_publication.py`
- Modify: `src/quote_app/services/full_pipeline.py:183-225`
- Modify: `tests/integration/test_full_pipeline_fixture_sites.py`
- Modify: `tests/unit/test_incremental_publication.py`

**Interfaces:**
- Consumes: `IncrementalExcelPublisher.paths` 和最终 `WebToExcelResult.quote_path/report_path`。
- Produces: `IncrementalExcelPublisher.discard_checkpoints_after_final_output(final_quote_path, final_report_path) -> None`，仅在最终两文件存在且与当前运行路径匹配时删除 publisher 自己的两份 `-处理中.xlsx`。

- [ ] **Step 1: 写失败的成功清理集成测试**

将现有 `test_full_pipeline_replaces_one_partial_excel_pair_after_each_stage` 末尾断言替换为：

```python
assert result.quote_path.is_file()
assert result.report_path.is_file()
assert not list(request.paths.output_dir.glob("*终端供货价报价表-处理中.xlsx"))
assert not list(request.paths.output_dir.glob("*报价执行报告-处理中.xlsx"))
```

再新增异常 runner 测试，断言 pipeline 抛出时两个处理中检查点仍存在。

- [ ] **Step 2: 运行测试，确认成功路径仍保留处理中对**

Run: `.venv/bin/pytest tests/integration/test_full_pipeline_fixture_sites.py -k 'partial_excel_pair or preserves_partial' -q`

Expected: FAIL，成功路径当前明确保留一对文件。

- [ ] **Step 3: 实现成对且延迟的清理**

在 `run_full_pipeline()` 最终 `write_web_results_to_excel()` 成功返回后，调用 publisher 的清理方法。方法必须先验证最终两文件是常规文件，随后在已有 `_pair_process_lock` 中仅删除 `self._paths.quote_path` 和 `self._paths.report_path`；不得使用 glob 删除，不得在异常块或最终写入之前调用。

- [ ] **Step 4: 运行增量发布与完整管线回归**

Run: `.venv/bin/pytest tests/unit/test_incremental_publication.py tests/integration/test_full_pipeline_fixture_sites.py -q`

Expected: PASS；成功仅剩最终对，失败仍有检查点。

- [ ] **Step 5: 提交文件清理**

```bash
git add src/quote_app/services/incremental_publication.py src/quote_app/services/full_pipeline.py tests/unit/test_incremental_publication.py tests/integration/test_full_pipeline_fixture_sites.py
git commit -m "fix: remove completed pipeline checkpoints"
```

### Task 6: 构建与验证独立荣耀官网稳定恢复包

**Files:**
- Modify: `src/quote_app/app.py`（仅构建标签）
- Create: `docs/testing/2026-08-01-honor-stable-recovery-build.md`
- Create: `dist-honor-stable-recovery/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: Tasks 1--5 的源码与完整测试结果。
- Produces: 新的、独立的签名 macOS app；不覆盖 `dist-honor-official-baseline` 或历史恢复包。

- [ ] **Step 1: 写失败的构建标签测试**

在 `tests/unit/test_app.py` 断言构建标签包含 `荣耀官网稳定恢复版`、`仅官网` 和日期。

- [ ] **Step 2: 运行测试确认标签尚未更新**

Run: `.venv/bin/pytest tests/unit/test_app.py -k build_label -q`

Expected: FAIL。

- [ ] **Step 3: 更新标签并运行全量验证**

Run:

```bash
.venv/bin/pytest tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py tests/unit/test_app.py tests/unit/test_macos_capture_runtime.py tests/unit/test_incremental_publication.py tests/integration/test_full_pipeline_fixture_sites.py -q
.venv/bin/mypy src
.venv/bin/pytest -q
```

Expected: 所有命令退出码为 0。

- [ ] **Step 4: 构建、签名校验并记录证据**

使用现有 macOS 构建脚本创建 `dist-honor-stable-recovery/福建移动铺货报价助手.app`；执行 `codesign --verify --deep --strict --verbose=2`，记录主可执行文件 SHA-256、构建时间、测试命令和签名输出到构建记录。

- [ ] **Step 5: 提交标签与构建记录**

```bash
git add src/quote_app/app.py tests/unit/test_app.py docs/testing/2026-08-01-honor-stable-recovery-build.md
git commit -m "build: package HONOR stable recovery app"
```

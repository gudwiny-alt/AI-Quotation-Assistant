# 荣耀官网基线验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让产品经理能够只运行基础表中的全部荣耀行与荣耀官网，验收官网价格、截图和 Excel 写入，且京东、天猫不会被访问或计入本轮结果。

**Architecture:** 在 `FullPipelineRequest` 增加可选渠道范围；现有三表关联和网站任务创建完成后，立即过滤为官网任务，再交给持久化、运行、增量发布和 Excel 写入。荣耀官网适配器不改动；Tk 默认传递 `HONOR + official`，源码指纹测试阻止荣耀官网模块改动。

**Tech Stack:** Python、Tkinter、pytest、openpyxl、Playwright、PyInstaller、macOS codesign。

## Global Constraints

- 只处理 `HONOR` 与 `WebsiteChannel.OFFICIAL`；不得访问京东或天猫。
- 不修改 `src/quote_app/sites/official.py` 或 `src/quote_app/sites/official_overrides/honor.py`。
- 输出只含荣耀行；官网写入 `AK` 与 `AN`，未运行渠道的价格/截图列保持空白。
- 未计划的渠道不得出现在任务、浏览器事件、Excel 状态或执行报告统计中。
- 每项生产代码变更前先运行相应失败测试；不做无关重构。
- 历史包 `dist-honor-official-restore/福建移动铺货报价助手.app` 不覆盖、不删除。

---

### Task 0: 回退未发布的官网试验改动，建立可验证基线

**Files:**
- Modify: `src/quote_app/sites/official.py:114,677-775`
- Modify: `src/quote_app/services/web_run.py:1-345`
- Modify: `tests/contract/test_official_honor_live.py`
- Modify: `tests/conftest.py`
- Modify: `tests/unit/test_web_run_service.py`
- Create: `tests/regression/test_honor_official_baseline.py`

**Interfaces:**
- Consumes: 已签名的 `dist-honor-official-restore/福建移动铺货报价助手.app` 和当前未打包的诊断/Enter 回退试验改动。
- Produces: 与“官网入口恢复版”一致的官网源码参照；诊断与 Enter 回退不进入本次验收包。

- [ ] **Step 1: 写失败的冻结范围测试**

在 `tests/regression/test_honor_official_baseline.py` 先断言 `official.py` 不包含本次未发布试验的两个标记：

```python
payload = OFFICIAL_MODULE.read_text(encoding="utf-8")
assert "_HONOR_CLICK_RESULT_POLLS" not in payload
assert "did not render product results after Enter" not in payload
```

Run: `.venv/bin/pytest tests/regression/test_honor_official_baseline.py -q`

Expected: FAIL，因为当前工作区仍有未打包的 Enter 回退试验。

- [ ] **Step 2: 删除未发布试验，不改变已恢复的官网路径**

只删除 `_HONOR_CLICK_RESULT_POLLS`、`_wait_for_honor_result_cards()` 和 `_observe_honor_live()` 中的 Enter 回退分支；保留“官网入口恢复版”已有的 `uses_live_contract(...) or _wait_for_honor_live_contract(...)` 逻辑。删除只为该试验添加的 `press()` fixture 和合同测试，以及只为诊断包添加的 `web_run.py` 诊断回调和单元测试。

- [ ] **Step 3: 验证基线范围**

Run: `.venv/bin/pytest tests/regression/test_honor_official_baseline.py tests/contract/test_official_honor_live.py tests/unit/test_web_run_service.py -q`

Expected: PASS；不再有未发布 Enter/诊断逻辑，既有荣耀合同测试仍通过。

- [ ] **Step 4: 记录并固定官网源码摘要**

扩展同一测试，记录 `official.py` 和 `official_overrides/honor.py` 的 SHA-256 常量。先以错误值运行获得摘要，再固定真实值。

Run: `.venv/bin/pytest tests/regression/test_honor_official_baseline.py -q`

Expected: PASS；此后这两个文件任何变动都会阻断测试。

- [ ] **Step 5: 提交冻结基线**

```bash
git add src/quote_app/sites/official.py src/quote_app/services/web_run.py tests/contract/test_official_honor_live.py tests/conftest.py tests/unit/test_web_run_service.py tests/regression/test_honor_official_baseline.py
git commit -m "test: freeze HONOR official baseline"
```

---

### Task 1: 在完整管线加入显式官网渠道范围

**Files:**
- Modify: `src/quote_app/services/full_pipeline.py:43-210`
- Modify: `tests/integration/test_full_pipeline_fixture_sites.py`

**Interfaces:**
- Consumes: `FullPipelineRequest.selected_brand: str | None` 与 `TaskBuildResult.tasks`。
- Produces: `FullPipelineRequest.selected_channels: frozenset[WebsiteChannel] | None`；`None` 保持全部渠道，`frozenset({WebsiteChannel.OFFICIAL})` 只运行官网。

- [ ] **Step 1: 写失败的多行官网筛选测试**

```python
def test_honor_official_scope_runs_only_official_tasks_for_all_honor_rows(
    fixture_inputs: InputPaths, tmp_path: Path,
) -> None:
    seen: list[WebsiteTask] = []

    def runner(request: WebsiteRunRequest) -> WebsiteRunSummary:
        seen.extend(request.tasks)
        return _fixture_website_runner(request)

    result = run_full_pipeline(
        _request(
            fixture_inputs, tmp_path,
            selected_brand="HONOR",
            selected_channels=frozenset({WebsiteChannel.OFFICIAL}),
        ),
        website_runner=runner,
    )
    assert [row.material_code for row in result.rows] == ["HONOR-FIRST", "HONOR-SECOND"]
    assert {task.channel for task in seen} == {WebsiteChannel.OFFICIAL}
    assert len(seen) == 2
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/pytest tests/integration/test_full_pipeline_fixture_sites.py::test_honor_official_scope_runs_only_official_tasks_for_all_honor_rows -q`

Expected: FAIL，`FullPipelineRequest` 或 `_request` 尚不接受 `selected_channels`。

- [ ] **Step 3: 实现最小渠道筛选**

在 `FullPipelineRequest` 增加：

```python
selected_channels: frozenset[WebsiteChannel] | None = None
```

在 `run_full_pipeline()` 验证渠道集合成员，并在 `build_website_tasks()` 后筛选：

```python
tasks = _select_channel_tasks(task_build.tasks, request.selected_channels)

def _select_channel_tasks(
    tasks: tuple[WebsiteTask, ...],
    selected_channels: frozenset[WebsiteChannel] | None,
) -> tuple[WebsiteTask, ...]:
    if selected_channels is None:
        return tasks
    return tuple(task for task in tasks if task.channel in selected_channels)
```

将 `tasks`（不能再用 `task_build.tasks`）传给 `IncrementalExcelPublisher`、`WebsiteRunRequest`、保存结果/观察的加载函数和 `WebToExcelRequest`。

- [ ] **Step 4: 运行测试并确认通过**

Run: `.venv/bin/pytest tests/integration/test_full_pipeline_fixture_sites.py::test_honor_official_scope_runs_only_official_tasks_for_all_honor_rows -q`

Expected: PASS；运行器只收到两条 `official` 任务。

- [ ] **Step 5: 运行管线回归并提交**

Run: `.venv/bin/pytest tests/integration/test_full_pipeline_fixture_sites.py tests/unit/test_web_run_service.py -q`

Expected: PASS。

```bash
git add src/quote_app/services/full_pipeline.py tests/integration/test_full_pipeline_fixture_sites.py
git commit -m "feat: scope HONOR acceptance to official channel"
```

### Task 2: 提供默认官网验收模式并冻结官网模块

**Files:**
- Modify: `src/quote_app/app.py:35-90,350-540`
- Modify: `tests/unit/test_app.py:55-210`
- Create: `tests/regression/test_honor_official_baseline.py`

**Interfaces:**
- Consumes: `make_full_pipeline_request(..., selected_brand, selected_channels)`。
- Produces: 默认“荣耀官网验收（仅官网）”的 UI 请求和不可变的官网源码指纹。

- [ ] **Step 1: 写失败的 UI 请求测试**

```python
def test_desktop_request_defaults_to_honor_official_scope(tmp_path: Path) -> None:
    request = make_full_pipeline_request(
        paths=_paths(tmp_path), quote_month=QuoteMonth(2026, 8),
        app_paths=build_app_paths("Darwin", home=tmp_path / "user"),
        selected_brand="HONOR",
        selected_channels=frozenset({WebsiteChannel.OFFICIAL}),
    )
    assert request.selected_brand == "HONOR"
    assert request.selected_channels == frozenset({WebsiteChannel.OFFICIAL})
```

更新既有 `QuoteApp.run()` 测试，断言状态包含 `荣耀官网验收模式（仅官网）`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/pytest tests/unit/test_app.py::test_desktop_request_defaults_to_honor_official_scope -q`

Expected: FAIL，`make_full_pipeline_request` 尚未接收 `selected_channels`。

- [ ] **Step 3: 实现界面范围传递**

为 `make_full_pipeline_request()` 增加 `selected_channels` 参数。新增：

```python
def _selected_channels_from_mode(self) -> frozenset[WebsiteChannel] | None:
    if self.brand_mode_var.get() == "荣耀官网验收（仅官网）":
        return frozenset({WebsiteChannel.OFFICIAL})
    if self.brand_mode_var.get() == "荣耀全站闭环（官网、京东、天猫）":
        return None
    raise InputValidationError("当前版本仅支持荣耀官网验收或荣耀全站闭环")
```

组合框默认使用第一个选项；`run()` 同时读取品牌和渠道范围，状态显示“荣耀官网验收模式（仅官网）”。

- [ ] **Step 4: 写失败的冻结护栏测试**

创建 `tests/regression/test_honor_official_baseline.py`，读取以下 UTF-8 文件的 SHA-256：

```python
OFFICIAL_MODULE = ROOT / "src/quote_app/sites/official.py"
HONOR_OVERRIDE_MODULE = ROOT / "src/quote_app/sites/official_overrides/honor.py"
```

先写入故意错误的摘要值。

Run: `.venv/bin/pytest tests/regression/test_honor_official_baseline.py -q`

Expected: FAIL，输出实际摘要。

- [ ] **Step 5: 固定摘要并运行测试**

记录步骤 4 输出的两个摘要；不得修改被校验的两个官网文件。

Run: `.venv/bin/pytest tests/regression/test_honor_official_baseline.py tests/unit/test_app.py -q`

Expected: PASS；未来改动官网模块必然失败。

- [ ] **Step 6: 提交 UI 与冻结护栏**

```bash
git add src/quote_app/app.py tests/unit/test_app.py tests/regression/test_honor_official_baseline.py
git commit -m "feat: add frozen HONOR official acceptance mode"
```

### Task 3: 验证多行输出并生成独立验收包

**Files:**
- Modify: `tests/integration/test_full_pipeline_fixture_sites.py`
- Modify: `docs/testing/live-site-matrix.md`
- Create: `dist-honor-official-baseline/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: 已冻结的官网模块及 Task 1/2 的官网范围。
- Produces: 独立“荣耀官网验收版”安装包，不覆盖历史包。

- [ ] **Step 1: 写失败的两行官网 Excel 输出测试**

新增两条荣耀官网成功结果的 fixture runner，并断言：

```python
assert sheet["AK2"].value == 4499
assert sheet["AK3"].value == 4599
assert len(sheet._images) == 2
assert {image_anchor(image) for image in sheet._images} == {"AN2", "AN3"}
assert sheet["AI2"].value is None
assert sheet["AJ2"].value is None
assert result.summary.completed_rows == 2
assert result.summary.failed_rows == 0
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/pytest tests/integration/test_full_pipeline_fixture_sites.py::test_honor_official_scope_writes_multiple_prices_and_images -q`

Expected: FAIL，测试尚未实现或非官网渠道仍参与。

- [ ] **Step 3: 只修正任务边界遗漏**

若失败，检查筛选后的 `tasks` 是否仍有 `jd`/`tmall` 传入发布器或最终写入器；只修正该数据流边界，绝不修改荣耀官网文件。

- [ ] **Step 4: 运行正式回归与类型检查**

Run: `.venv/bin/pytest tests/regression/test_honor_official_baseline.py tests/unit/test_app.py tests/integration/test_full_pipeline_fixture_sites.py tests/integration/test_macos_web_to_excel.py tests/unit/test_web_run_service.py -q`

Expected: PASS。

Run: `.venv/bin/mypy src/quote_app/app.py src/quote_app/services/full_pipeline.py`

Expected: `Success: no issues found`。

- [ ] **Step 5: 生成独立包并校验签名**

Run: `.venv/bin/pyinstaller --noconfirm --clean --distpath dist-honor-official-baseline --workpath build-honor-official-baseline packaging/quotation_app.spec`

Run: `codesign --force --deep --sign - "dist-honor-official-baseline/福建移动铺货报价助手.app" && codesign --verify --deep --strict --verbose=2 "dist-honor-official-baseline/福建移动铺货报价助手.app"`

Expected: 签名验证通过，`dist-honor-official-restore` 未被改动。

- [ ] **Step 6: 记录构建信息并提交**

在 `docs/testing/live-site-matrix.md` 记录构建路径、官网范围、冻结摘要及“等待真实多条荣耀验收”；不得声称真实网站已通过。

```bash
git add docs/testing/live-site-matrix.md
git commit -m "build: package HONOR official baseline acceptance app"
```

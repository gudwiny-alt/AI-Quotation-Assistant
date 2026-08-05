# 京东无报价截图与天猫报价校验最小修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复京东无报价页搜索框短暂为空导致的截图失败，并让天猫报价不再被库存或收货地区阻断，同时保持所有既有成功路径不变。

**Architecture:** 京东只在正式截图阶段增加一个有上限的可见搜索词稳定等待器，不重新导航或搜索。天猫从价格稳定性证据中移除库存和地区，只以受控详情页、官方店铺、型号、内存容量、颜色和价格构造稳定报价状态；使用稳定的“报价政策不要求”语义标记承接现有 `VerifiedSemanticState` 接口，不修改全局状态模型和其他站点。

**Tech Stack:** Python 3.12、pytest、Playwright 页面协议、PyInstaller、macOS codesign。

## Global Constraints

- 只修改京东截图等待和天猫价格稳定性门槛，不修改荣耀官网或 Excel 写入逻辑。
- 京东截图仍必须实际显示目标搜索词；不得只凭 URL 截图，不得自动重新提交搜索。
- 天猫价格成功必须保留受控详情页、官方店铺、匹配型号、目标内存容量、目标颜色和有效价格。
- 天猫库存与收货地区不是报价字段，不得阻断取价或截图。
- 京东保持全程固定 80%；天猫只在正式判断与截图阶段切换 80%。
- 既有成功结果不得被失败重试覆盖。

---

### Task 1: 京东无报价截图等待稳定搜索词

**Files:**
- Modify: `tests/contract/test_jd_adapter.py`
- Modify: `src/quote_app/sites/jd.py`

**Interfaces:**
- Consumes: `JDAdapter._validated_result_search_input(page, model_name) -> Any | None`
- Produces: `JDAdapter._wait_for_capture_search_input(page, model_name) -> Any | None`

- [ ] **Step 1: 写入失败测试和最小页面时序夹具**

在 `_FixturePage` 增加仅用于模拟真实懒加载的 `capture_search_value_ready_after` 参数；
`wait_for_timeout` 到达计数后，把结果页可见店内搜索框的 `value` 设为目标词。新增测试：

```python
def test_jd_no_model_capture_waits_for_matching_search_input_to_stabilize() -> None:
    html = _no_model_html_with_blank_result_search_input()
    page = _FixturePage(
        html=html,
        capture_search_value_ready_after=2,
        capture_search_value="小米 15",
    )
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.NO_MODEL
    assert page.capture_view_positions == ["search"]
    assert page.wait_timeout_milliseconds.count(250) >= 1
```

保留或新增反例，确保搜索框始终为空时仍抛出 `CaptureViewGeometryError`，安全阶段为
`搜索框定位`。

- [ ] **Step 2: 运行测试并确认旧代码按预期失败**

Run:

```bash
.venv/bin/pytest tests/contract/test_jd_adapter.py::test_jd_no_model_capture_waits_for_matching_search_input_to_stabilize -q
```

Expected: FAIL，旧实现第一次读取空搜索框后即报 `JD no-model search keyword is not visible for capture`。

- [ ] **Step 3: 实现有限稳定等待**

在 `JDAdapter` 内增加：

```python
def _wait_for_capture_search_input(self, page: Any, model_name: str) -> Any | None:
    for attempt in range(_MAX_VERIFIED_STATE_POLLS):
        search_input = self._validated_result_search_input(page, model_name)
        if search_input is not None:
            return search_input
        if attempt + 1 < _MAX_VERIFIED_STATE_POLLS:
            page.wait_for_timeout(_VERIFIED_STATE_INTERVAL_MS)
    return None
```

仅在 `prepare_capture_view` 的 `NO_MODEL` 分支和正式截图矩形读取中调用该等待器。
非空但错误的搜索词继续由 `_validated_result_search_input` 立即拒绝；不调用 `fill`、
`press`、`click` 或 `goto`。

- [ ] **Step 4: 运行京东契约测试**

Run:

```bash
.venv/bin/pytest tests/contract/test_jd_adapter.py -q
```

Expected: PASS，包括京东 Power2、固定 80%、无报价合法页和错误关键词拒绝测试。

- [ ] **Step 5: 提交京东最小修复**

```bash
git add src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git commit -m "fix: wait for stable JD no-model search evidence"
```

---

### Task 2: 天猫报价忽略库存与收货地区

**Files:**
- Modify: `tests/contract/test_tmall_adapter.py`
- Modify: `src/quote_app/sites/tmall.py`

**Interfaces:**
- Consumes: 已选配置 `tuple[str, str]`、匹配标题和 `tuple[PriceCandidate, ...]`
- Produces: `_stable_visible_price(page, task, configuration) -> Decimal`

- [ ] **Step 1: 写入失败测试并更新已变更的旧规则测试**

新增一个基于 `normal.html` 的测试，同时移除收货地区并移除库存节点：

```python
def test_tmall_price_and_capture_do_not_require_stock_or_delivery_region() -> None:
    html = _normal_html_without_stock_or_delivery_region()
    page = _FixturePage(html=html)
    task = _task()
    adapter = TmallAdapter(_xiaomi_spec())

    observation = adapter.observe(task, cast(Any, page))
    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4299")
    assert page.capture_scales == [0.8]
    assert page.capture_view_positions == ["capacity"]
```

把旧的“库存或地区缺失/重复必须失败”测试改为“不会改变价格结果”；继续保留型号、
内存、颜色、价格缺失或冲突必须失败的测试。

- [ ] **Step 2: 运行测试并确认旧代码按预期失败**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py::test_tmall_price_and_capture_do_not_require_stock_or_delivery_region -q
```

Expected: FAIL，旧实现报 `stock state` 或 `delivery region` 缺失。

- [ ] **Step 3: 最小化天猫稳定状态**

在 `tmall.py` 定义固定策略标记：

```python
_TMALL_QUOTATION_REGION = "not-required-for-quotation"
_TMALL_QUOTATION_STOCK = "not-required-for-quotation"
```

从 `TmallVisibleConfigurationEvidence` 删除 `stock`；删除报价路径对
`_visible_stock_sample` 的调用，使 `_stable_visible_price` 只返回 `Decimal`。在
`_observe_detail`、`verified_state_reader` 和 `_prepare_capture_view_at_scale` 构造语义状态时
使用上述稳定标记。`NO_MODEL` 等合法无报价路径继续使用现有 `not-applicable`，不改变。

- [ ] **Step 4: 运行天猫契约测试**

Run:

```bash
.venv/bin/pytest tests/contract/test_tmall_adapter.py -q
```

Expected: PASS，包括天猫 Power2 同类详情页、80%截图、错误型号/配置/价格拒绝和畅玩80
无报价路径。

- [ ] **Step 5: 提交天猫最小修复**

```bash
git add src/quote_app/sites/tmall.py tests/contract/test_tmall_adapter.py
git commit -m "fix: limit Tmall quotation proof to required offer fields"
```

---

### Task 3: 冻结回归、版本标识与 Mac 测试包

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-marketplace-minimal-gates-44/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的适配器行为
- Produces: `.43` 的后继独立测试包 `.44`，不覆盖任何旧包

- [ ] **Step 1: 先更新版本标识测试并确认失败**

将期望版本设为：

```python
"京东无报价截图与天猫必要字段校验版（全部荣耀行）2026.08.05.44"
```

Run:

```bash
.venv/bin/pytest tests/unit/test_app.py -q
```

Expected: FAIL，应用仍显示 `.43`。

- [ ] **Step 2: 只修改 `APP_BUILD_LABEL` 并确认应用测试通过**

Run:

```bash
.venv/bin/pytest tests/unit/test_app.py -q
```

Expected: PASS。

- [ ] **Step 3: 运行冻结回归矩阵**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_jd_adapter.py \
  tests/contract/test_tmall_adapter.py \
  tests/regression/test_honor_official_baseline.py \
  tests/integration/test_web_to_excel.py \
  tests/unit/test_macos_capture_runtime.py -q
```

Expected: PASS，确认官网、京东 Power2、天猫畅玩80及 Excel 增量写入不回退。

- [ ] **Step 4: 运行完整测试与代码检查**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

若唯一失败仍为沙箱禁止 `127.0.0.1` 绑定，则仅在获批后于沙箱外单独复核：

```bash
.venv/bin/pytest tests/integration/test_persistent_browser.py::test_cookie_and_local_storage_survive_close_and_reopen -q
```

- [ ] **Step 5: 提交版本标识并构建独立安装包**

```bash
git add src/quote_app/app.py tests/unit/test_app.py
git commit -m "chore: label minimal marketplace gate build"
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-marketplace-minimal-gates-44 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-marketplace-minimal-gates-44 \
  --workpath build-marketplace-minimal-gates-44 \
  packaging/quotation_app.spec
codesign --force --deep --sign - \
  'dist-marketplace-minimal-gates-44/福建移动铺货报价助手.app'
codesign --verify --deep --strict --verbose=2 \
  'dist-marketplace-minimal-gates-44/福建移动铺货报价助手.app'
```

- [ ] **Step 6: 校验安装包源码新鲜度**

打开应用可执行文件的 PyInstaller `CArchive` 与 `PYZ.pyz`，抽取
`quote_app.app`、`quote_app.sites.jd`、`quote_app.sites.tmall`，递归标准化
`co_filename` 后与当前源码 `compile(..., optimize=0)` 的 marshal SHA-256 比较。
Expected: 三个模块均 `match=True`，最终 `ALL_MATCH=True`。

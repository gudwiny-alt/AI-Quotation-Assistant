# Mac `.167` Hotfix and Native Chrome Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `.167` 最近一次运行暴露的京东截图、华为取价、小米无结果截图和 Excel 图片锚定问题，并让官网、天猫、京东分别使用独立普通 Chrome 会话。

**Architecture:** 保留全部站点适配器接口和任务调度接口，只在现有 Mac 截图预检、三个局部数据处理点和浏览器阶段编排处做最小修改。先让京东 Native Chrome 的窗口进入安全工作区，再把同一会话类型扩展到天猫和官网；每个渠道仍串行执行并使用独立用户目录。

**Tech Stack:** Python 3.11、Playwright sync API、Chrome CDP、openpyxl、pytest、macOS Quartz/Accessibility bridge。

**Spec:** `docs/superpowers/specs/2026-09-03-mac-167-hotfix-and-native-chrome-isolation-design.md`

## Global Constraints

- 基线必须是 `v0.167-stable`；不得修改或移动该标签。
- 不修改 Windows 分支。
- 不修改已通过品牌的搜索、详情页、颜色、容量和价格业务规则。
- 每项生产代码修改前必须先新增并运行一个会因缺失该行为而失败的测试。
- 截图成功后不得因为后置取价失败而删除截图。
- 网站顺序保持“品牌官网 → 天猫 → 京东”。

---

### Task 1: 京东 Native Chrome 安全窗口几何

**Files:**
- Modify: `src/quote_app/evidence/macos_runtime.py`
- Modify: `src/quote_app/browser/session.py`
- Test: `tests/unit/test_macos_capture_runtime.py`
- Test: `tests/integration/test_persistent_browser.py`

**Interfaces:**
- Consumes: `MacFormalCaptureRuntime.browser_launch_args()` 以及带 `startup_preflight` 参数的 `NativeChromeCdpSession`。
- Produces: `MacFormalCaptureRuntime.browser_startup_preflight() -> Callable[[Any], None] | None`，在视觉复核模式下把普通 Chrome 窗口归一化到安全工作区。

- [ ] **Step 1: 写失败测试，证明过高的固定窗口参数必须收敛到安全尺寸**

在 `tests/unit/test_macos_capture_runtime.py` 新增：

```python
def test_darwin_beta_provides_idempotent_safe_window_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    context = _WindowPreflightContext(
        work_area={"left": 0, "top": 33, "width": 1470, "height": 863},
        window={"left": 6, "top": 33, "width": 1464, "height": 863},
    )
    runtime = MacFormalCaptureRuntime(
        policy=MacCapturePolicy.MAC_VISUAL_REVIEW_BETA,
    )

    preflight = runtime.browser_startup_preflight()
    assert preflight is not None
    preflight(context)
    preflight(context)

    assert context.applied_bounds == [
        {"left": 24, "top": 49, "width": 1422, "height": 823}
    ]
```

- [ ] **Step 2: 运行测试并确认失败原因是预检仍为 `None`**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/unit/test_macos_capture_runtime.py::test_darwin_beta_provides_idempotent_safe_window_preflight`

Expected: FAIL，`preflight is None`。

- [ ] **Step 3: 实现最小安全窗口预检**

在 `macos_runtime.py` 增加只依赖 Playwright `BrowserContext` 的预检回调。回调通过第一个可见页面的 CDP `Browser.getWindowForTarget`、`Browser.getWindowBounds` 和 `Browser.setWindowBounds` 读取/写入普通窗口；安全矩形使用工作区四边至少 24 DIP 内边距，顶部至少 49 DIP，宽高不得超过可用工作区。当前矩形已安全时不重复写入。

在 `session.py` 保持 `startup_preflight` 在 CDP 连接成功、页面使用前调用，并在正式截图绑定窗口前复用该幂等回调。

- [ ] **Step 4: 增加 Native Chrome 连接后预检顺序测试**

在 `tests/integration/test_persistent_browser.py` 断言生命周期为：启动普通 Chrome → CDP 连接 → `startup_preflight(context)` → 页面使用；预检失败时关闭浏览器、进程和 profile lock。

- [ ] **Step 5: 运行京东几何和会话测试**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/unit/test_macos_capture_runtime.py tests/integration/test_persistent_browser.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/quote_app/evidence/macos_runtime.py src/quote_app/browser/session.py tests/unit/test_macos_capture_runtime.py tests/integration/test_persistent_browser.py
git commit -m "fix: normalize native Chrome capture bounds"
```

### Task 2: 华为官网只读取主商品价格

**Files:**
- Modify: `src/quote_app/sites/official_brands/huawei.py`
- Test: `tests/contract/test_official_huawei_live.py`

**Interfaces:**
- Consumes: `_PRICE_STYLE` 已返回的 `primaryDetailRoot: bool`。
- Produces: `_current_price(page) -> tuple[Decimal, Any]` 只接受当前详情主商品根节点的价格。

- [ ] **Step 1: 写失败测试，推荐区 `199` 不能抢在主商品 `2199` 前**

```python
def test_huawei_post_capture_ignores_recommendation_price_before_main_price() -> None:
    adapter = _adapter()
    task = _task(model_name="华为畅享 90 Pro Max", storage="256GB")
    page = _HuaweiPage()
    page.prepend_auxiliary_product_price("¥199")
    page.set_main_product_price("¥2199")
    observation = adapter.observe_for_capture(task, page)

    completed = adapter.finalize_observation(task, page, observation)

    assert completed.price == Decimal("2199")
```

- [ ] **Step 2: 运行并确认当前实现错误返回 `199`**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/contract/test_official_huawei_live.py::test_huawei_post_capture_ignores_recommendation_price_before_main_price`

Expected: FAIL，得到 `Decimal("199")`。

- [ ] **Step 3: 最小修改价格候选过滤**

在 `_current_price.accepted()` 增加 `require_primary_detail_root` 参数。所有正式候选和回退候选均要求 `_PRICE_STYLE["primaryDetailRoot"] is True`；不得在主商品价格缺失时接受其他 `[data-prdid]` 的金额。保留现有划线价、上下文、颜色和叶节点过滤。

- [ ] **Step 4: 写失败测试，主商品价格缺失时截图结果仍保留**

```python
def test_huawei_recommendation_only_price_is_not_used_after_capture() -> None:
    adapter = _adapter()
    page = _HuaweiPage()
    page.hide_main_product_prices()
    page.prepend_auxiliary_product_price("¥199")
    observation = adapter.observe_for_capture(_task(), page)

    with pytest.raises(NonRetryableTechnicalError) as captured:
        adapter.finalize_observation(_task(), page, observation)

    assert captured.value.code == "PRICE_UNAVAILABLE_AFTER_CAPTURE"
```

- [ ] **Step 5: 运行华为合同与完整官方站点测试**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/contract/test_official_huawei_live.py tests/integration/test_huawei_official_pipeline.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/quote_app/sites/official_brands/huawei.py tests/contract/test_official_huawei_live.py
git commit -m "fix: read Huawei price from primary detail product"
```

### Task 3: 小米无结果截图填写任务型号

**Files:**
- Modify: `src/quote_app/sites/official_brands/xiaomi.py`
- Test: `tests/integration/test_xiaomi_official_pipeline.py`

**Interfaces:**
- Consumes: `WebsiteTask.model_name`、当前已接受的 `VerifiedSemanticState.NO_MODEL`。
- Produces: 截图准备后的可见搜索框值、重新计算的 `search_keyword` 与 `result_region` 矩形。

- [ ] **Step 1: 写失败测试，URL 正确但可见搜索框是推荐词**

```python
def test_xiaomi_no_model_capture_rewrites_visible_search_box_without_resubmit() -> None:
    task = _task("xiaomi-empty", model="REDMI R70 5G")
    page = _XiaomiFixturePage("no_model.html")
    page.goto("https://www.mi.com/shop/search?keyword=REDMI%20R70%205G")
    page.visible_search_input().fill("REDMI K90至尊版")
    adapter = _adapter()
    observed = adapter.observe(task, page)

    adapter.prepare_capture_view(task, page, observed.semantic_state)

    assert page.visible_search_input().input_value() == "REDMI R70 5G"
    assert page.search_submissions == 1
    assert tuple(r.role for r in adapter.capture_rectangles_for_capture(
        task, page, observed.semantic_state
    )) == ("search_keyword", "result_region")
```

- [ ] **Step 2: 运行并确认测试因搜索框未改写而失败**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/integration/test_xiaomi_official_pipeline.py::test_xiaomi_no_model_capture_rewrites_visible_search_box_without_resubmit`

Expected: FAIL，输入值仍为站点推荐词。

- [ ] **Step 3: 实现无导航的搜索框填充**

在 `prepare_capture_view()` 的 `NO_MODEL` 分支中重新定位 `_SEARCH_KEYWORD` 第一个可见输入框，调用 `fill(task.model_name)`，校验 `input_value().strip() == task.model_name`。重新调用 `_no_model_proof_locators()` 获取当前矩形并写入 `_prepared_rectangles`。不得按回车、点击按钮或调用 `goto()`。

- [ ] **Step 4: 运行小米合同和证据质量测试**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/integration/test_xiaomi_official_pipeline.py tests/unit/test_evidence_quality.py`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/quote_app/sites/official_brands/xiaomi.py tests/integration/test_xiaomi_official_pipeline.py
git commit -m "fix: show Xiaomi task model in empty-result capture"
```

### Task 4: Excel 图片使用双边界单元格锚点

**Files:**
- Modify: `src/quote_app/excel/quote_writer.py`
- Test: `tests/unit/test_quote_writer.py`

**Interfaces:**
- Consumes: `QuoteEvidenceImage(anchor: str, payload: bytes)`。
- Produces: openpyxl `TwoCellAnchor(editAs="twoCell")`，从目标单元格左上角到下一列、下一行的左上角。

- [ ] **Step 1: 写失败测试，保存并重开后必须是双边界锚点**

```python
def test_writer_binds_evidence_image_to_entire_target_cell(tmp_path: Path) -> None:
    source = BytesIO()
    Image.new("RGB", (1512, 982), (34, 48, 71)).save(source, format="PNG")
    output = write_quote_workbook(QuoteWriteRequest(
        quote_month=QuoteMonth(2026, 8),
        rows=_rows(),
        template_path=TEMPLATE_PATH,
        output_dir=tmp_path,
        evidence_images=(QuoteEvidenceImage(anchor="AL2", payload=source.getvalue()),),
    ))
    workbook = load_workbook(output)
    image = workbook["5G手机"]._images[0]

    assert isinstance(image.anchor, TwoCellAnchor)
    assert image.anchor.editAs == "twoCell"
    assert (image.anchor._from.col, image.anchor._from.row) == (37, 1)
    assert (image.anchor.to.col, image.anchor.to.row) == (38, 2)
```

- [ ] **Step 2: 运行并确认当前 `OneCellAnchor` 失败**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/unit/test_quote_writer.py::test_writer_binds_evidence_image_to_entire_target_cell`

Expected: FAIL，实际锚点为 `OneCellAnchor`。

- [ ] **Step 3: 实现 `TwoCellAnchor`**

在 `quote_writer.py` 引入：

```python
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
from openpyxl.utils.cell import column_index_from_string
```

解析目标列和行后设置：

```python
zero_based_column = column_index_from_string(column_letter) - 1
zero_based_row = row_number - 1
image.anchor = TwoCellAnchor(
    editAs="twoCell",
    _from=AnchorMarker(col=zero_based_column, row=zero_based_row),
    to=AnchorMarker(col=zero_based_column + 1, row=zero_based_row + 1),
)
```

继续设置统一列宽和行高，不再把图片压缩为固定像素 `ext`。

- [ ] **Step 4: 运行 Excel 写入与 Web-to-Excel 集成测试**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/unit/test_quote_writer.py tests/integration/test_web_to_excel.py tests/integration/test_macos_web_to_excel.py`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/quote_app/excel/quote_writer.py tests/unit/test_quote_writer.py
git commit -m "feat: bind evidence images to Excel cells"
```

### Task 5: 官网、天猫、京东三阶段独立 Native Chrome

**Files:**
- Modify: `src/quote_app/browser/session.py`
- Modify: `src/quote_app/services/web_run.py`
- Modify: `src/quote_app/browser/login.py`
- Test: `tests/unit/test_web_run_service.py`
- Test: `tests/integration/test_persistent_browser.py`
- Test: `tests/unit/test_login_browser.py`

**Interfaces:**
- Consumes: `NativeChromeCdpSession`、`WebsiteChannel.OFFICIAL/TMALL/JD`。
- Produces: `official_profile_dir_for(profile_dir: Path) -> Path` 和三个串行 Native Chrome 阶段。

- [ ] **Step 1: 写失败测试，三个渠道必须各自进入 Native Chrome 阶段**

```python
def test_service_runs_three_channels_in_isolated_native_chrome_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tasks = (
        _task("jd", channel=WebsiteChannel.JD),
        _task("tmall", channel=WebsiteChannel.TMALL),
        _task("official", channel=WebsiteChannel.OFFICIAL),
    )
    # Fake NativeChromeCdpSession records profile_dir and phase task IDs.
    summary = web_run.run_website_tasks(_request(tmp_path, tasks))

    assert opened_profiles == [
        tmp_path / "profile-official",
        tmp_path / "profile",
        tmp_path / "profile-jd",
    ]
    assert phase_tasks == [("official",), ("tmall",), ("jd",)]
    assert summary.succeeded == 3
```

- [ ] **Step 2: 运行并确认当前官网与天猫共用 `PersistentBrowserSession`**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/unit/test_web_run_service.py::test_service_runs_three_channels_in_isolated_native_chrome_sessions`

Expected: FAIL，当前阶段为 `("official", "tmall")` 和 `("jd",)`。

- [ ] **Step 3: 增加官网 profile 路径函数**

在 `session.py` 增加：

```python
def official_profile_dir_for(profile_dir: Path) -> Path:
    normalized = Path(profile_dir).expanduser().resolve()
    return normalized.with_name(f"{normalized.name}-official")
```

保留现有 `profile_dir` 给天猫，保留 `jd_profile_dir_for()` 给京东。

- [ ] **Step 4: 重写阶段编排但不改任务排序和 runner**

在 `run_website_tasks()` 中按 `WebsiteChannel.OFFICIAL`、`TMALL`、`JD` 分成三个任务元组；非空阶段均通过 `_browser_session_for_runtime` 创建 `NativeChromeCdpSession`。阶段列表顺序固定为官网、天猫、京东。`_run_browser_phase()`、人工继续机制和每任务失败隔离保持原样。

- [ ] **Step 5: 更新首次登录入口**

`open_login_browser()` 继续为天猫使用 `profile_dir`，为京东使用 `profile-jd`；官网没有登录要求，不主动打开登录页，但通过 `prepare_dedicated_profile(official_profile_dir_for(profile_dir))` 预建合法 profile marker。

- [ ] **Step 6: 写失败隔离测试**

```python
def test_native_channel_task_failure_does_not_skip_later_native_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    official = _task("official", channel=WebsiteChannel.OFFICIAL)
    tmall = _task("tmall", channel=WebsiteChannel.TMALL)
    jd = _task("jd", channel=WebsiteChannel.JD)
    entered_phases: list[str] = []
    phase_results = {
        "official": _failure(official, "CAPTURE_GEOMETRY"),
        "tmall": _success(tmall, _evidence(tmp_path / "tmall.png", b"tmall")),
        "jd": _success(jd, _evidence(tmp_path / "jd.png", b"jd")),
    }
    # Reuse the file's Repository/Runner fakes; Runner records the one channel
    # received in each phase and returns the result above.
    monkeypatch.setattr(web_run, "NativeChromeCdpSession", RecordingNativeBrowser)
    monkeypatch.setattr(web_run, "WebsiteTaskRunner", RecordingPhaseRunner)

    summary = web_run.run_website_tasks(
        _request(tmp_path, (official, tmall, jd)),
        runtime_factory=lambda _registry: runtime,
    )

    assert entered_phases == ["official", "tmall", "jd"]
    assert summary.technical_failure == 1
    assert summary.succeeded == 2
```

若现有服务级异常模型要求“阶段启动失败”上抛，则在服务层把该阶段所有任务记录为同一技术失败后继续；不得吞掉 `KeyboardInterrupt`、`SystemExit` 或用户取消。

- [ ] **Step 7: 运行浏览器、服务、登录与调度测试**

Run: `../ui-run-scope/.venv/bin/pytest -q tests/unit/test_web_run_service.py tests/integration/test_persistent_browser.py tests/unit/test_login_browser.py tests/unit/test_scheduler.py`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add src/quote_app/browser/session.py src/quote_app/services/web_run.py src/quote_app/browser/login.py tests/unit/test_web_run_service.py tests/integration/test_persistent_browser.py tests/unit/test_login_browser.py
git commit -m "feat: isolate website channels in native Chrome sessions"
```

### Task 6: 回归、打包和稳定检查点

**Files:**
- Modify only if required by version packaging: `src/quote_app/__init__.py`
- Verify: entire repository

**Interfaces:**
- Consumes: Tasks 1–5 commits。
- Produces: 可供用户复测的独立 Mac `.app`，以及新的候选 Git 检查点；不覆盖 `.167`。

- [ ] **Step 1: 运行针对性矩阵**

Run:

```bash
../ui-run-scope/.venv/bin/pytest -q \
  tests/unit/test_macos_capture_runtime.py \
  tests/integration/test_persistent_browser.py \
  tests/unit/test_web_run_service.py \
  tests/contract/test_official_huawei_live.py \
  tests/integration/test_huawei_official_pipeline.py \
  tests/integration/test_xiaomi_official_pipeline.py \
  tests/unit/test_quote_writer.py \
  tests/integration/test_web_to_excel.py \
  tests/integration/test_macos_web_to_excel.py
```

Expected: PASS。

- [ ] **Step 2: 运行完整测试、静态检查和冻结矩阵**

Run: `../ui-run-scope/.venv/bin/pytest -q`

Run: `../ui-run-scope/.venv/bin/ruff check src tests`

Run: `../ui-run-scope/.venv/bin/mypy src`

Expected: 全部通过。

- [ ] **Step 3: 构建独立候选包**

使用仓库现有 macOS 正式打包脚本，输出到新的候选目录，不覆盖 `.167` 分发目录。完成代码签名和严格校验，并记录 `.app` 的绝对路径与 SHA-256。

- [ ] **Step 4: 提交候选检查点**

```bash
git status --short
git log --oneline --decorate -8
```

工作树必须干净。用户实测通过前不创建新的 `stable` 标签；实测通过后再按用户确认建立新的稳定标签。

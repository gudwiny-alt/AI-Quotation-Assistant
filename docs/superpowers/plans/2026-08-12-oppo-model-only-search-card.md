# OPPO 官网按机型进入商品卡 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使 OPPO A5m、A6t 在搜索结果卡不包含目标颜色或容量时，仍能按精确基础机型进入第一张合法官方详情卡，并在详情页选中目标容量和颜色后取价、截图。

**Architecture:** 保留现有 OPPO 真实全屏搜索弹层、安全 URL 校验、详情页配置选择和四证据同屏截图流程。仅将 `_preferred_exact_result_link()` 从“容量/颜色加权”改为“精确基础机型 + 合法详情 URL + 页面顺序”；详情页仍严格选择基础表容量和颜色。截图阶段验证已稳定的 80% 单次缩放/最多一次定向定位，不重写共享截图基础设施。

**Tech Stack:** Python 3.12, synchronous Playwright-compatible adapter API, pytest, openpyxl integration tests, PyInstaller macOS app packaging, `codesign`.

## Global Constraints

- 仅修改 OPPO 官网适配器、OPPO fixture/契约测试和 OPPO 管线集成测试。
- 不修改荣耀、小米、京东、天猫、Excel 或其他品牌的生产代码。
- 搜索结果阶段不用容量、颜色或库存筛选/排序；只允许精确基础机型。
- 相似机型（如 A6 Pro、A6i、A6k、A6m、A5m Pro）必须继续被拒绝。
- 详情页必须严格选择基础表目标容量和颜色；目标缺失时保留现有合法“无容量/无颜色”逻辑。
- 正常价截图只要产品名称、官网价格、目标容量、目标颜色四项同屏；不增加库存、地区、套餐条件。
- 如已经是 80% 不重复缩放；四项已同屏不滚动，仅不同屏时最多定向定位一次。
- Mac 真站验收前不宣称 OPPO 官网已冻结。

---

### Task 1: 用 A5m/A6t 失败契约锁定“搜索只看机型”

**Files:**
- Modify: `tests/contract/test_official_oppo_live.py:119-198`

**Interfaces:**
- Consumes: `OppoOfficialAdapter.observe(task: WebsiteTask, page: BrowserPage) -> AdapterObservation`
- Produces: `_oppo_task(model_name: str, ram: str, storage: str, color: str) -> WebsiteTask` 测试辅助函数，以及两个真实失败场景契约。

- [ ] **Step 1: 将契约任务工厂参数化**

```python
def _oppo_task(
    *,
    model_name: str,
    ram: str,
    storage: str,
    color: str,
) -> WebsiteTask:
    return WebsiteTask(
        task_id=f"oppo-{model_name}-{ram}-{storage}-{color}",
        run_id="run-oppo-model-only",
        source_row_number=2,
        output_row_number=2,
        material_code="OPPO-MODEL-ONLY",
        brand="欧珀",
        model_name=model_name,
        ram=ram,
        storage=storage,
        color=color,
        channel=WebsiteChannel.OFFICIAL,
    )


def _task() -> WebsiteTask:
    return _oppo_task(
        model_name="OPPO A6 5G",
        ram="12GB",
        storage="256GB",
        color="蓝海浮光",
    )
```

- [ ] **Step 2: 新增可重复配置搜索卡和详情配置的 fixture 辅助函数**

```python
def _set_result_cards(
    page: _OppoFixturePage,
    cards: tuple[tuple[str, str], ...],
) -> None:
    region = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "results"
    )
    region.children.clear()
    page._product_urls.clear()
    for title, href in cards:
        article = _OfficialNode("article", {"class": "five-item goods-card"}, region)
        link = _OfficialNode(
            "a",
            {"data-oppo-role": "product-link", "class": "goods-card app-card-hover", "href": href},
            article,
        )
        label = _OfficialNode("span", {"data-oppo-role": "product-title"}, link)
        label.text_parts = [title]
        link.children.append(label)
        article.children.append(link)
        region.children.append(article)
        page._product_urls.add(f"https://www.opposhop.cn{href}")


def _set_detail_product(
    page: _OppoFixturePage,
    *,
    model_name: str,
    capacity: str,
    color: str,
) -> None:
    detail_title = next(
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "detail-title"
    )
    detail_title.text_parts = [f"{model_name} {color} {capacity} 官方标配"]
    capacity_options = [
        node for node in page.root.descendants()
        if node.attrs.get("data-option-kind") == "capacity"
    ]
    color_options = [
        node for node in page.root.descendants()
        if node.attrs.get("data-option-kind") == "color"
    ]
    capacity_options[0].text_parts = [capacity]
    color_options[0].text_parts = [color]
```

- [ ] **Step 3: 新增 A5m 搜索卡无目标颜色仍进详情页的失败测试**

```python
def test_oppo_a5m_enters_first_exact_model_card_before_selecting_target_color() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A5m 5G",
        ram="8GB",
        storage="256GB",
        color="钻石白",
    )
    _set_result_cards(
        page,
        (
            ("OPPO A5m 水晶粉 6GB+128GB", "/cn/web/products/38672.html?us=search"),
            ("OPPO A5m 水晶粉 8GB+256GB", "/cn/web/products/38675.html?us=search"),
        ),
    )
    _set_detail_product(page, model_name="OPPO A5m", capacity="8GB+256GB", color="钻石白")

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/38672.html?us=search")
    assert page.option_clicks == ["capacity", "color"]
```

- [ ] **Step 4: 新增 A6t 搜索卡无目标颜色仍进详情页的失败测试**

```python
def test_oppo_a6t_enters_exact_model_card_and_rejects_neighbor_variants() -> None:
    page = _OppoFixturePage("normal.html")
    task = _oppo_task(
        model_name="OPPO A6t",
        ram="6GB",
        storage="128GB",
        color="墨竹黑",
    )
    _set_result_cards(
        page,
        (
            ("OPPO A6 Pro 冰川蓝 12GB+256GB", "/cn/web/products/41950.html?us=search"),
            ("OPPO A6t 青出于蓝 6GB+128GB 官方标配", "/cn/web/products/41956.html?us=search"),
            ("OPPO A6i 夜幕黑 8GB+256GB", "/cn/web/products/41960.html?us=search"),
        ),
    )
    _set_detail_product(page, model_name="OPPO A6t", capacity="6GB+128GB", color="墨竹黑")

    observation = _adapter().observe(task, page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/41956.html?us=search")
```

- [ ] **Step 5: 运行两个新契约并确认 RED 原因是容量/颜色评分或结果卡选择**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_official_oppo_live.py::test_oppo_a5m_enters_first_exact_model_card_before_selecting_target_color \
  tests/contract/test_official_oppo_live.py::test_oppo_a6t_enters_exact_model_card_and_rejects_neighbor_variants -vv
```

Expected: A5m 测试 FAIL 于进入了第二张“容量得分更高”的卡；A6t 测试 PASS 并作为相似机型拒绝保护。不得出现 fixture 缺少详情页容量或颜色的失败。

- [ ] **Step 6: 提交失败契约**

```bash
git add tests/contract/test_official_oppo_live.py
git commit -m "test: reproduce OPPO model-only result selection"
```

### Task 2: 将 OPPO 搜索卡选择改为精确机型的页面顺序

**Files:**
- Modify: `src/quote_app/sites/official_brands/oppo.py:453-474`
- Modify: `src/quote_app/sites/official_brands/oppo.py:812-844`
- Modify: `tests/contract/test_official_oppo_live.py:153-172`

**Interfaces:**
- Consumes: `_title_matches_model(model_name: str, title: str) -> bool`, `_approved_product_url(value: str | None) -> str`
- Produces: `_preferred_exact_result_link(page: Any, task: WebsiteTask) -> Any | None` 返回页面顺序中第一张精确机型且 URL 合法的可见商品卡。

- [ ] **Step 1: 将旧的“容量+颜色评分优先”测试改为“页面顺序优先”**

```python
def test_oppo_uses_first_exact_model_card_regardless_of_card_capacity_or_color() -> None:
    page = _OppoFixturePage("normal.html")
    result_titles = [
        node
        for node in page.root.descendants()
        if node.attrs.get("data-oppo-role") == "product-title"
    ]
    result_titles[0].text_parts = ["OPPO A6 5G 丝绒灰 8GB+256GB 官方标配"]
    result_titles[1].text_parts = ["OPPO A6 5G 蓝海浮光 12GB+256GB 官方标配"]

    observation = _adapter().observe(_task(), page)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.goto_calls[-1].endswith("/32740.html?us=search")
```

- [ ] **Step 2: 实现最小的顺序选择，删除搜索卡容量/颜色评分**

```python
def _preferred_exact_result_link(self, page: Any, task: WebsiteTask) -> Any | None:
    scope = self._search_scope(page)
    if scope is None or self._explicit_empty_result(scope):
        return None
    for link in _visible(scope, _PRODUCT_LINKS):
        if not _title_matches_model(task.model_name, _link_title(link)):
            continue
        approved = _approved_product_url(link.get_attribute("href"))
        if not _detail_identity(approved).product_key.isdigit():
            raise LayoutRecognitionError("OPPO product identity is invalid")
        return link
    return None
```

- [ ] **Step 3: 删除仅为搜索卡评分服务的私有函数**

```python
# Delete _title_has_capacity() and _title_has_color() after confirming with rg
# that no production or test caller remains.
```

Run:

```bash
rg -n "_title_has_capacity|_title_has_color" src tests
```

Expected: no matches.

- [ ] **Step 4: 运行 OPPO 契约并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/contract/test_official_oppo_live.py -q
```

Expected: all OPPO contract tests PASS.

- [ ] **Step 5: 提交 OPPO 搜索卡最小修复**

```bash
git add src/quote_app/sites/official_brands/oppo.py tests/contract/test_official_oppo_live.py
git commit -m "fix: enter first exact OPPO model card"
```

### Task 3: 冻结详情页四证据与单次 80% 视图逻辑

**Files:**
- Modify: `tests/contract/test_official_oppo_live.py:175-198`
- Verify only: `src/quote_app/sites/official_brands/oppo.py:233-280`
- Verify only: `src/quote_app/sites/official_brands/oppo.py:323-403`
- Verify only: `src/quote_app/sites/detail_capture_view.py`

**Interfaces:**
- Consumes: `prepare_capture_view(task, page, expected) -> None`, `_capture_proof_locators(task, page) -> tuple[Any, ...]`
- Produces: 契约保证四项已同屏时不定位；不同屏时只定位一次；已在 80% 时不重复缩放。

- [ ] **Step 1: 新增已是 80% 时不重复缩放的回归契约**

```python
def test_oppo_capture_does_not_repeat_zoom_when_detail_is_already_at_80_percent() -> None:
    adapter = _adapter()
    task = _task()
    page = _OppoFixturePage("normal.html")
    observation = adapter.observe(task, page)
    assert page.capture_scales == [0.8]

    adapter.prepare_capture_view(task, page, observation.semantic_state)

    assert page.capture_scales == [0.8]
    assert page.position_attempts == 0
```

- [ ] **Step 2: 运行四证据/视图定向测试**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_official_oppo_live.py::test_oppo_capture_accepts_exactly_title_price_capacity_and_color_in_one_view \
  tests/contract/test_official_oppo_live.py::test_oppo_capture_positions_once_only_when_four_proofs_do_not_fit \
  tests/contract/test_official_oppo_live.py::test_oppo_capture_does_not_repeat_zoom_when_detail_is_already_at_80_percent -q
```

Expected: PASS；`capture_scales == [0.8]`，四项已齐全时 `position_attempts == 0`，不齐全时 `position_attempts == 1`。

- [ ] **Step 3: 确认现有幂等缩放路径无需生产代码改动**

```python
# Expected code state after Step 2:
# - _observe_loaded_detail() calls ensure_capture_scale(page, scale=0.8)
# - prepare_capture_view() may call the same idempotent helper
# - the fixture records only one actual transition: capture_scales == [0.8]
# Do not modify detail_capture_view.py and do not add keyboard/browser zoom.
```

- [ ] **Step 4: 提交截图冻结契约**

```bash
git add tests/contract/test_official_oppo_live.py src/quote_app/sites/official_brands/oppo.py
git commit -m "test: freeze OPPO four-proof capture view"
```

### Task 4: 验证 OPPO 管线写入、品牌隔离与冻结回归

**Files:**
- Modify: `tests/integration/test_oppo_official_pipeline.py:22-164`
- Verify only: `tests/contract/test_official_honor_live.py`
- Verify only: `tests/contract/test_official_xiaomi_live.py`
- Verify only: `tests/regression/test_honor_official_baseline.py`

**Interfaces:**
- Consumes: `run_full_pipeline(request, website_runner=...) -> FullPipelineResult`
- Produces: `_oppo_model_only_inputs(tmp_path: Path) -> InputPaths` 和 `_OppoModelOnlyScenarioPage`，使 A5m/A6t 两行依基础表顺序写入 `AK` 和 `AN`，且每行只处理一次。

- [ ] **Step 1: 将 OPPO 集成输入扩展为 A5m/A6t 真实目标配置**

```python
OPPO_ROWS = (
    ("OPPO-A5M", "OPPO A5m 5G", "8GB", "256GB", "钻石白"),
    ("OPPO-A6T", "OPPO A6t", "6GB", "128GB", "墨竹黑"),
)


def _oppo_model_only_inputs(tmp_path: Path) -> InputPaths:
    inputs = tmp_path / "model-only-inputs"
    inputs.mkdir()
    base = save_workbook(
        inputs / "base.xlsx",
        _headers(
            13,
            A="集团一级库物料编码",
            C="2026年3月结算报价（元/台）",
            D="2026年7月结算报价（元/台）",
        ),
        [
            _row(13, A=material, B=f"经理{index}", C=2199, D=1999)
            for index, (material, _model, _ram, _storage, _color)
            in enumerate(OPPO_ROWS, start=1)
        ],
    )
    marketing = save_workbook(
        inputs / "marketing.xlsx",
        _headers(45, I="物料编码"),
        [
            _row(
                45,
                B="智能手机",
                C="欧珀",
                E=model,
                I=material,
                M="5G手机",
                V="2026-01-01",
                X=2199,
                AQ=ram,
                AR=storage,
                AS=color,
            )
            for material, model, ram, storage, color in OPPO_ROWS
        ],
    )
    bop = save_workbook(
        inputs / "bop.xlsx",
        _headers(26, K="集团一级库编码"),
        [
            _row(26, K=material, Y=f"{ram.removesuffix('GB')}+{storage.removesuffix('GB')}", Z="已配置")
            for material, _model, ram, storage, _color in OPPO_ROWS
        ],
    )
    return InputPaths(base, marketing, bop, tmp_path / "outputs")
```

- [ ] **Step 2: 新增根据当前搜索词配置详情页的受控页面**

```python
class _OppoModelOnlyScenarioPage(_OppoScenarioPage):
    def activate_results(self) -> None:
        keyword = self.locator('[data-oppo-role="search-input"]').input_value()
        if keyword == "OPPO A5m 5G":
            _set_result_cards(
                self,
                (("OPPO A5m 水晶粉 6GB+128GB", "/cn/web/products/38672.html?us=search"),),
            )
            _set_detail_product(
                self,
                model_name="OPPO A5m",
                capacity="8GB+256GB",
                color="钻石白",
            )
        elif keyword == "OPPO A6t":
            _set_result_cards(
                self,
                (("OPPO A6t 青出于蓝 6GB+128GB", "/cn/web/products/41956.html?us=search"),),
            )
            _set_detail_product(
                self,
                model_name="OPPO A6t",
                capacity="6GB+128GB",
                color="墨竹黑",
            )
        super().activate_results()
```

The integration test imports `_set_result_cards` and `_set_detail_product` from `tests.contract.test_official_oppo_live`, matching the existing test-only fixture reuse pattern.

- [ ] **Step 3: 新增两行顺序、价格和截图锚点集成断言**

```python
def test_oppo_a5m_a6t_keep_input_order_and_write_ak_an(tmp_path: Path) -> None:
    session = _OppoSession(_OppoModelOnlyScenarioPage())
    result = run_full_pipeline(
        _full_request(_oppo_model_only_inputs(tmp_path), tmp_path),
        website_runner=lambda request: _run_real_runner(
            request,
            capture=_FormalCapture(),
            session=session,  # type: ignore[arg-type]
        ),
    )

    assert [row.material_code for row in result.rows] == ["OPPO-A5M", "OPPO-A6T"]
    quote = load_workbook(result.quote_path, data_only=False)
    try:
        sheet = quote["5G手机"]
        assert [sheet["AK2"].value, sheet["AK3"].value] == [1899, 1899]
        assert _image_anchors(sheet) == {"AN2", "AN3"}
    finally:
        quote.close()
```

- [ ] **Step 4: 运行 OPPO 契约与管线集成测试**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_official_oppo_live.py \
  tests/integration/test_oppo_official_pipeline.py -q
```

Expected: all PASS; A5m/A6t `AK` 和 `AN` 均存在。

- [ ] **Step 5: 运行品牌隔离与荣耀/小米冻结回归**

Run:

```bash
.venv/bin/pytest \
  tests/contract/test_official_adapters.py \
  tests/contract/test_official_honor_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/regression/test_honor_official_baseline.py -q
```

Expected: all PASS.

- [ ] **Step 6: 运行静态检查与 OPPO 相关广回归**

Run:

```bash
.venv/bin/ruff check src/quote_app/sites/official_brands/oppo.py \
  tests/contract/test_official_oppo_live.py \
  tests/integration/test_oppo_official_pipeline.py
.venv/bin/mypy src/quote_app/sites/official_brands/oppo.py
.venv/bin/pytest -q -k "oppo or official or task_builder or runner_registry or scheduler or full_pipeline"
```

Expected: Ruff PASS, mypy PASS, selected regression suite PASS.

- [ ] **Step 7: 提交管线回归保护**

```bash
git add tests/integration/test_oppo_official_pipeline.py
git commit -m "test: cover OPPO A5m A6t pipeline"
```

### Task 5: 构建、签名并交付新 Mac 测试包

**Files:**
- Verify only: `packaging/quotation_app.spec`
- Create: `build-official-oppo-63/`
- Create: `dist-official-oppo-63/福建移动铺货报价助手.app`

**Interfaces:**
- Consumes: 通过契约、集成、冻结回归的当前工作树。
- Produces: 可在 Mac 实机验收 A5m/A6t 官网闭环的 `.63` 签名应用。

- [ ] **Step 1: 核对 PyInstaller 入口和输出目录不覆盖 `.62`**

```bash
rg -n "name=|BUNDLE|CFBundle" packaging/quotation_app.spec
test ! -e dist-official-oppo-63/福建移动铺货报价助手.app
```

Expected: 新输出目录不存在，`.62` 不受影响。

- [ ] **Step 2: 构建 `.63`**

```bash
.venv/bin/pyinstaller --noconfirm \
  --distpath dist-official-oppo-63 \
  --workpath build-official-oppo-63 \
  packaging/quotation_app.spec
```

Expected: `dist-official-oppo-63/福建移动铺货报价助手.app` exists.

- [ ] **Step 3: 签名并验证应用结构**

```bash
codesign --force --deep --sign - \
  dist-official-oppo-63/福建移动铺货报价助手.app
codesign --verify --deep --strict --verbose=2 \
  dist-official-oppo-63/福建移动铺货报价助手.app
```

Expected: `valid on disk` and `satisfies its Designated Requirement`.

- [ ] **Step 4: 运行打包冒烟测试**

Run:

```bash
.venv/bin/pytest tests/smoke/test_packaging_spec.py -q
```

Expected: PASS.

- [ ] **Step 5: 交付实机验收清单**

```text
1. 选择 OPPO 官网测试范围。
2. A5m 搜索结果即使只显示水晶粉，也应进入同机型详情页。
3. A6t 搜索结果即使只显示青出于蓝，也应进入同机型详情页。
4. 详情页分别选中钻石白/8GB+256GB 与墨竹黑/6GB+128GB。
5. 输出表 AK 有官网价，AN 有产品名称+价格+容量+颜色四项同屏截图。
6. 如 Mac 系统首次弹出截图权限，点击允许后重试当条；不将权限弹窗造成的失败误判为站点适配失败。
```

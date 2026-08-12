# vivo 官网真实闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为规范品牌“维沃”实现仅 vivo、不含 iQOO 的官网搜索、精确机型、目标版本与颜色、较低有效官网价、四项同屏截图、检查点恢复和 Excel `AK`/`AN` 闭环。

**Architecture:** 保留荣耀、小米和 OPPO 适配器；把工厂中的 `VivoOfficialAdapter` 占位类替换为 `LiveOfficialAdapterBase` 的独立实现。vivo 的域名、搜索、商品卡、详情 URL、版本、颜色、价格和截图视图全部封装在 `vivo.py`；公共运行时只追加现有注册，不引入跨品牌 DOM 条件。

**Tech Stack:** Python 3.12、Playwright 同步页面协议、pytest、openpyxl、Tkinter、PyInstaller、macOS codesign。

## Global Constraints

- 只支持 vivo，不支持 iQOO；iQOO 任务在官网访问前以明确人工处理原因结束。
- 正常截图只要求产品名称、官网价格、目标版本、目标颜色四项同屏。
- 四项已经同屏时立即截图，不滚动、不缩放。
- 只有四项不足时才幂等切换 80%；仍不足或被浮层遮挡时最多定向滚动一次。
- 库存、地区、促销、购买按钮、套餐、服务、隐藏 SKU 和固定父级层级不作为截图门槛。
- 缺货或到货通知不阻止进入详情、取价和截图。
- 每行先保存价格和详情 URL，再截图；截图失败不得清空价格。
- 不修改荣耀、小米、OPPO、京东、天猫和 Excel 列映射的既有业务行为。
- 新测试包使用独立 `.69` 目录，不覆盖 `.68` 及历史包。

---

### Task 1: 建立 vivo 真实页面契约

**Files:**
- Create: `tests/fixtures/sites/official_live/vivo/search_results.html`
- Create: `tests/fixtures/sites/official_live/vivo/detail_normal.html`
- Create: `tests/fixtures/sites/official_live/vivo/detail_missing_capacity.html`
- Create: `tests/fixtures/sites/official_live/vivo/detail_missing_color.html`
- Create: `tests/contract/test_official_vivo_live.py`

**Interfaces:**
- Consumes: `WebsiteTask`、`AdapterObservation`、`VerifiedSemanticState`、`LiveOfficialAdapterBase`。
- Produces: vivo 搜索、详情、配置、价格、截图及 iQOO 范围隔离的失败契约。

- [ ] **Step 1: 保存脱敏 vivo 真实结构 fixture**

  保留搜索框、具体结果区域、精确卡、相似派生卡、`/product/<数字>` 链接、版本、颜色、选中态、主售价及优惠/分期干扰金额。加入“缺货但卡片可进入”和“首张精确卡 URL 非法、后续精确卡合法”两个场景。

- [ ] **Step 2: 写正常流程 RED 测试**

  测试任务使用 `brand="维沃"`、`model_name="vivo X200"`、`ram="12GB"`、`storage="256GB"`、目标颜色；断言只进入精确基础机型，先版本后颜色，生成 `PRICE_FOUND`，并从主购买摘要有效候选中取较低值。

  ```python
  observation = adapter.observe(task, page)
  assert observation.outcome is BusinessOutcome.PRICE_FOUND
  assert observation.price == Decimal("4499")
  assert page.option_clicks == ["capacity", "color"]
  ```

- [ ] **Step 3: 写 iQOO 范围隔离 RED 测试**

  构造 `model_name="iQOO 15"` 的维沃任务；断言抛出明确不可支持错误，且 `goto_calls == []`、`fill_calls == []`，不能转成 `NO_MODEL`。

  ```python
  with pytest.raises(NonRetryableTechnicalError, match="iQOO"):
      adapter.observe(task, page)
  assert page.goto_calls == []
  ```

- [ ] **Step 4: 写搜索与合法“无”RED 测试**

  覆盖：相似派生机型排除、卡片颜色不一致仍进入、缺货卡仍进入、首张非法 URL 后继续、全部非法技术失败、完整等待后 `NO_MODEL`、版本缺失、颜色缺失。三类合法“无”分别断言证据角色为 `search_keyword/result_region`、`capacity`、`color`。

- [ ] **Step 5: 写价格和截图 RED 测试**

  在主价格区放入当前价、券后价、券金额、分期月供、划线价、配件价，断言只从当前主商品有效价格取较低值。再覆盖四项同屏零操作、80% 后同屏、浮层遮挡任一四项时一次定向滚动、一次后仍遮挡则失败。

- [ ] **Step 6: 运行测试并确认 RED**

  Run: `.venv/bin/pytest -q tests/contract/test_official_vivo_live.py`

  Expected: 因 `src/quote_app/sites/official_brands/vivo.py` 尚不存在、工厂仍返回占位类而失败。

---

### Task 2: 实现 vivo 独立适配器

**Files:**
- Create: `src/quote_app/sites/official_brands/vivo.py`
- Modify: `src/quote_app/sites/official_brands/factory.py`
- Modify: `src/quote_app/sites/official_brands/__init__.py`
- Test: `tests/contract/test_official_vivo_live.py`
- Test: `tests/unit/test_official_brand_factory.py`
- Test: `tests/regression/test_official_brand_isolation.py`

**Interfaces:**
- Consumes: `LiveOfficialAdapterBase.observe()`、`resume()`、`build_observation()`、`WebsiteTask`、`SiteSpec`。
- Produces: `VivoOfficialAdapter(SiteSpec)`，实现观察、恢复、正式状态复核、截图准备/恢复和证据矩形钩子。

- [ ] **Step 1: 实现任务范围和批准域名**

  在任何 `page.goto()` 前规范化检查机型；匹配 `iqoo` 的大小写/空格变体时抛出 `NonRetryableTechnicalError("UNSUPPORTED_VIVO_MODEL_FAMILY", "vivo 官网适配暂不支持 iQOO，需人工处理")`。只批准 vivo 官方 HTTPS host 和页面真实 `/product/<数字>` 详情 URL。

- [ ] **Step 2: 实现搜索和精确商品卡**

  提交一次真实搜索；在具体结果区内等待可见卡片。精确匹配基础机型，排除派生型号；不以卡片颜色、容量、缺货文字或搜索框是否保留搜索词作为进入条件。遍历精确卡直到得到首个批准详情 URL。

- [ ] **Step 3: 实现详情身份、版本和颜色**

  进入后复核真实最终 URL 与标题。先精确匹配 `RAM＋ROM` 版本，再精确匹配颜色；已经唯一选中的目标不重复点击。配置组短时未完整加载时等待有界窗口；只有完整窗口结束后才写合法“无”。

- [ ] **Step 4: 实现 vivo 价格策略**

  即时快照必须同时复核唯一目标版本和颜色。候选限定在当前主商品购买摘要，排除券、补贴、满减、分期、以旧换新、保险、服务、配件、推荐商品与划线价；其余当前有效人民币价格取较低值。价格短暂缺失只重试价格不可用，配置/URL/标题漂移立即失败。

- [ ] **Step 5: 实现四证据截图准备**

  获取产品、价格、版本、颜色四个可见矩形并逐一中心 hit-test。已同屏且未遮挡返回零操作；否则幂等切换 80%，重新取证；仍不满足时按实际被遮/越界证据计算一次不超过 160px 的轻微定位，随后重新取证。不得操作官网 DOM 隐藏浮层。

- [ ] **Step 6: 运行契约、工厂与隔离测试**

  Run: `.venv/bin/pytest -q tests/contract/test_official_vivo_live.py tests/unit/test_official_brand_factory.py tests/regression/test_official_brand_isolation.py`

  Expected: PASS，且工厂对“维沃”返回新的独立 `VivoOfficialAdapter`，其他品牌类身份不变。

- [ ] **Step 7: 提交适配器实现**

  ```bash
  git add src/quote_app/sites/official_brands/vivo.py src/quote_app/sites/official_brands/factory.py src/quote_app/sites/official_brands/__init__.py tests/fixtures/sites/official_live/vivo tests/contract/test_official_vivo_live.py tests/unit/test_official_brand_factory.py tests/regression/test_official_brand_isolation.py
  git commit -m "feat: add vivo official live adapter"
  ```

---

### Task 3: 接入正式截图与 Excel 闭环

**Files:**
- Create: `tests/integration/test_vivo_official_pipeline.py`
- Modify: `src/quote_app/evidence/macos_runtime.py`（仅在现有注册不足时追加 vivo reader）
- Modify: `tests/regression/test_official_capture_reader_isolation.py`

**Interfaces:**
- Consumes: `WebsiteTaskRunner`、SQLite checkpoint repository、`MacFormalCaptureRuntime`、`run_full_pipeline`。
- Produces: vivo `WebsiteResult`、`AK` 价格、`AN` 截图、执行报告统计和可恢复检查点。

- [ ] **Step 1: 写完整闭环 RED 测试**

  使用真实 runner、checkpoint repository 和 Excel pipeline，只替换浏览器及系统截图边界。断言正常结果写 `AK`/`AN`，截图失败保留 `AK` 与详情 URL，两行保持输入顺序，报告总数/完成数一致，重启从详情恢复且搜索调用次数不增加。

  ```python
  assert sheet["AK2"].value == 4499
  assert sheet._images
  assert checkpoint.url.startswith("https://shop.vivo.com.cn/product/")
  ```

- [ ] **Step 2: 确认正式截图 reader 注册**

  若 `MacFormalCaptureRuntime` 已由通用 live-official 映射覆盖 `("维沃", OFFICIAL)`，不改生产代码；否则只追加该键。回归断言荣耀、小米、OPPO、京东、天猫 reader 不变。

- [ ] **Step 3: 运行集成与截图隔离测试**

  Run: `.venv/bin/pytest -q tests/integration/test_vivo_official_pipeline.py tests/regression/test_official_capture_reader_isolation.py tests/unit/test_macos_capture_runtime.py`

  Expected: PASS。

- [ ] **Step 4: 提交流水线闭环**

  ```bash
  git add tests/integration/test_vivo_official_pipeline.py src/quote_app/evidence/macos_runtime.py tests/regression/test_official_capture_reader_isolation.py
  git commit -m "test: cover vivo official quotation pipeline"
  ```

---

### Task 4: 冻结回归、打包和实机验收

**Files:**
- Modify: `src/quote_app/app.py`（只更新 vivo 构建标签）
- Modify: `tests/unit/test_app.py`
- Modify: `docs/testing/live-site-matrix.md`

**Interfaces:**
- Consumes: 已通过的 vivo 适配器、现有“vivo 官网验收（仅官网）”运行模式及打包脚本。
- Produces: 独立 `.69` Mac 测试包，不覆盖 `.68`。

- [ ] **Step 1: 运行当前站点与冻结站点回归**

  Run: `.venv/bin/pytest -q tests/contract/test_official_vivo_live.py tests/integration/test_vivo_official_pipeline.py tests/contract/test_official_oppo_live.py tests/integration/test_oppo_official_pipeline.py tests/contract/test_official_xiaomi_live.py tests/integration/test_xiaomi_official_pipeline.py tests/contract/test_official_honor_live.py tests/contract/test_jd_adapter.py tests/contract/test_tmall_adapter.py tests/contract/test_official_adapters.py`

  Expected: 全部 PASS。

- [ ] **Step 2: 运行 Excel、完整回归、静态和类型检查**

  Run: `.venv/bin/pytest -q tests/integration/test_web_to_excel.py tests/integration/test_core_pipeline.py tests/integration/test_full_pipeline_fixture_sites.py tests/unit/test_app.py`

  Run: `.venv/bin/ruff check src/quote_app/sites/official_brands tests/contract/test_official_vivo_live.py tests/integration/test_vivo_official_pipeline.py`

  Run: `.venv/bin/mypy src/quote_app/sites/official_brands/vivo.py src/quote_app/sites/official_brands/factory.py src/quote_app/app.py`

  Expected: 全部 PASS；若全量测试仅因沙箱禁止本地套接字失败，必须在允许环境单独复跑该测试并记录结果。

- [ ] **Step 3: 独立审查**

  审查必须确认：iQOO 不会访问官网；价格未受优惠污染；正常截图只有四项；80% 和定位幂等；截图失败不丢价格；没有修改冻结站点行为。Critical/Important 全部关闭后才能打包。

- [ ] **Step 4: 构建 `.69` 并校验签名**

  使用 `build-official-vivo-69/` 和 `dist-official-vivo-69/` 执行 PyInstaller，随后执行 ad-hoc 签名、严格验证和应用启动 smoke。不得覆盖 `.68`。

- [ ] **Step 5: 用户真站验收**

  先验证材料行 `vivo Y50s 6GB+256GB 钻黑` 的真实“有价或合法无机型”；再验证一条当前在售 vivo 机型的价格、截图、`AK`、`AN` 和执行报告。多行按基础表顺序成功后，由用户确认冻结 vivo，再进入华为设计。

- [ ] **Step 6: 提交构建标签与验收矩阵**

  ```bash
  git add src/quote_app/app.py tests/unit/test_app.py docs/testing/live-site-matrix.md
  git commit -m "build: prepare vivo official Mac test package"
  ```

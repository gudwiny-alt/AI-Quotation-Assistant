# 京东最终截图阶段固定 90% Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以 `.37` 行为为基线，只在京东正式截图阶段固定使用 90% 网页视觉比例，使无报价页的搜索词与商品名称同屏，并使详情页的标题、价格、颜色与容量同屏。

**Architecture:** 公共取景模块提供可逆的固定视觉比例和严格同屏校验；`JDAdapter` 仅在 `prepare_capture_view` 中应用 90%，搜索和报价逻辑保持原样。macOS 正式截图运行时持有一次性的恢复回调，在截图成功、失败或上下文构造失败时恢复原比例。

**Tech Stack:** Python 3.12、Playwright、pytest、PyInstaller、macOS 原生整屏截图运行时。

## Global Constraints

- 不修改荣耀官网任何代码或行为。
- 不修改天猫任何代码或行为。
- 不修改京东登录、风控、搜索、商品匹配、详情进入、SKU 选择或价格读取。
- 只使用固定 90%，不采用 100%／90%／80% 循环。
- 缩放只覆盖京东最终截图阶段，所有结束路径均恢复原比例。
- 条件满足后立即截图，不再上下往返滚动。

---

### Task 1: 可逆的固定视觉比例与严格同屏定位

**Files:**
- Modify: `src/quote_app/sites/detail_capture_view.py`
- Test: `tests/unit/test_detail_capture_view.py`

**Interfaces:**
- Produces: `apply_capture_scale(page: Any, *, scale: float) -> None`
- Produces: `restore_capture_scale(page: Any) -> None`
- Extends: `position_result_cards_for_capture(..., search_input: Any | None = None) -> None`
- Preserves: `position_detail_for_capture(...) -> None`

- [ ] **Step 1: Write failing fixed-scale lifecycle tests**

Add page-fixture support that records `page.evaluate(script, value)` calls, then add:

```python
def test_applies_one_fixed_capture_scale_and_waits_for_layout() -> None:
    page = _Page()

    apply_capture_scale(page, scale=0.9)

    assert page.capture_scales == [0.9]
    assert page.waits == [300]


def test_restores_the_original_capture_scale() -> None:
    page = _Page()
    apply_capture_scale(page, scale=0.9)

    restore_capture_scale(page)

    assert page.scale_restored is True
```

- [ ] **Step 2: Run the new scale tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_detail_capture_view.py::test_applies_one_fixed_capture_scale_and_waits_for_layout \
  tests/unit/test_detail_capture_view.py::test_restores_the_original_capture_scale -q
```

Expected: FAIL because `apply_capture_scale` and `restore_capture_scale` do not exist.

- [ ] **Step 3: Implement the minimal reversible scale helpers**

In `detail_capture_view.py`, add scripts that:

```python
_CAPTURE_SCALE_WAIT_MS = 300


def apply_capture_scale(page: Any, *, scale: float) -> None:
    if not 0.5 <= scale <= 1.0:
        raise ValueError("capture scale must be between 0.5 and 1.0")
    applied = page.evaluate(_APPLY_CAPTURE_SCALE, scale)
    if applied is not True:
        raise LayoutRecognitionError("capture scale could not be applied")
    page.wait_for_timeout(_CAPTURE_SCALE_WAIT_MS)


def restore_capture_scale(page: Any) -> None:
    restored = page.evaluate(_RESTORE_CAPTURE_SCALE)
    if restored is not True:
        raise LayoutRecognitionError("capture scale could not be restored")
    page.wait_for_timeout(_CAPTURE_SCALE_WAIT_MS)
```

The JavaScript must store the previous inline `zoom` value on `document.documentElement`, set `zoom` to `0.9`, and remove the saved attribute after restoration. It must return a boolean and must not change browser-window bounds or system display settings.

- [ ] **Step 4: Write failing result-page same-viewport tests**

Add tests proving a result screenshot requires the searched input and a readable card title together:

```python
def test_no_model_result_requires_search_input_and_card_name_in_viewport() -> None:
    page = _Page()
    page.in_viewport["search"] = False

    with pytest.raises(LayoutRecognitionError, match="search input"):
        position_result_cards_for_capture(
            page,
            search_input=_Locator(page, "search"),
            product_name=_Locator(page, "title"),
            product_card=_Locator(page, "card"),
            site_name="JD",
        )


def test_no_model_result_accepts_search_input_and_card_name_together() -> None:
    page = _Page()
    page.in_viewport["search"] = True

    position_result_cards_for_capture(
        page,
        search_input=_Locator(page, "search"),
        product_name=_Locator(page, "title"),
        product_card=_Locator(page, "card"),
        site_name="JD",
    )

    assert page.centered == ["card"]
```

- [ ] **Step 5: Run the result-page tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_detail_capture_view.py -q
```

Expected: FAIL because `position_result_cards_for_capture` does not accept or verify `search_input`.

- [ ] **Step 6: Implement the search-input visibility check**

Extend `position_result_cards_for_capture` with `search_input: Any | None = None`. After the one card-positioning action, reject the view when the supplied search input or product name is outside the viewport. For an explicit empty result with no card, still require the supplied search input to be visible.

- [ ] **Step 7: Run unit tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_detail_capture_view.py -q
```

Expected: PASS with no 100%／90%／80% loop.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/quote_app/sites/detail_capture_view.py tests/unit/test_detail_capture_view.py
git commit -m "feat: add fixed capture scale lifecycle"
```

---

### Task 2: 京东最终取景固定为 90%

**Files:**
- Modify: `src/quote_app/sites/jd.py`
- Test: `tests/contract/test_jd_adapter.py`

**Interfaces:**
- Consumes: `apply_capture_scale(page, scale=0.9)` and `restore_capture_scale(page)` from Task 1.
- Produces: `JDAdapter.restore_capture_view(task, page, expected) -> None`
- Preserves: all `observe`, `resume`, result matching, SKU and price behavior.

- [ ] **Step 1: Write failing no-model 90% test**

Extend `_FixturePage` to record capture-scale scripts and add a test equivalent to:

```python
def test_jd_no_model_uses_fixed_90_percent_and_keeps_keyword_with_card_name() -> None:
    page = _FixturePage("no_model.html")
    task = _task()
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.capture_scales == [0.9]
    assert page.capture_view_positions == ["result-card"]
    assert page.search_input_visibility_checks == 1
```

- [ ] **Step 2: Write failing Power2-style detail 90% test**

Add a modern-detail fixture test proving that formal capture applies 90%, positions the selected capacity once, and does not re-run search or detail navigation:

```python
def test_jd_detail_uses_fixed_90_percent_and_positions_once() -> None:
    page = _FixturePage(html=modern_price_html)
    task = _task(ram="16GB", storage="512GB")
    adapter = JDAdapter(_xiaomi_spec())
    observation = adapter.observe(task, cast(Any, page))
    goto_count = len(page.goto_calls)

    adapter.prepare_capture_view(task, cast(Any, page), observation.semantic_state)

    assert page.capture_scales == [0.9]
    assert page.capture_view_positions == ["modern-capacity"]
    assert len(page.goto_calls) == goto_count
```

- [ ] **Step 3: Run both JD tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contract/test_jd_adapter.py::test_jd_no_model_uses_fixed_90_percent_and_keeps_keyword_with_card_name \
  tests/contract/test_jd_adapter.py::test_jd_detail_uses_fixed_90_percent_and_positions_once -q
```

Expected: FAIL because JD does not apply 90% and does not pass the validated search input into final positioning.

- [ ] **Step 4: Apply fixed scale only in JD `prepare_capture_view`**

At the beginning of `JDAdapter.prepare_capture_view`, call:

```python
apply_capture_scale(browser_page, scale=0.9)
```

For `NO_MODEL`, pass the already revalidated `result_search_input` into `position_result_cards_for_capture`. For `PRICE_FOUND`, retain the existing title, selling-price, selected-color and selected-capacity readers and call `position_detail_for_capture` once.

Wrap final positioning so any exception before a successful return immediately calls `restore_capture_scale(browser_page)` and re-raises the original failure.

- [ ] **Step 5: Add the JD restoration method**

Add:

```python
def restore_capture_view(
    self,
    task: WebsiteTask,
    page: Any,
    expected: VerifiedSemanticState,
) -> None:
    self._validate_task(task)
    if not isinstance(expected, VerifiedSemanticState):
        raise LayoutRecognitionError("JD capture state is unavailable for restoration")
    restore_capture_scale(_playwright_page(page))
```

- [ ] **Step 6: Run the JD contract suite and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/contract/test_jd_adapter.py -q
```

Expected: PASS; existing observation, matching, SKU and price tests remain unchanged.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
git commit -m "fix: frame JD evidence at fixed 90 percent"
```

---

### Task 3: macOS 截图结束后精确恢复京东比例

**Files:**
- Modify: `src/quote_app/evidence/macos_runtime.py`
- Test: `tests/unit/test_macos_capture_runtime.py`

**Interfaces:**
- Consumes: optional adapter method `restore_capture_view(task, page, expected) -> None`.
- Produces: one lease-owned cleanup callback stored in `_CurrentState` and invoked exactly once.
- Preserves: existing window identity, foreground-window and semantic-state probe behavior.

- [ ] **Step 1: Write failing successful-capture cleanup test**

Extend `_CapturePreparedAdapter.__init__` with
`self.restore_calls: list[tuple[WebsiteTask, object, VerifiedSemanticState]] = []`
and add this method:

```python
def restore_capture_view(
    self,
    task: WebsiteTask,
    page: object,
    expected: VerifiedSemanticState,
) -> None:
    self.restore_calls.append((task, page, expected))
```

Then add:

```python
def test_runtime_restores_prepared_view_once_after_successful_capture() -> None:
    calls: list[str] = []
    adapter = _CapturePreparedAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    page = object()
    state = _state(canonical_url="https://example.test/jd/item-1")
    context = runtime.capture_context_provider(task, page, state)
    runtime.evidence_capture()._pipeline = _RawCapturePipeline(
        result=object()
    )  # type: ignore[assignment]

    runtime.evidence_capture().capture(_capture_request(context))

    assert adapter.restore_calls == [(task, page, state)]
```

- [ ] **Step 2: Write failing error-path cleanup tests**

Define a fixture adapter whose rectangle reread fails after preparation:

```python
class _FailingCaptureGeometryAdapter(_CapturePreparedAdapter):
    def capture_rectangles_for_capture(
        self,
        task: WebsiteTask,
        page: object,
        expected: VerifiedSemanticState,
    ) -> tuple[CssRect, ...]:
        del task, page, expected
        raise LayoutRecognitionError("fixture final geometry failed")
```

Cover both failures:

```python
def test_runtime_restores_view_when_capture_fails() -> None:
    calls: list[str] = []
    adapter = _CapturePreparedAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    page = object()
    state = _state(canonical_url="https://example.test/jd/item-1")
    context = runtime.capture_context_provider(task, page, state)
    runtime.evidence_capture()._pipeline = _RawCapturePipeline(
        error=OSError("capture failed")
    )  # type: ignore[assignment]

    with pytest.raises(OSError, match="capture failed"):
        runtime.evidence_capture().capture(_capture_request(context))

    assert adapter.restore_calls == [(task, page, state)]


def test_runtime_restores_view_when_context_build_fails_after_prepare() -> None:
    calls: list[str] = []
    adapter = _FailingCaptureGeometryAdapter(_site_spec(WebsiteChannel.JD))
    runtime = MacFormalCaptureRuntime(
        sampler=_Sampler(calls),
        bridge=_Bridge(calls),
        binder=_Binder(calls),
        adapter_registry=_CustomAdapterRegistry(adapter),
        monotonic_clock=lambda: _NOW,
    )
    task = _task(channel=WebsiteChannel.JD)
    page = object()
    state = _state(canonical_url="https://example.test/jd/item-1")

    assert (
        _capture_error_code(
            lambda: runtime.capture_context_provider(task, page, state)
        )
        == "CAPTURE_ENVIRONMENT"
    )
    assert adapter.restore_calls == [(task, page, state)]
```

Each test must assert that the original capture/context error remains the reported failure and cleanup is called exactly once.

- [ ] **Step 3: Run cleanup tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_macos_capture_runtime.py -k 'restores_prepared_view or restores_view_when' -q
```

Expected: FAIL because `_CurrentState` has no restoration callback and runtime does not invoke the adapter cleanup.

- [ ] **Step 4: Store one optional restoration callback in the capture lease**

Add to `_CurrentState`:

```python
restore_capture_view: Callable[[], None] | None
```

After `_injected_honor_reader_factory` successfully prepares the view, obtain the same prepared adapter and create a closure around its optional `restore_capture_view(task, page, state)` method. Store the closure before rectangle rereading.

- [ ] **Step 5: Restore exactly once on every terminal path**

Add a private helper that clears the callback before invoking it. Call it:

- from `_capture_once` `finally`, after the formal screenshot attempt;
- from `_build_capture_context` exception handling when preparation succeeded but context construction failed.

Do not call restoration for official or Tmall adapters that do not provide the method. Do not retry cleanup, and do not change the original capture error when cleanup itself fails during an already-failing path.

- [ ] **Step 6: Run macOS runtime tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_macos_capture_runtime.py -q
```

Expected: PASS, including window-identity regression tests.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/quote_app/evidence/macos_runtime.py tests/unit/test_macos_capture_runtime.py
git commit -m "fix: restore JD capture scale after evidence"
```

---

### Task 4: 版本标记、跨站回归与 Mac 测试包

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Verify: `tests/regression/test_honor_official_baseline.py`
- Verify: `tests/contract/test_tmall_adapter.py`
- Produces: `dist-jd-fixed-90-capture/福建移动铺货报价助手.app`

**Interfaces:**
- Produces: `APP_BUILD_LABEL = "京东90%定格截图版（全部荣耀行）2026.08.04.40"`.
- Preserves: `.37` 荣耀官网和天猫行为。

- [ ] **Step 1: Write the failing build-label test**

Update the existing assertion in `tests/unit/test_app.py` to:

```python
assert APP_BUILD_LABEL == "京东90%定格截图版（全部荣耀行）2026.08.04.40"
```

- [ ] **Step 2: Run the label test and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_app.py::test_app_build_label_identifies_honor_three_site_test_scope -q
```

Expected: FAIL with the prior `.39` label.

- [ ] **Step 3: Update the application label**

Change only `APP_BUILD_LABEL` in `src/quote_app/app.py` to the exact `.40` string above.

- [ ] **Step 4: Run focused and cross-site regression**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_detail_capture_view.py \
  tests/contract/test_jd_adapter.py \
  tests/unit/test_macos_capture_runtime.py \
  tests/regression/test_honor_official_baseline.py \
  tests/contract/test_tmall_adapter.py \
  tests/unit/test_app.py -q
```

Expected: PASS. Confirm Tmall test fixtures record no capture-scale calls and official baseline results remain unchanged.

- [ ] **Step 5: Run static checks**

Run:

```bash
.venv/bin/python -m ruff check src tests
```

Expected: PASS.

- [ ] **Step 6: Build and sign the Mac test package**

Run:

```bash
packaging/macos/build.sh
codesign --verify --deep --strict --verbose=2 "dist/福建移动铺货报价助手.app"
```

Expected: full test suite passes during build and code-sign verification exits 0.

Copy the signed app into a new non-overwriting delivery folder:

```bash
mkdir -p dist-jd-fixed-90-capture
ditto "dist/福建移动铺货报价助手.app" \
  "dist-jd-fixed-90-capture/福建移动铺货报价助手.app"
```

- [ ] **Step 7: Commit version and verification changes**

```bash
git add src/quote_app/app.py tests/unit/test_app.py
git commit -m "build: label JD fixed 90 percent capture package"
```

- [ ] **Step 8: Hand off focused manual acceptance**

Ask the user to run the `.40` app with the two-HONOR-row source and verify only:

1. 畅玩80京东截图同时显示“荣耀畅玩80”搜索词和不匹配商品名称；
2. Power2京东截图同时显示标题、价格、颜色和容量；
3. 荣耀官网和天猫输出与 `.37` 一致。

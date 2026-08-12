# OPPO 搜索商品卡只读诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 OPPO 官网搜索结果阶段发生结构识别失败时，保存当前搜索页面截图和逐卡 JSON 诊断，同时不改变任何选卡、详情页、价格、截图或 Excel 业务规则。

**Architecture:** 保持现有 `WebsiteTaskRunner` 的诊断文件生命周期：它为一次失败提供安全的 `evidence/diagnostics/<task>.g<a>.a<b>.png` 目标路径，并将成功返回的路径写入任务结果。新增 OPPO 专用诊断采集器，严格只接受 OPPO 官方渠道的搜索结果阶段错误；采集器从当前受控页面读取有限的可见链接、嵌套标题、URL 与匹配结果，并把 JSON 写在截图同名侧车文件中。`web_run.py` 只在既有诊断回调中增加 OPPO 分发，不改变其他站点回调行为。

**Tech Stack:** Python 3.12、同步 Playwright 页面 API、pytest、现有 `WebsiteTaskRunner` 诊断接口、UTF-8 JSON。

## Global Constraints

- 仅 OPPO 官网（`brand == "OPPO"` 且 `channel == OFFICIAL`）可以生成本轮 JSON；荣耀、小米、京东、天猫逻辑不得改变。
- 诊断只读：不得点击卡片、修改搜索词、等待时间、商品卡顺序、详情页配置、价格、截图或 Excel/报告汇总。
- 缺货与商品标题省略 `5G` 只记录为原始文本，绝不能成为拒绝原因。
- 仍由原有异常分类记录 `LAYOUT_CHANGED`；诊断写入错误必须被吞掉，不能改变任务状态、重试次数或原错误。
- JSON 仅保存有界的公开页面结构文本，禁止 Cookie、Local Storage、账号、地址、手机号、浏览历史及含凭据 URL。
- 新包只能输出到 `dist-official-oppo-65/`，不得覆盖 `.64`。

---

## File structure

- Create: `src/quote_app/sites/oppo_diagnostics.py` — OPPO 搜索卡截图与 JSON 侧车采集；不含浏览器控制和任务重试。
- Modify: `src/quote_app/services/web_run.py` — 在既有诊断回调内精确分发给荣耀或 OPPO 采集器。
- Modify: `src/quote_app/sites/official_brands/oppo.py` — 只将“已进入搜索结果选择阶段而无法继续”的异常标为 OPPO 搜索选择失败，供诊断器精确识别；不改变选择算法。
- Create: `tests/unit/test_oppo_search_diagnostics.py` — JSON 字段、拒绝原因、数据边界与写入失败的单元契约。
- Modify: `tests/contract/test_official_oppo_live.py` — 搜索结果阶段异常的类型/标记契约；正常选卡不触发诊断。
- Modify: `tests/integration/test_fixture_capture.py` — 通过真实 runner 断言 OPPO 搜索失败留下诊断路径，采集故障不覆盖 `LAYOUT_CHANGED`。

## Task 1: Define OPPO search-selection failure and its read-only diagnostic contract

**Files:**

- Modify: `src/quote_app/sites/official_brands/oppo.py:320-366`
- Modify: `tests/contract/test_official_oppo_live.py`

**Interfaces:**

- Produces: `OppoSearchCardSelectionError(LayoutRecognitionError)` with stable attribute `oppo_search_card_failure = True`.
- Consumes: existing `_wait_for_search_results()` and `_preferred_exact_result_link()`; retains their current return values and timing.

- [ ] **Step 1: Write the failing contract tests**

```python
def test_oppo_unstable_result_cards_raise_diagnostic_search_selection_error() -> None:
    page = _OppoFixturePage("normal.html")
    page.search_waiting_for_enter = False
    page.result_cards_never_stabilize = True

    with pytest.raises(OppoSearchCardSelectionError):
        _adapter().observe(_task(), page)


def test_oppo_exact_card_with_no_approved_url_keeps_search_diagnostic_marker() -> None:
    page = _OppoFixturePage("normal.html")
    _set_result_cards(page, [("OPPO A5m 水晶粉 暂时缺货", "javascript:void(0)")])

    with pytest.raises(OppoSearchCardSelectionError):
        _adapter().observe(_task(), page)
```

- [ ] **Step 2: Run the two tests to verify they fail**

Run: `pytest tests/contract/test_official_oppo_live.py -k 'diagnostic_search_selection_error or diagnostic_marker' -v`

Expected: FAIL because `OppoSearchCardSelectionError` does not yet exist or the legacy `LayoutRecognitionError` is raised.

- [ ] **Step 3: Add the minimal marker exception**

```python
class OppoSearchCardSelectionError(LayoutRecognitionError):
    """OPPO search results are visible but cannot safely select a card."""

    oppo_search_card_failure = True
```

Replace only the two search-result-stage `LayoutRecognitionError` raises: results never stabilize, and exact-model cards have no approved numeric detail URL. Do not alter selectors, matching, timing, click mechanics, or no-model behavior.

- [ ] **Step 4: Run the focused contract tests**

Run: `pytest tests/contract/test_official_oppo_live.py -k 'diagnostic_search_selection_error or diagnostic_marker' -v`

Expected: PASS.

- [ ] **Step 5: Commit the independently valid marker change**

```bash
git add src/quote_app/sites/official_brands/oppo.py tests/contract/test_official_oppo_live.py
git commit -m "feat: mark OPPO search-card selection failures"
```

## Task 2: Capture bounded OPPO card diagnostics

**Files:**

- Create: `src/quote_app/sites/oppo_diagnostics.py`
- Create: `tests/unit/test_oppo_search_diagnostics.py`

**Interfaces:**

- Consumes: `capture_oppo_search_diagnostic(task: WebsiteTask, error: BaseException, screenshot_path: Path, page: Any) -> Path | None`.
- Produces: the requested `*.png` plus UTF-8 sidecar `*.png.json`; JSON card fields are `aria_label`, `title_attribute`, `visible_text`, `nested_titles`, `raw_href`, `normalized_href`, `model_matches`, and `decision`.

- [ ] **Step 1: Write failing unit tests with small fake locators**

```python
def test_oppo_diagnostic_records_generic_link_and_nested_exact_model(tmp_path: Path) -> None:
    path = capture_oppo_search_diagnostic(
        _task(), OppoSearchCardSelectionError("unavailable"),
        tmp_path / "diagnostic.png", _page_with_generic_link(),
    )
    payload = json.loads((tmp_path / "diagnostic.png.json").read_text())

    assert path == tmp_path / "diagnostic.png"
    assert payload["cards"][0]["aria_label"] == "查看商品"
    assert payload["cards"][0]["nested_titles"] == ["OPPO A5m 水晶粉 暂时缺货"]
    assert payload["cards"][0]["decision"] == "accepted_exact_detail"


def test_oppo_diagnostic_does_not_reject_sold_out_or_missing_5g(tmp_path: Path) -> None:
    payload = _capture_payload(tmp_path, "OPPO A5m 水晶粉 暂时缺货")
    assert payload["cards"][0]["model_matches"] is True
    assert payload["cards"][0]["decision"] == "accepted_exact_detail"


def test_oppo_diagnostic_records_near_model_and_invalid_url_separately(tmp_path: Path) -> None:
    payload = _capture_two_cards(tmp_path)
    assert [card["decision"] for card in payload["cards"]] == [
        "model_not_matched", "approved_detail_url_missing"
    ]
```

Include assertions that text is bounded, credential-bearing URLs serialize as an empty string, non-OPPO/non-official errors return `None`, and a screenshot write exception returns `None` without raising.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/unit/test_oppo_search_diagnostics.py -v`

Expected: FAIL with `ModuleNotFoundError: quote_app.sites.oppo_diagnostics`.

- [ ] **Step 3: Implement the isolated collector**

```python
def capture_oppo_search_diagnostic(task, error, screenshot_path, page):
    if not _is_oppo_search_card_failure(task, error):
        return None
    page.screenshot(path=str(target), full_page=False)
    payload = _payload(task, page, target)
    target.with_suffix(f"{target.suffix}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target
```

Use only visible dialog-scoped card links. Cap cards at 12 and every string at 240 normalized characters. Reuse OPPO’s approved-URL and base-model helpers for consistency, but never call `.click()`, `.fill()`, `.press()`, `.goto()` or `wait_for_timeout()`.

- [ ] **Step 4: Run unit test file**

Run: `pytest tests/unit/test_oppo_search_diagnostics.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the collector**

```bash
git add src/quote_app/sites/oppo_diagnostics.py tests/unit/test_oppo_search_diagnostics.py
git commit -m "feat: capture OPPO search-card diagnostics"
```

## Task 3: Attach OPPO diagnostics to the existing runner evidence chain

**Files:**

- Modify: `src/quote_app/services/web_run.py:17,243-248`
- Modify: `tests/integration/test_fixture_capture.py`

**Interfaces:**

- Consumes: existing `WebsiteTaskRunner.diagnostic_capture(task, error, path)` callback and its safe `evidence/diagnostics` path validation.
- Produces: `WebsiteResult.diagnostic_path` for a marked OPPO search-card error, with original `LAYOUT_CHANGED` code intact.

- [ ] **Step 1: Write failing integration tests**

```python
def test_oppo_search_selection_failure_persists_runner_diagnostic(tmp_path: Path) -> None:
    task = _oppo_official_task()
    runner = _runner_with_oppo_selection_failure(tmp_path)

    runner.run((task,))

    result = repository.load_result(task.task_id)
    assert result.error_code == "LAYOUT_CHANGED"
    assert result.diagnostic_path is not None
    assert result.diagnostic_path.with_suffix(".png.json").is_file()


def test_oppo_diagnostic_capture_fault_preserves_layout_changed(tmp_path: Path) -> None:
    runner = _runner_with_oppo_selection_failure(tmp_path, screenshot_raises=True)
    runner.run((_oppo_official_task(),))

    result = repository.load_result("oppo-task")
    assert result.error_code == "LAYOUT_CHANGED"
    assert result.diagnostic_path is None
```

- [ ] **Step 2: Run the focused integration tests to verify failure**

Run: `pytest tests/integration/test_fixture_capture.py -k 'oppo_search_selection_failure or oppo_diagnostic_capture_fault' -v`

Expected: FAIL because the current `web_run` callback only calls the HONOR collector.

- [ ] **Step 3: Add exact OPPO callback dispatch**

```python
def _capture_search_diagnostic(task, error, path, page):
    return (
        capture_honor_search_diagnostic(task, error, path, page)
        or capture_oppo_search_diagnostic(task, error, path, page)
    )
```

Pass this function to `WebsiteTaskRunner`. Do not modify runner, scheduler, serialization, Excel code, capture policy, or non-OPPO collectors.

- [ ] **Step 4: Run integration tests and frozen representative regressions**

Run: `pytest tests/integration/test_fixture_capture.py -k 'oppo_search_selection_failure or oppo_diagnostic_capture_fault' -v && pytest tests/contract/test_official_oppo_live.py tests/unit/test_oppo_search_diagnostics.py tests/unit/test_honor_search_diagnostics.py tests/regression/test_honor_official_baseline.py -v`

Expected: PASS; the first test has `LAYOUT_CHANGED` and a valid local diagnostic path; the second remains `LAYOUT_CHANGED` without a path.

- [ ] **Step 5: Commit runner wiring**

```bash
git add src/quote_app/services/web_run.py tests/integration/test_fixture_capture.py
git commit -m "feat: persist OPPO search diagnostics"
```

## Task 4: Verify and build a separately named Mac diagnostic package

**Files:**

- Modify: no source files unless test failures prove a defect.
- Create: `dist-official-oppo-65/福建移动铺货报价助手.app`

**Interfaces:**

- Consumes: commits from Tasks 1–3 and existing `packaging/quotation_app.spec`.
- Produces: a new Mac test app preserving `.64`, with diagnostics in `~/Library/Application Support/福建移动铺货报价助手/evidence/diagnostics/`.

- [ ] **Step 1: Run static and focused verification**

Run: `ruff check src/quote_app/sites/oppo_diagnostics.py src/quote_app/sites/official_brands/oppo.py src/quote_app/services/web_run.py tests/unit/test_oppo_search_diagnostics.py && mypy src/quote_app/sites/oppo_diagnostics.py src/quote_app/sites/official_brands/oppo.py src/quote_app/services/web_run.py && pytest tests/contract/test_official_oppo_live.py tests/unit/test_oppo_search_diagnostics.py tests/unit/test_honor_search_diagnostics.py tests/regression/test_honor_official_baseline.py -v`

Expected: all pass.

- [ ] **Step 2: Run relevant channel/Excel isolation regression**

Run: `pytest tests/contract/test_official_honor_live.py tests/contract/test_official_xiaomi_live.py tests/contract/test_tmall_adapter.py tests/integration/test_web_to_excel.py -v`

Expected: all pass; no non-OPPO behavior changes.

- [ ] **Step 3: Build without overwriting `.64`**

Run: `rm -rf build-official-oppo-65 dist-official-oppo-65 && pyinstaller --noconfirm --clean --distpath dist-official-oppo-65 --workpath build-official-oppo-65 packaging/quotation_app.spec`

Expected: `dist-official-oppo-65/福建移动铺货报价助手.app` exists and `.64` remains unchanged.

- [ ] **Step 4: Validate package signature and smoke launch metadata**

Run: `codesign --verify --deep --strict dist-official-oppo-65/福建移动铺货报价助手.app && plutil -p dist-official-oppo-65/福建移动铺货报价助手.app/Contents/Info.plist`

Expected: signature validation succeeds and bundle metadata reads successfully.

- [ ] **Step 5: Commit verification-only documentation if changed**

```bash
git status --short
# Stage only an intentionally updated diagnostic handoff/checklist file.
git commit -m "docs: record OPPO diagnostic package verification"
```

## Self-review

- Scope coverage: Tasks 1–3 implement the required trigger, bounded per-card fields, storage path, error isolation, and existing task-result diagnostic link; Task 4 enforces a new `.65` package and representative non-OPPO isolation.
- Placeholder scan: no `TODO`, `TBD`, “similar to”, or unspecified test steps remain.
- Type consistency: the marker is `OppoSearchCardSelectionError`; the collector is `capture_oppo_search_diagnostic`; both the runner callback and tests use `(WebsiteTask, BaseException, Path, Any) -> Path | None`.

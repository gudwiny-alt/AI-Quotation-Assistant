# 荣耀搜索匹配诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在荣耀官网搜索卡片无法唯一匹配时，保留本地、可复查的搜索页诊断材料。

**Architecture:** 新增一个站点专用的纯诊断模块。生产网站运行服务将当前的单一自动化页面闭包传给该模块；现有任务运行器仍只负责为技术失败安全分配诊断目录和持久化返回的 PNG 路径。

**Tech Stack:** Python 3.12、pytest、Playwright 同步页面接口。

## Global Constraints

- 仅适用于 HONOR 官网的两个稳定匹配失败码。
- 不修改匹配规则、详情页、正式截图、Excel、京东、天猫或窗口控制。
- 诊断不可包含凭据、Cookie 或个人浏览器资料。

---

### Task 1: 锁定诊断材料格式

**Files:**
- Create: `tests/unit/test_honor_search_diagnostics.py`

**Interfaces:**
- Consumes: `capture_honor_search_diagnostic(task, error, screenshot_path, page)`。
- Produces: 写入 PNG 和同名 JSON，JSON 含 URL、机型、候选卡片文本和链接；无关任务返回 `None`。

- [ ] **Step 1: Write the failing test**

```python
def test_capture_honor_match_failure_writes_search_page_png_and_cards(tmp_path):
    path = capture_honor_search_diagnostic(task, error, tmp_path / "task.png", page)
    assert path == tmp_path / "task.png"
    assert json.loads(path.with_suffix(".png.json").read_text())["cards"][0]["text"]

def test_capture_ignores_non_honor_match_failures(...):
    assert capture_honor_search_diagnostic(...) is None
```

- [ ] **Step 2: Run test to verify RED**

Run: `python -m pytest -q tests/unit/test_honor_search_diagnostics.py`

### Task 2: Implement the diagnostic boundary

**Files:**
- Create: `src/quote_app/sites/honor_diagnostics.py`
- Modify: `src/quote_app/services/web_run.py`

**Interfaces:**
- `capture_honor_search_diagnostic(task: WebsiteTask, error: BaseException, screenshot_path: Path, page: Any) -> Path | None`
- The web-run service passes it as the existing `WebsiteTaskRunner(diagnostic_capture=...)` callback.

- [ ] **Step 1: Implement gated metadata and screenshot persistence**

Capture no more than 12 visible `li.grid-items` cards, each bounded to 240 characters. Write JSON only after validation and return the requested PNG only when the screenshot was produced.

- [ ] **Step 2: Wire the existing runner callback**

Create a closure over `browser.automation_page()` in `run_website_tasks`; it delegates only to the new HONOR-gated function. Do not change runner behavior.

- [ ] **Step 3: Verify focused and regression suites**

Run: `python -m pytest -q tests/unit/test_honor_search_diagnostics.py tests/unit/test_web_run_service.py tests/contract/test_official_honor_live.py tests/regression/test_honor_official_baseline.py`.

### Task 3: Package a diagnostic-only Mac build

**Files:**
- Modify: `src/quote_app/app.py`
- Modify: `tests/unit/test_app.py`
- Create: `dist-honor-search-diagnostic/`

- [ ] **Step 1: Add a unique user-visible build label**

Name the build “荣耀搜索诊断版” and keep its statement limited to collecting local search diagnostics on matching failure.

- [ ] **Step 2: Build and verify**

Build in a new dist directory, ad-hoc sign it and verify with `codesign --verify --deep --strict`.

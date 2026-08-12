# Task 4 Report — Frozen Regressions and Huawei Mac Acceptance Build

## Outcome and boundary

Task 4 completed the automatic regression, quality, packaging-discovery,
PyInstaller and macOS ad-hoc signing gates for the isolated Huawei acceptance
package. It did not launch VMALL and does not claim Huawei live-site acceptance.

The only production-file change in this task is release metadata:

```text
华为官网验收版（仅官网，荣耀/小米/OPPO/vivo冻结）2026.08.13.73
```

The existing UI run-mode mapping already exposes
`华为官网验收（仅官网）`. Its default selection, scheduler, website logic,
Excel mapping and all frozen adapters remain unchanged.

## Regression and quality evidence

Commands were run from `.worktrees/core-excel` on 2026-08-13.

```text
.venv/bin/pytest -q \
  tests/contract/test_official_huawei_live.py \
  tests/integration/test_huawei_official_pipeline.py \
  tests/unit/test_official_brand_factory.py \
  tests/regression/test_official_brand_isolation.py \
  tests/regression/test_official_capture_reader_isolation.py
110 passed in 4.08s

.venv/bin/pytest -q \
  tests/contract/test_official_honor_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_vivo_live.py \
  tests/contract/test_jd_adapter.py \
  tests/contract/test_tmall_adapter.py \
  tests/integration/test_web_to_excel.py \
  tests/unit/test_macos_capture_runtime.py
703 passed in 3.96s

.venv/bin/ruff check .
All checks passed!

.venv/bin/mypy src/quote_app/sites/official_brands \
  src/quote_app/tasks/runner.py
Success: no issues found in 9 source files

.venv/bin/pytest -q tests/smoke/test_packaging_spec.py
1 passed in 0.24s
```

After adding explicit package-discovery assertions for OPPO, vivo and Huawei:

```text
.venv/bin/pytest -q tests/smoke/test_packaging_spec.py tests/unit/test_app.py
53 passed in 0.37s

.venv/bin/ruff check tests/smoke/test_packaging_spec.py \
  src/quote_app/app.py tests/unit/test_app.py
All checks passed!
```

The broad suite inside the restricted sandbox produced:

```text
.venv/bin/pytest -q
1 failed, 2736 passed in 51.39s
```

The sole failure was
`tests/integration/test_persistent_browser.py::test_cookie_and_local_storage_survive_close_and_reopen`.
Its temporary `127.0.0.1` HTTP server failed to bind with
`PermissionError: [Errno 1] Operation not permitted`. The exact test was then
rerun outside the network sandbox with approval:

```text
.venv/bin/pytest -q \
  tests/integration/test_persistent_browser.py::test_cookie_and_local_storage_survive_close_and_reopen
1 passed in 8.39s
```

This records the environmental failure rather than presenting the first broad
command as green. No Huawei or frozen-site test failed.

## Packaging evidence

Pre-build guards proved both new roots absent and the accepted `.72` app
present. Its main-executable SHA-256 was recorded before the build:

```text
a4ce1c23b7ae18ea36f57cf3fc24285854ffd35ba5ab7607545b20445ce81cdf
```

Build command:

```text
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-huawei-73 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-official-huawei-73 \
  --workpath build-official-huawei-73 \
  packaging/quotation_app.spec
```

PyInstaller 6.21.0 exited successfully and created:

```text
/Users/yangguowei/Documents/资金物流平台铺货报价智能体/.worktrees/core-excel/dist-official-huawei-73/福建移动铺货报价助手.app
```

The app is 226 MB. Recursive archive inspection contains:

- `quote_app.app`;
- `quote_app.sites.official_brands.huawei`;
- the frozen Xiaomi, OPPO and vivo independent adapters.

The new main-executable SHA-256 is:

```text
8466b02c96979161ebef790c63e6927cf6238cd41ca63e7d7dd67ca748cfee69
```

Signing commands:

```text
codesign --force --deep --sign - \
  dist-official-huawei-73/福建移动铺货报价助手.app
codesign --verify --deep --strict --verbose=2 \
  dist-official-huawei-73/福建移动铺货报价助手.app
```

Strict verification reports `valid on disk` and
`satisfies its Designated Requirement`. `codesign -dv` reports an arm64,
ad-hoc signed bundle with identifier `com.fjmobile.quotation`.

`spctl --assess` returned `internal error in Code Signing subsystem` for this
ad-hoc, non-notarized local test build and is not reported as passing. The
repository's established strict `codesign` validation did pass.

The `.72` main-executable hash remained unchanged after the build.

## Required Mac live acceptance

The automatic status is **PRECHECK PASSED / LIVE VMALL NOT RUN**.

1. Select `华为官网验收（仅官网）` and run
   `华为畅享 90 Pro Max / 8GB+256GB / 曜金黑`; accept only a normal result or
   a correctly evidenced no-model result.
2. Run one currently sold VMALL phone and verify exact capacity/color, the
   lowest valid current price, a clear four-proof full-screen image, `AK`,
   `AN`, and report totals.

Only user confirmation of both steps may change Huawei from `预检通过` to
`完整通过`.

## Risks and handoff

- VMALL is dynamic; real selectors, product availability, and security pages
  must still be observed in the user-controlled Mac session.
- The development worktree contains extensive historical dirty changes and
  build artifacts. The `.73` build/dist directories are intentionally not
  committed. Task 4 stages only its exact release metadata, test, report and
  Huawei documentation changes.

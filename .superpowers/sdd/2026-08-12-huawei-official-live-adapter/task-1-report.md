# Task 1 Report — VMALL True-Structure Contract

## Outcome

Task 1 is complete at the intended TDD RED checkpoint. This commit adds only
sanitized VMALL fixtures and the Huawei live-contract harness/tests; it does not
add or modify a Huawei production adapter or any frozen site implementation.

## Fixture provenance

- Public site observed: `https://www.vmall.com/`
- Public detail evidence rechecked on 2026-08-13: entering
  `https://www.vmall.com/product/comdetail/index.html?prdId=10086259366534`
  redirects to
  `https://item.vmall.com/product/comdetail/index.html?prdId=10086259366534`.
- Public detail-route variants retained by the contract:
  `/product/<numeric>.html` and
  `/product/comdetail/index.html?prdId=<numeric>`.
- The fixtures are sanitized snapshots, not copied executable page source.
  The detail contract uses the reviewed title node
  `div#prd-detail-name[data-testid='prd-detail-name']`, red
  `data-testid='vui_text_container'` price candidates scoped to the purchase
  summary, and label-anchored `版本` / `颜色` sibling groups. Selection is
  represented by the observed red border/text style, not invented `adopted`
  or `data-group` markers.
- Current public reference amounts retained in the price fixture are `4999`
  and `5199`; the excluded reference/promotional amounts are `6499`, `750`,
  `208.29`, `1000` and `699`.
- No test-only `official-huawei-*` selector namespace was added to the new
  fixtures. Blank, home, results and detail DOMs are separate and verified not
  to leak into one another.

## Contract coverage

- One real homepage fill + Enter submission; 40-tick result completion and
  late exact-card behavior.
- Exact-model selection, derived/accessory rejection, sold-out card entry and
  strict numeric VMALL detail routes.
- Full RAM+storage selection, storage-only fallback, 20-tick legal-no outcomes,
  preselected state and sold-out independence.
- Lowest valid current price, late/stable price behavior, ambiguity and
  identity/configuration drift.
- Resume from a numeric detail checkpoint.
- Four formal proof selectors (title, selected current-price candidate,
  capacity and color),
  current-scale fit, idempotent 80%, one signed scroll bounded to 160 pixels,
  overlay and missing-geometry fail-closed behavior.

## Verification evidence

Commands run from `.worktrees/core-excel`:

```text
.venv/bin/ruff check tests/contract/test_official_huawei_live.py
All checks passed!

.venv/bin/pytest -q tests/contract/test_official_huawei_live.py -k 'fixture or capture_harness'
8 passed, 55 deselected in 0.10s

.venv/bin/pytest -q tests/contract/test_official_huawei_live.py
8 passed, 55 failed in 1.13s
```

All 55 production behavior cases fail at the same intentional boundary:

```text
ModuleNotFoundError: No module named 'quote_app.sites.official_brands.huawei'
```

This is the expected RED state for Task 1. The eight fixture/harness self-checks
are green, collection succeeds, and no failure reaches unrelated production
code.

## Risks and handoff

- Public VMALL content is dynamic; Task 2 must implement only the approved
  semantic structures and routes, and live acceptance must reconfirm the
  current page before release.
- The fixture deliberately contains multiple purchase-summary current-price
  candidates plus reference, promotion and duplicate sticky-footer amounts.
  The harness locates the price selected by business extraction dynamically;
  it does not mark or hard-code `4999` as a fixture-only adopted node.
- Legal-no capacity/color observations must return two same-screen rectangles:
  `title` plus the complete corresponding `capacity_group` (`版本`) or
  `color_group` (`颜色`). A group rectangle alone is not valid evidence.
- Task 2 must not weaken exact-model, option-completeness, price-stability,
  route or capture-geometry assertions merely to make this contract green.

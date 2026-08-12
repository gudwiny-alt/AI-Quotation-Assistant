# Task 1 Report — VMALL True-Structure Contract

## Outcome

Task 1 is complete at the intended TDD RED checkpoint. This commit adds only
sanitized VMALL fixtures and the Huawei live-contract harness/tests; it does not
add or modify a Huawei production adapter or any frozen site implementation.

## Fixture provenance

- Public site observed: `https://www.vmall.com/`
- Public detail evidence observed on 2026-08-12:
  `https://www.vmall.com/product/comdetail/index.html?prdId=10086259366534`
- Public detail-route variants retained by the contract:
  `/product/<numeric>.html` and
  `/product/comdetail/index.html?prdId=<numeric>`.
- The fixtures are sanitized snapshots, not copied executable page source.
  They preserve the observed VMALL semantics needed by the design: homepage
  search, product-card links, product title, current/reference/promotional
  amounts, `版本`, `颜色`, selected state and sold-out copy.
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
- Four formal proof selectors (title, adopted price, capacity and color),
  current-scale fit, idempotent 80%, one signed scroll bounded to 160 pixels,
  overlay and missing-geometry fail-closed behavior.

## Verification evidence

Commands run from `.worktrees/core-excel`:

```text
.venv/bin/ruff check tests/contract/test_official_huawei_live.py
All checks passed!

.venv/bin/pytest -q tests/contract/test_official_huawei_live.py -k 'fixture or capture_harness'
4 passed, 53 deselected in 0.09s

.venv/bin/pytest -q tests/contract/test_official_huawei_live.py
4 passed, 53 failed in 0.89s
```

All 53 production behavior cases fail at the same intentional boundary:

```text
ModuleNotFoundError: No module named 'quote_app.sites.official_brands.huawei'
```

This is the expected RED state for Task 1. The four fixture/harness self-checks
are green, collection succeeds, and no failure reaches unrelated production
code.

## Risks and handoff

- Public VMALL content is dynamic; Task 2 must implement only the approved
  semantic structures and routes, and live acceptance must reconfirm the
  current page before release.
- The fixture deliberately contains multiple current-price candidates. The
  contract marks the adopted `4999` node separately for the four-proof capture
  harness while still requiring production price extraction to compare all
  valid current candidates.
- Task 2 must not weaken exact-model, option-completeness, price-stability,
  route or capture-geometry assertions merely to make this contract green.

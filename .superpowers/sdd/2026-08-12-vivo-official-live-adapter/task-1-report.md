# Task 1 report — vivo official live contract

## Created files

- `tests/fixtures/sites/official_live/vivo/search_results.html`
- `tests/fixtures/sites/official_live/vivo/detail_normal.html`
- `tests/fixtures/sites/official_live/vivo/detail_missing_capacity.html`
- `tests/fixtures/sites/official_live/vivo/detail_missing_color.html`
- `tests/contract/test_official_vivo_live.py`
- `.superpowers/sdd/2026-08-12-vivo-official-live-adapter/task-1-report.md`

## RED verification

Command:

```text
.venv/bin/pytest -q tests/contract/test_official_vivo_live.py
```

Result: collection succeeds; two fixture/provenance checks pass and the
behavior contracts are RED at runtime because importing
`quote_app.sites.official_brands.vivo` raises `ModuleNotFoundError`.

Reason: Task 1 intentionally adds no production code.  The independent vivo
adapter module does not yet exist and the factory still routes `维沃` to its
placeholder class, so the new executable contract cannot pass until the
dedicated adapter is implemented.

## Covered contract scenarios

- The fixtures retain the 2026-08-12 read-only shapes and provenance notes:
  homepage search input, actual `www.vivo.com.cn/search/searchResult` route,
  `div.result-card[data-position][data-skuid]` cards, and official numeric
  `shop.vivo.com.cn/product/<id>?skuId=...` detail URLs. Detail retains the
  public `product.d7d3a960.js` hierarchy through `h1.name`, direct-text
  `p.sale-price`, market price, and `dl.sku-module.specs` groups.
- The normal flow requires entry navigation, one `vivo X200` fill and Enter
  submission before searching. Exact base-model selection excludes Pro, Plus,
  Ultra, Max, X200S, and X200T; it verifies the final observed numeric URL.
- An invalid first exact URL is skipped; a sold-out or different-card-color
  exact card remains eligible; every exact card with an invalid URL is a named
  technical selection failure.
- iQOO is rejected before navigation/input for case and inner-space variants,
  without fixing an implementation exception subclass.
- A late exact card is revealed only after multiple waits; ordinary loading
  cannot cause early `NO_MODEL`. Visible terminal `no-goods` is required for
  the fixture's legal no-model state, which retains
  `search_keyword`/`result_region` evidence only after the full poll window.
- Capacity then color are selected (without repeating uniquely selected
  options); an exact disabled target is legal no. All invalid exact URLs,
  detail-title drift, and final numeric URL identity drift are technical
  failures.
- Current price is unavailable until both target selections. Whole-offer polls
  are `4499 → 4399 → 4399`, so the adopted final stable value is 4399; a
  non-converging whole-offer sequence is rejected. Coupon, installment, list,
  and accessory contamination are excluded.
- Capture derives title/current price/selected version/selected color from
  the real fixture nodes; its harness computes DOMRects and hit testing from
  those nodes. It covers 100% fit, 80%-only fit, one bounded geometry scroll,
  overlap failure after one attempt, and a disappearing proof. Saving AK/AN
  after an operating-system capture failure belongs to the later runner task.

## Blockers and concerns

- No implementation blocker for the contract fixture/test deliverable.
- The RED state is runtime-only: the module collects and all behavior tests
  fail only when they request the missing independent vivo adapter. No test
  permanently asserts that the module must remain absent.

## Commit

`82aa5fd` — initial Task 1 contract; `7e40c15` — fix round 1;
`c9366f4` — fix round 2; fix round 3 commit pending.

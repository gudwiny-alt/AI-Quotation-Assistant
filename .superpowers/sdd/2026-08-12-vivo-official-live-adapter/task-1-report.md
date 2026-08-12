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

Result: collection succeeds; fixture-provenance test passes and the behavior
contracts are RED at runtime because importing
`quote_app.sites.official_brands.vivo` raises `ModuleNotFoundError`.

Reason: Task 1 intentionally adds no production code.  The independent vivo
adapter module does not yet exist and the factory still routes `维沃` to its
placeholder class, so the new executable contract cannot pass until the
dedicated adapter is implemented.

## Covered contract scenarios

- The fixtures retain sanitized structure from the public 2026-08-12 vivo
  chunks `product.d7d3a960.js` and `productlist.1495b354.js`, with source URL,
  capture date, and sanitization note in the fixture. Search uses
  `ul.spu-item-list > li.spu-item > a[target=_blank]`; detail uses
  `summary_price/sale-price` and `dl.sku-module.specs` version/color groups.
- Exact base-model search selection excludes Pro, Plus, Ultra, Max, X200S, and
  X200T; it verifies only the final observed numeric detail URL/state, allowing
  either a validated click or navigation to the observed card URL.
- An invalid first exact URL is skipped; a sold-out or different-card-color
  exact card remains eligible; every exact card with an invalid URL is a named
  technical selection failure.
- iQOO is rejected before navigation/input for case and inner-space variants,
  without fixing an implementation exception subclass.
- A late exact card is revealed from a retained real-shaped list node at the
  fixture's explicit settlement boundary; ordinary loading cannot cause early
  `NO_MODEL`; stable absence retains
  `search_keyword`/`result_region` evidence only after the full poll window.
- Capacity then color are selected (without repeating uniquely selected
  options); an exact disabled target is legal no. Detail title and product
  identity drift after card entry are technical failures.
- Current price is unavailable until both target selections. Whole-offer polls
  are `4499 → 4399 → 4399`, so the adopted final stable value is 4399; a
  non-converging whole-offer sequence is rejected. Coupon, installment, list,
  and accessory contamination are excluded.
- Capture requires the adopted `¥4399` title/price/version/color proofs through
  DOMRect + `elementFromPoint`; it covers 100% fit, 80%-only fit, one
  geometry-bounded light scroll, overlap failure, and retaining observation
  price when capture preparation fails.

## Blockers and concerns

- No implementation blocker for the contract fixture/test deliverable.
- The RED state is runtime-only: the module collects and all behavior tests
  fail only when they request the missing independent vivo adapter. No test
  permanently asserts that the module must remain absent.

## Commit

`82aa5fd` — initial Task 1 contract; `7e40c15` — fix round 1; fix round 2
commit pending.

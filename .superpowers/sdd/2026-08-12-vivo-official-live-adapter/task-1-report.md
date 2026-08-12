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

Result: RED during collection with `ModuleNotFoundError: No module named
'quote_app.sites.official_brands.vivo'`.

Reason: Task 1 intentionally adds no production code.  The independent vivo
adapter module does not yet exist and the factory still routes `维沃` to its
placeholder class, so the new executable contract cannot pass until the
dedicated adapter is implemented.

## Covered contract scenarios

- Raw, desensitized vivo-shaped DOM fixtures use class, ARIA, semantic element,
  href, hierarchy, and real text patterns only. `data-vivo-role`,
  `data-screen`, `data-option-kind`, and similar production-visible fixture
  hooks have been removed.
- Exact base-model search selection excludes Pro, Plus, Ultra, Max, T, and S;
  it clicks an approved numeric `/product/<数字>` card and verifies final detail
  URL/state rather than prescribing direct navigation.
- An invalid first exact URL is skipped; a sold-out or different-card-color
  exact card remains eligible; every exact card with an invalid URL is a named
  technical selection failure.
- iQOO is rejected before navigation/input for case and whitespace variants.
- A late exact card arriving on the bounded final poll must be quoted; ordinary
  loading cannot cause early `NO_MODEL`; stable absence retains
  `search_keyword`/`result_region` evidence only after the full poll window.
- Capacity then color are selected (without repeating uniquely selected
  options); an exact disabled target is legal no. Detail title and product
  identity drift after card entry are technical failures.
- Current price is unavailable until both target selections, then delayed until
  repeated reads settle. A permanently changing selected-offer price is not
  quoted. Coupon, installment, list, and accessory contamination are excluded.
- Capture works from concrete title/current-price/capacity/color nodes. It
  preserves an already fitting viewport; otherwise it applies 80% before one
  positive bounded position calculated from proof geometry, handles an overlay,
  and fails closed for a still-unfit or disappearing proof.

## Blockers and concerns

- No implementation blocker for the contract fixture/test deliverable.
- The RED state is now runtime-only: the test module collects and one explicit
  seam test confirms the absent module. All production-behavior cases fail
  solely when they request the missing independent vivo adapter.

## Commit

`82aa5fd` — initial Task 1 contract; fix-round commit pending.

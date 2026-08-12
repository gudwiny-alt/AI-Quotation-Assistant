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

- exact vivo base-model card selection, excluding Pro and S derivatives;
- accepted numeric `/product/<数字>` URL, including a later approved card after
  the first exact card URL is invalid;
- card color and stock state do not preclude entering an exact model detail;
- all exact result URLs invalid is a technical selection failure;
- iQOO is rejected before any navigation or search input;
- complete search wait produces `NO_MODEL` with `search_keyword` and
  `result_region` evidence;
- missing capacity and missing color produce their respective legal-no
  outcomes and evidence roles;
- select capacity before color and take the lower valid current main-product
  price while excluding coupon, instalment, crossed-out, and accessory prices;
- capture keeps four proofs in place when already visible, tries 80% once when
  needed, uses one bounded position attempt for an obscuring floating layer,
  and then fails closed.

## Blockers and concerns

- No implementation blocker for the contract fixture/test deliverable.
- The planned RED state is an import-time failure because the specified vivo
  adapter module has not yet been created; this is the expected Task 1 failure
  mode, not a fixture or test-harness error.

## Commit

`7654cef` — `test: define vivo official live contract`

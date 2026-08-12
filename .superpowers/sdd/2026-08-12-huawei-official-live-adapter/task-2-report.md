# Task 2 Report — Independent Huawei VMALL Adapter

## Outcome

Task 2 is complete. `HuaweiOfficialAdapter` is an independent
`LiveOfficialAdapterBase` implementation registered only for the canonical
brand `华为`. The frozen HONOR, Xiaomi, OPPO, vivo, JD and Tmall adapters were
not modified.

The adapter implements the approved VMALL behavior contract:

- one real homepage search, a complete 40-tick result window and exact base
  model matching;
- only approved HTTPS `www.vmall.com` / `item.vmall.com` numeric product
  routes, with redirect identity validation and route-safe resume;
- exact full RAM+storage selection, storage-only fallback only when the page
  has no RAM dimension, exact color selection and 20-tick option windows;
- product-level sold-out text does not prevent detail entry or selection;
- lowest valid current purchase-summary price, excluding struck-through and
  unrelated promotional amounts, stable for three seconds inside a five-second
  budget;
- formal four-proof capture (`title`, `price`, `capacity`, `color`), current
  scale first, idempotent 80% only when required, and at most one geometry
  scroll bounded to 160 CSS pixels;
- legal-no model evidence and same-screen title plus complete capacity/color
  option-group evidence.

## Approved mechanical evidence compatibility

Huawei's complete legal-no option-group evidence required a minimal public
role compatibility extension. It was explicitly approved for Task 2 and does
not change any existing site's output:

- capacity unavailable continues to accept `("capacity",)` and additionally
  accepts `("title", "capacity_group")`;
- color unavailable continues to accept `("color",)` (and the annotation
  layer's existing `("capacity", "color")`) and additionally accepts
  `("title", "color_group")`;
- every other unapproved group-role combination remains fail-closed.

The new unit contract carries both approved combinations through
`OfficialBusinessState -> VerifiedSemanticState -> AdapterObservation ->
WebsiteTaskRunner/CaptureRequest -> annotation validation`, while four invalid
combinations are rejected.

## Approved Task 1 harness-only corrections

The first implementation run reached `59 passed, 4 failed`. Two failures were
independently reproduced as contradictions in the approved test harness, then
fixed only after parent-task approval:

1. The real default detail fixture remains preselected. The test whose purpose
   is to require two clicks now explicitly removes both target selection styles
   before observing; the separate preselected test still proves no re-click.
2. The delayed-option helper now removes the target's selected style before it
   hides the option. This allows its deterministic wait counter to reach tick
   19 before the adapter selects the revealed target.

No production business rule or real fixture default state was weakened by
these two harness-only corrections.

## Verification evidence

### Review fix round

The independent review requested four focused hardening changes. They were
implemented without changing any frozen adapter or public evidence layer:

- exact base-model boundaries now reject `Pro`, `Pro+`, `Plus`, `Ultra` and
  `Max` candidates for a non-derived target, while a target that itself names
  `Pro` still accepts only legal capacity/color/stock tails;
- price extraction and formal capture now share the same structurally scoped
  main purchase-summary current-price selector. Promotional nodes carrying the
  same public `data-testid`, reference/line-through prices and a duplicate
  sticky-bar price cannot enter the candidate set;
- the capture harness resolves the formal CSS selector naturally and no longer
  applies an extra fixture-only purchase-price predicate;
- common Huawei login and security URL/DOM states explicitly raise the
  approved zero-cost manual pause errors.

The fix round added 12 Huawei contract cases. It did not modify HONOR, Xiaomi,
OPPO, vivo, JD, Tmall, Excel, runtime, or the approved public compatibility
extension.

Commands run from `.worktrees/core-excel`:

```text
.venv/bin/pytest -q tests/contract/test_official_huawei_live.py
75 passed in 13.60s

.venv/bin/pytest -q \
  tests/contract/test_official_huawei_live.py \
  tests/unit/test_official_brand_factory.py \
  tests/regression/test_official_brand_isolation.py \
  tests/unit/test_huawei_legal_no_evidence_roles.py \
  tests/unit/test_annotations.py \
  tests/unit/test_semantic_state.py \
  tests/unit/test_site_observation.py \
  tests/unit/test_runner_registry_observation.py
222 passed in 15.88s

.venv/bin/pytest -q \
  tests/contract/test_official_honor_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_vivo_live.py
208 passed in 13.79s

.venv/bin/ruff check \
  src/quote_app/sites/official_brands/huawei.py \
  tests/contract/test_official_huawei_live.py
All checks passed!

.venv/bin/mypy src/quote_app/sites/official_brands/huawei.py
Success: no issues found in 1 source file
```

## Risks and handoff

- VMALL content remains dynamic. Live acceptance must reconfirm the approved
  public title, current-price and label-anchored option-group structures.
- This task proves the adapter and frozen-site boundaries. Task 3 still owns
  the runner/checkpoint/Excel/report integration acceptance.
- The worktree contains extensive unrelated pre-existing changes and build
  artifacts. Only the precise Task 2 files/hunks are staged and committed.

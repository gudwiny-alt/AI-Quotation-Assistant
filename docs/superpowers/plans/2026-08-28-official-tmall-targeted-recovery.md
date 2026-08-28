# Official and Tmall Targeted Recovery Plan

**Goal:** Recover the five failures observed in run `3a980921-caa1-496a-85aa-346a7aa2195d`, plus the two user-reported official capture framing issues, without changing the accepted JD `.139` behavior.

**Baseline:** Commit `f9d543ad9528797301ab7c2f9b83d4bb5992f0ee`, tag `jd-stable-2026-08-28`.

**Architecture:** Keep every change inside the affected brand/channel adapter. Preserve the shared business-state and Excel contracts. Use fresh DOM resolution after dynamic option clicks and separate semantic revalidation from capture positioning.

## Frozen paths

- JD: all six brands, all behavior.
- Official: HONOR and vivo behavior; OPPO detail behavior; Apple selection and price behavior.
- Tmall: vivo, OPPO, and Xiaomi legal-no behavior.
- Excel output and task scheduling.

## Task 1: Add failing official-site regression tests

- [x] Huawei current VMALL result card opens the exact visible model even when the top search field contains unrelated stale text.
- [x] Xiaomi current R70 result card is accepted without coupling card entry to the instantaneous search-input value.
- [x] Apple capture framing makes the full selected colour heading visible while keeping title, price, colour, and capacity in one viewport.
- [x] OPPO no-model capture applies capture-only scaling and keeps the query plus all actual search results visible.
- [x] Run focused tests and record the expected failures before changing production code.

## Task 2: Implement and freeze official-site recovery

- [x] Add the smallest Huawei and Xiaomi current-card recognition fallbacks.
- [x] Add Apple-only upward capture correction.
- [x] Add OPPO-only no-model capture scale and complete-result positioning.
- [x] Run official contract, integration, and brand-isolation tests.

## Task 3: Add failing Tmall regression tests

- [x] HONOR capacity selection re-resolves the live option after the page rebuilds the SKU DOM.
- [x] Huawei capture recovery keeps the already verified offer and only repositions evidence.
- [x] Apple accepts only bounded aliases of the same approved official flagship store.
- [x] Run focused tests and record the expected failures before changing production code.

## Task 4: Implement and freeze Tmall recovery

- [x] Re-resolve HONOR selected capacity during polling.
- [x] Make Huawei formal capture positioning idempotent and avoid search/detail replay after geometry-only failure.
- [x] Add Apple-only approved seller aliases while keeping host and item identity fail-closed.
- [x] Run Tmall contract, recovery, capture, and marketplace isolation tests.

## Task 5: Verify and package

- [x] Run all official and Tmall focused suites.
- [x] Run JD frozen regression suites and confirm no JD source changes.
- [x] Run the complete pytest suite, Ruff, Mypy, package smoke checks, and signing validation.
- [x] Build the next numbered `.app` without overwriting `.139`.
- [x] Record package path and rollback checkpoint; record the commit after verification.

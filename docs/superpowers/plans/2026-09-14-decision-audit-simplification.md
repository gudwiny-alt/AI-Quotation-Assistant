# Decision and audit simplification implementation plan

> Execute sequentially using the executing-plans workflow; no additional agents required.

**Goal:** Implement the user's approved date selection, margin trial, automatic price comparisons and channel-based audit design.

**Architecture:** Keep collection and Excel writeback unchanged. Extend local decision controls; make deterministic review rules automatic and consolidate per-channel evidence into four checks. Preserve source proofs, exact decimal arithmetic and genuine missing/conflicting evidence states.

**Tech stack:** Python, Tk, Openpyxl, existing offline Vision recognition.

**Spec:** User-approved design in the conversation immediately preceding this task.

## Constraints
- Do not change collectors or the proven K/L/M/P/Q/AO writeback whitelist.
- Margin denominator is settlement price; markup denominator is procurement price.
- History means valid prior months supplied in this batch's base source, with visible scope.
- Compare distribution, warehouse interval upper end, and verified external price separately.
- Only verified source identity and evidence support automatic passing; do not infer success from file existence.

## Tasks
- [x] Add failing tests for exact margin, invalid amounts, calendar day selection, automatic history and independent ceilings.
- [x] Implement date selection and margin display in native decision controls; verify save still writes only approved fields.
- [x] Add failing tests for source identity comparison, four checks per channel, storefront wording, conditional prices near adopted amount and duplicate suppression.
- [x] Implement source proof recovery and consolidated local review checks; retain detailed reasons and audit fingerprints.
- [x] Add channel/group filters to the selected-product audit panel while preserving batch category statistics.
- [x] Verify targeted tests, native interactions/layout, full regression and existing-file replay; inspect screenshots.
- [x] Build an isolated release, verify package/source consistency, commit checkpoint and deliver.

## Verification (2026-09-14)
- Full non-UI regression: 3,101 passed; one local HTTP binding test was blocked by sandbox permissions and passed when rerun with a test-only local port. The final additional subsidy-banner case also passed in the targeted channel suite.
- Native UI: 61 passed, including date selection/apply, exact margin/markup updates, page resizing and retaining channel filters when switching products.
- Targeted rules: exact source identity/hash recovery, conflict rejection, separate price ceilings, missing evidence dependency suppression, subsidy/reference-price ambiguity and official domain boundary tests passed.
- Read-only replay of six actual products and 17 saved images: all six source identity checks pass; missing screenshot is represented once, with the original Huawei JD NO_VALID_SELLING_PRICE reason retained. Existing historical/manual draft values remain intact.
- Screenshot OCR uses full-image plus central-region recognition for large Retina screenshots; original evidence files and collection logic are unchanged. Unclear/conditional price findings remain reviewable with specific reasons.
- Build completed; seven changed runtime modules in the bundled PYZ match source bytecode. Code-signature and startup verification performed before release.
- Protected surfaces: no collector, browser automation or screenshot capture changes; Excel writeback whitelist and backup/identity/version safeguards unchanged.

## Deliberate limits
- This change does not make unreadable images or ambiguous price conditions automatically pass.
- Existing final-report checks for spreadsheet calculation results and final reporting scope remain in force; this iteration does not define new submission policy or bypass those checks.

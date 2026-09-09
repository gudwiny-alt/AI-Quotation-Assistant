# Native component polish — 2026-09-10

Approved scope: existing Mac .170 UI worktree; prebundled manufacturer/channel marks, generic phone artwork (not SKU photography), rounded controls, readable rich rows, and grouped activity actions. No collection/pricing/permission logic changes.

- Artwork uses local bundled PNGs and a window-scoped cache. The generic product image is generated with the built-in image_gen tool; its original prompt is in assets/ui-media. Real screenshot evidence remains separate and is only opened from an actual existing evidence file.
- Brands: Huawei, OPPO, vivo, Honor, Xiaomi, Apple, JD, Tmall, Chrome. Sources and license/provenance are in assets/ui-brands. An unknown official brand uses the existing globe icon.
- The default tall window has an equal-width vertical action rail. Windows under 900 px tall use one aligned toolbar to preserve the content viewport. Existing log growth, text and action states survive the change.
- Rich product/history tables retain stable task IDs, mouse and keyboard selection, filters and read-only navigation. Canvas rows are virtualised; the 1,000-row test bounds the number of drawn objects.
- Quote highlighting compares finite Decimal prices against the existing AH output; it never computes or changes the quote. Equivalent decimal representations match, zero remains valid, and unavailable values such as 无 never receive a minimum-price highlight.

Validation:
- Final code: 1,944 unit tests + 21 native UI tests passed.
- Native coverage: 8 pages/subtabs × 3 window sizes × empty/populated, plus stable row identity, selection, callbacks, 1,000-row painting, asset cache and numeric edge cases.
- Ruff, mypy and git diff whitespace checks passed.
- Independent static reviewer found the decimal highlight issue; repaired and covered by three parameterised cases. No other actionable findings.
- Real packaged UI inspected through CUA; see design-qa.md for scope and remaining P3 differences.

Screenshots in this folder are captures of the real packaged native UI with an isolated fixture. They are prominently labelled 布局测试数据 and are not production execution evidence. Normal app packaging does not include the fixture entrypoint.

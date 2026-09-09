# Native UI component polish QA — 2026-09-10

final result: passed

Scope: the user's approved local asset and native component refinement, not a claim of pixel-identical reproduction of all earlier mockups.

References: user-provided red-box screenshots of product rows, source marks, price cards, state labels and activity actions. Compared against actual CUA captures of the packaged native workbench at its default 1280×1020 client size; images in docs/testing/ui-refresh-evidence/polish-2026-09-10. Test fixtures are visibly labelled and never represented as real collection evidence.

Verified improvements:
- Equal-width vertical activity actions at the tall default size. Compact single toolbar at short sizes; preserved log text, disabled state and callbacks.
- Product artwork, two-line product/specification rows, rounded selected row, status pills and local source marks are visible in the three workbenches. Source labels fit without ellipses at the default size.
- Existing minimum quote is prominent, numeric ties handled correctly, missing prices not presented as winning quotes.
- Intelligence/audit evidence placeholder is visible at the default size; actual files remain the only source of real evidence previews.
- Official Chrome/JD/Tmall marks are clear in settings and data preparation. Six manufacturer marks are bundled and used by model-name lookup; unknown official sources use a globe.
- Removed stale empty context height when navigating to history/settings from data preparation.
- Native geometry/reachability and mouse/keyboard selection tested across supported sizes and empty/populated states. No P0/P1/P2 issues remain for this scope.

Remaining P3 differences from the generated concept: native combo boxes/scrollbars retain their system appearance; long selected detail content scrolls; manufacturer wordmarks have less detail at very small row sizes. The four granular collection steps in the concept have not been fabricated; this version continues to show actual supported task/outcome/evidence states.

Verification: 1,944 unit tests + 21 native UI tests passed on final code; Ruff/mypy clean. A separate static reviewer identified one numeric highlighting issue, repaired and covered with decimal/zero/unavailable cases. PyInstaller QA and production builds completed. Bundle signature and archive checks recorded in the release notes.

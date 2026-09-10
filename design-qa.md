# Native form controls QA — 2026-09-10

final result: passed

Scope: approved polish of month/year selection, brand/status dropdowns, task search, output directory field, and scrollbars across the existing native app. Stable .170 business logic is unchanged.

Evidence: docs/testing/ui-refresh-evidence/controls-2026-09-10, with direct CUA captures of the final packaged QA build. Data is visibly labelled as a fixture. Prior product artwork and channel mark QA remains in docs/testing/ui-refresh-evidence/polish-2026-09-10.

Verified changes:
- One calendar field with draft year and twelve month buttons; cancel preserves values; invalid year does not commit.
- Consistent 42px white controls, 8px rounded outlines, blue focus, native text editing and separate placeholders.
- White dropdowns with pale blue selection, library chevrons, keyboard selection and Escape/outside dismissal; repeated clicks close correctly and cleanup removes popup bindings/traces.
- Arrowless slim vertical/horizontal scrollbars across page containers, tables and logs; no painted thumb when all content fits.
- Search changes, including pasted text, filter the existing read-only history data.
- Actual geometry and content reachability verified in empty/populated pages and supported window sizes. The current display clamps a requested tall window to 946px; log expansion follows actual available height.

Verification: 1,944 unit tests + 29 native UI tests passed; Ruff/mypy clean. Static review identified a selector focus/toggle race, fixed and covered. Escape focus return was also reproduced and fixed on macOS. Production and independent QA bundles built successfully. Release signature/archive checks are recorded alongside the package.

No P0/P1/P2 findings remain for this scope. P3 differences: menus retain a plain rectangular popup surface; native text editors remain inside styled shells. The new visuals do not imply additional data collection functionality.

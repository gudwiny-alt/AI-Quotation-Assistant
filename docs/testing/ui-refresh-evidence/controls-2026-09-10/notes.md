# Native form controls — 2026-09-10

The two JPEGs are direct CUA captures of the final packaged native QA application. They show synthetic layout data, visibly labelled; no business run was started and no website was contacted.

- data-preparation.jpg: combined month field, white brand selector and arrowless slim scrollbar.
- history.jpg: search icon/placeholder and matching status selector; no test history was persisted.

Keyboard opening of the month grid was visually inspected during iteration. Final native interaction tests cover month validation/apply/cancel, keyboard selection, Escape focus restoration, click-to-toggle, popup lifecycle, IME-compatible native Entry and drag limits. CUA coordinate actions did not reliably activate Tk controls, so no claim is made that CUA click/scroll actions verified those interactions; native event tests provide that evidence.

Current screen constrained a requested 1020-pixel window to 946 pixels. The log still expands with available height. Geometry/reachability tests cover requested 1000×720, 1280×850 and 1280×1020 sizes, with empty and populated states, respecting the native window manager's actual bounds.

1,944 unit tests and 29 native UI tests passed. Ruff and mypy clean. Review found a selector focus/toggle race, reproduced with a failing native test and fixed. A separate native test reproduced and verified the macOS Escape focus restoration fix.

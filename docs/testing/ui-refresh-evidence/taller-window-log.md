# Taller startup window and activity log

User requested a larger recent-activity area using the excess vertical whitespace, and a startup height matching their 2550×2088 screenshot (roughly 1280×1020 client points).

Implemented in f9363b2:
- Default client size 1280×1020, capped by screen height with 100px allowance and existing 720px minimum.
- Log grows from the compact two-line layout at 850px to seven requested text lines at 1020px, capped at twelve in larger windows.
- Root Configure binding ignores child events and only changes the text height when required. Log contents and continue/cancel state survive resizing and navigation.

Validation: 81 focused tests passed in the initial adjustment; after final sizing refinement, all 14 native UI tests passed, including 48 view/size/data-state geometry combinations. Ruff and mypy passed. Real CUA capture `taller-window-log.jpg` confirms the report cards, lower hint and expanded activity area are all visible at startup size. The screenshot uses explicitly marked isolated fixture data. Original .170 business logic was not changed.

# Task 1 — native workbench fidelity

Implemented the approved native view revision on the existing .170 controller. No business service, browser control, data writer, rule engine, or packaging code was changed by this task.

## Delivered views

- Six sidebar destinations with licensed local icon loading, a blue selected state using the cross-platform `clam` ttk theme, Chinese-capable system typography, and native rounded card backgrounds.
- Overview tabs: task dashboard, local Excel preparation, and report results. Preparation follows the reference source composition: online channels, the marketing system (local marketing workbook), and Fujian Mobile BOSS (local BOP resource workbook), with the required base workbook in its own compact bar. File selection is explicitly labeled as selection, not validated import. The dashboard shows real received-channel and saved-screenshot counts, per-channel bars, pending-login/evidence/error reminders, and direct links to the three agents. Both output cards require an actual file before becoming clickable.
- Three agent views with stage navigation, real result tables, structured selected-record fields, channel-price comparison, real evidence previews, source text, and existing open-file actions.
- Read-only task history with task-id/status filtering and selected-batch information. System settings groups the actual readiness/login callbacks and local storage paths.
- Native page shortcuts: Command 1–6; overview tabs: Command Shift 1–3 (Control equivalents outside macOS). Scroll areas respond to wheel/trackpad input.

## State and honesty

All metrics derive from `DesktopState`; no demo numbers are seeded by production code. The dashboard explains that its channel bars compare saved screenshots against received records, because an overall planned-task total is not available in this view model. Saved prices and completed evidence remain separate. Internal business inputs remain local Excel imports. No automatic BOSS access or human adjudication workflow is claimed.

The controller's `continue_button`, `cancel_button`, `open_button`, and `status` widgets survive page navigation. Existing pipeline callbacks are preserved. `begin_run` snapshots the run month, scope, and date so editing the next run's inputs cannot relabel an existing report. Data preparation labels the next task settings; history/settings do not display a misleading run period.

## Verification

Focused tests include new native Tk regressions for navigation/input retention, output-file gating and reset, honest observation/evidence counts, read-only history filtering, keyboard navigation, run-month snapshotting, and full-size native card underlays despite Tk frame padding. The native layout test traverses all eight views at 1280×850 and 1000×720 with both empty state and isolated populated fixtures: 32 screen-state combinations. It checks native widget bounds/text sizing and scroll reachability, and requires all three agent cards to fit the overview at the default window size.

Red tests were observed before implementing tabs, output gating, channel dashboard projections, history filtering, keyboard navigation/default overview fit, and run-month snapshotting. The existing mocked shell test was updated only to substitute the new decorative card widget alongside its existing Tk widget doubles.

Native visual capture and independent packaging are owned by Task 2. UI geometry checks validate reachability but do not substitute for Task 2's actual screenshots. The rounded card canvas is an intentional decorative underlay; sibling-overlap diagnostics should ignore canvases marked `_decorative_card`.

Final checks (core-excel venv): 78 focused tests passed; Ruff passed; mypy reported no issues. Native geometry checks cover all 32 combinations described above.

Commands:

- `QUOTE_NATIVE_UI_TESTS=1 python -m pytest tests/ui/test_desktop_workbench.py tests/unit/test_app.py tests/unit/test_desktop_state.py -q`
- `python -m ruff check src/quote_app/desktop_ui.py tests/ui/test_desktop_workbench.py tests/unit/test_app.py`
- `python -m mypy src/quote_app/desktop_ui.py --follow-imports=silent`

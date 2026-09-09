# Mac native UI refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the approved blue-white desktop workbench using real .170 data and actions.
**Architecture:** Keep quote_app.app.BetaApp as the controller. Add a focused desktop UI module and a testable presentation model; forward real existing events/results without rewriting business services.
**Tech Stack:** Python 3.12, tkinter/ttk, existing Pillow/SQLite models; no new UI dependencies.
**Spec:** docs/superpowers/specs/2026-09-09-native-ui-refresh.md

## Global Constraints

- Base 94aaecb; branch codex/mac-170-ui-refresh only.
- No production mock data, auto BOSS claims, automatic margin recommendations or approval actions.
- Preserve event queues, login/cancel/continue, capture focus and all .170 business behavior.
- Tk updates only on the main thread; no task process launch from view navigation.
- Keep old App artifacts untouched.

## Task 1: Coherent native workbench and real data binding

**Files:** modify src/quote_app/app.py; create src/quote_app/desktop_ui.py and src/quote_app/desktop_state.py as useful; add tests/unit/test_desktop_state.py and focused tests/unit/test_app.py coverage.
**Consumes:** BetaApp vars/actions, WorkerEvent, FullPipelineResult, AppPaths, existing repository read APIs.
**Produces:** a UI view invoked from BetaApp._build and state updates forwarded from its existing event/result methods. Keep public request/formatter APIs intact.

- [ ] Inspect existing app tests, repository read methods and approved images before choosing exact view bindings.
- [ ] Run baseline: ../core-excel/.venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_app.py.
- [ ] Add regression tests proving that observation with price does not count as completed evidence, a new run resets prior task display, and missing prices remain blank. Example invariants: `assert row.evidence_state != 'complete'` after an observation, and `assert model.rows == []` after a reset; use final actual API names in tests.
- [ ] Implement a single view/controller boundary. Controller forwards existing events on the Tk queue drain; no string parsing of human logs to manufacture task truth.
- [ ] Build sidebar and live pages matching reference structure, with accessible true actions and read-only details. Compact source cards retain file selection; full path remains available but does not dominate.
- [ ] Build real empty states and a persistent readable execution log. Make task history reads read-only and lazy; catch invalid/missing database and show an honest empty/error state.
- [ ] Run focused tests and ruff on changed code. Record exact commands and results.
- [ ] Self-review and commit only scoped changes; report changed files and any unavailable data fields.

## Task 2: Independent review and native/package validation

**Files:** inspect diff, any fixes only in UI integration; independent build destination; docs/testing/2026-09-09-native-ui-refresh.md.

- [ ] Review spec compliance and code quality with independent reviewer, fix confirmed findings.
- [ ] Run full pytest once after focused checks pass; run ruff and relevant mypy.
- [ ] Launch UI against isolated temporary paths without starting quotation. Inspect actual screenshot and navigation; repair clipping or disabled action mistakes.
- [ ] Build existing PyInstaller spec into independent dist-mac-ui-refresh/work directory, verify Mac signature when applicable.
- [ ] Document baseline, changes, evidence and live-site verification limits; leave stable baseline untouched.

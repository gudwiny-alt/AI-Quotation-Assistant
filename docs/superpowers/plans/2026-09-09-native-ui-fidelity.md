# Native UI fidelity revision

## Approved specification
Continue native Tk App on .170 logic. Match approved 1536x1024 blue-white screenshots in 申报材料/界面配图_2026-09-09, especially original three agent views and 02 overview. Sidebar stays: 协同总览 / 价格情报智能体 / 报价决策智能体 / 稽核审查智能体 / 任务记录 / 系统设置. Overview gets 任务总览 / 数据准备 / 报表结果 tabs. All counts, results and actions use actual state; internal data remain local imports. Human conflict adjudication is planned, not part of implementation. No browser autofocus, new business rules, fabricated production data, pipeline rewrite, or stable build overwrite.

## Task 1 — native view implementation
Refactor desktop_ui.py into coherent visual components. Airy typography, pale sidebar, blue active navigation, polished cards, icons provided by controller, clear headers/actions. Implement overview dashboard, data cards, report cards and three tabs. Improve agent tables and structured result/details, history and settings. Preserve controller references and callbacks, model state across navigation, accurate empty/busy/success states. Use existing desktop_state helpers; add focused state tests if behavior is added. Run focused tests and lint/type checks. Implementer owns desktop_ui.py and necessary UI tests, not asset files or business services.

## Task 2 — assets and native QA
Controller provides licensed icon assets and visual reference measurements, resolves native capture, tests actual windows at 1280x850 and 1000x720. Compare against approved mockups with real empty state or isolated test data. Save honest QA report. Review implementation, fix meaningful defects. Package separate preview only after checks; do not merge stable branch.

## Interfaces / preflight
Task 1 consumes Task 2 icon asset folder through a lightweight native loader; packaging must include it. Work concurrently on separate files. Existing app callbacks are binding API. No task mandates simulated production behavior. Native scope overrides website scaffolding from image-to-code skill.

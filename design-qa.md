# Native UI design QA

Source visual truth: `/Users/yangguowei/Documents/资金物流平台铺货报价智能体/申报材料/界面配图_2026-09-09/02_协同总览.png`, `01_启动与数据准备.png`, `06_报价明细.png`, `07_异常清单.png`, `10_报表生成与数据导出.png`, plus original three agent references in `既有参考图/`.

Implementation evidence: `docs/testing/ui-refresh-evidence/fidelity-*-final.jpg`. Raw screenshots come from the actual Tk application via CUA; fixture views are visibly marked as test data. `fidelity-history-empty-final.jpg` in fact contains existing local history and is not an empty database.

Viewport: Tk client 1280×850; native capture including chrome 1120×768 after capture-provider downscaling. Source images are 1536×1024. Comparison used matching page/state and proportional app-content regions, excluding different native titlebars. Source and implementation were opened together in the same visual comparison input; this is a proportional comparison, not a pixel-difference score. Geometry additionally checked 1000×720. No CSS viewport or browser deviceScaleFactor applies to this native app.

## Comparison history

1. P1: macOS rendered tk.Button side navigation with gray bezels and unreadable active text. Fixed with dedicated ttk clam navigation styles. Verified in real subsequent screenshots.
2. P2: sidebar title clipped after adding icons; overview agent entries fell below the default viewport. Corrected font, padding and fixed-region height; all three cards now visible at 1280×850. Native geometry checks cover all eight views with empty and populated state.
3. P2: rounded card canvas started inside frame padding and cropped its outline. Fixed `bordermode='ignore'` and canvas-based Configure sizing. Clean rebuilt archive inspected for corrected code, then actual native view recaptured as `fidelity-overview-fixture-final.jpg`.
4. P2: data preparation showed three raw file types instead of the approved source composition. Changed to online price channels / terminal marketing system / Fujian Mobile BOSS source cards; base quotation workbook has its own row. Internal sources explicitly say local import. Verified `fidelity-data-empty-final.jpg`.
5. P2: existing result header adopted the next-run editable month. Snapshot the run context; data page distinguishes next-run configuration when results exist. Failing-first native regression and review passed.
6. P2: decision exception metric had a success icon, while valid prices had a waiting icon. Changed exception to orange alert, valid prices to green success. Verified after clean rebuild in `fidelity-decision-fixture-final.jpg`; exception and valid-price colors now match their meaning.

## Required fidelity surfaces

- Typography: system Chinese font, dark navy titles, clear title/body/helper hierarchy. Chinese source labels and populated long model names fit tested widths. Detailed value rows and price tables visually inspected.
- Spacing/layout: 236px sidebar at 1280px (18.4%, same proportion as reference); three metrics; split list/detail; overview channel/reminder region; three workbench entry cards; common log footer. Long preparation/settings/detail content scrolls instead of clipping.
- Color: pale blue sidebar/background, white surfaces, blue selection, teal success, amber pending/error attention. Final decision exception/success semantics were verified in the post-fix native capture.
- Images/icons: actual licensed Tabler assets in local PNG variants, with original vectors and license. Live evidence uses saved screenshot files only. No fabricated product image or evidence is inserted into production.
- Copy/content: real state only. Current received rows are not falsely labelled total planned tasks. Internal systems remain local Excel import. The planned constraint-conflict/human-adjudication mockup is not exposed as an implemented workflow.

## Expected differences and P3 polish

Reference images contain sample data and some planned capabilities. Native implementation uses authoritative available metrics, so overview uses three state tiles rather than four invented counters. Toolkit buttons/scrollbars are flatter than generated art, and icons are close library alternatives rather than exact generated brand drawings. These are documented native component/content differences. Small windows intentionally scroll long data/settings/detail sections. A true end-to-end business run was outside this UI-only verification.

7. P2: the minimum valid price lacked the reference’s visual emphasis. Channel values increased to 17pt and the existing AH value to 24pt blue bold. Verified in the final native decision screenshot; it remains visible in the default viewport. No calculation logic was added.

Focused comparison: original `既有参考图/报价决策智能体.png` and final populated decision capture were opened together. Price cards, minimum-price emphasis, model/specification text, table headers and exception/success colors were readable at full capture size; a separate raster crop was unnecessary. The separate `06_报价明细.png` is a detail composition reference, not claimed as an identical standalone page. Data preparation uses the same source-card composition, compared in actual unselected-file state rather than the reference’s sample imported state.

final result: passed

All actionable P0/P1/P2 findings in this native presentation revision are resolved. Expected native control/content differences and the unperformed live business run are listed above.

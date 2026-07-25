# Core Sample Acceptance — 2026-07-25

## Scope

The completed local core pipeline was run for quotation month August 2026
against the three supplied sample workbooks:

- `1.基础表.xlsx`
- `2.营销商品信息查询.xlsx`
- `3.BOP资源信息表.xlsx`

The clean template was `resources/templates/quote_template.xlsx`. Outputs were
written to an isolated temporary directory; the supplied files were opened
read-only and were not copied over or saved.

## Result

Status: **Passed**

- Nonblank Base A records: 7
- Generated quotation rows: 7
- Execution-report detail rows: 7
- Source order: exact match
- Quotation worksheets: only `5G手机`
- Summary: 0 completed, 6 partial, 0 failed, 1 unsupported, 7 requiring
  manual website supplementation
- Product managers: 杨旭佳、林旭明、许自骋、罗慧玲、杨春、庄映、陆少美
- Manual columns K, L, M, N, P, and Q: blank for all generated rows
- Rolling headers: March 2026, July 2026, and August 2026
- X2 formula:
  `=IF(K2="","",IF(J2="无","无",(K2-J2)/J2))`
- Quote rows, report detail rows, and report summary totals reconcile
- SHA-256 digests of all three inputs and the clean template were identical
  before and after the run

## Workbook verification

Both generated workbooks were reopened successfully. The spreadsheet runtime
inspected values and formulas and rendered every output worksheet:

- quotation: `5G手机`
- report: `运行总览`
- report: `处理明细`

No `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, or `#N/A` cells were found.
Visual inspection confirmed that the report contains the actual product-manager
names from quotation column AG; the long special-situation notes from AB do not
enter the manager detail or manager summary. Material-code cells are stored as
text, including support for leading zeroes.


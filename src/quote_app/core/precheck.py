from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from quote_app.core.months import required_price_months
from quote_app.core.normalization import normalize_code, normalize_text
from quote_app.domain.models import InputPaths, Issue, QuoteMonth
from quote_app.excel.source_reader import SheetTable, WorkbookReadError, read_first_table

MINIMUM_COLUMNS = {
    "base": 1,
    "marketing": 9,
    "bop": 11,
}

CODE_HEADERS = {
    "base": ("集团一级库物料编码", 0),
    "marketing": ("物料编码", 8),
    "bop": ("集团一级库编码", 10),
}

SOURCE_LABELS = {
    "base": "基础表",
    "marketing": "营销商品信息查询表",
    "bop": "BOP资源信息表",
}


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    fatal_issues: tuple[Issue, ...]
    row_issues: tuple[Issue, ...]
    identified_sheets: Mapping[str, SheetTable]


def month_header(month: QuoteMonth) -> str:
    return f"{month.year}年{month.month}月结算报价（元/台）"


def precheck_inputs(paths: InputPaths, quote_month: QuoteMonth) -> PrecheckResult:
    fatal_issues: list[Issue] = []
    row_issues: list[Issue] = []
    identified_sheets: dict[str, SheetTable] = {}

    for source, path in _source_paths(paths).items():
        try:
            table = read_first_table(path)
        except WorkbookReadError:
            fatal_issues.append(
                Issue(
                    code="UNREADABLE_WORKBOOK",
                    message=f"{SOURCE_LABELS[source]}文件无法读取：{path.name}",
                    fatal=True,
                )
            )
            continue

        if not _is_expected_table(source, table):
            expected_header, expected_index = CODE_HEADERS[source]
            fatal_issues.append(
                Issue(
                    code="INVALID_SOURCE_TABLE",
                    message=(
                        f"{source} workbook must have at least {MINIMUM_COLUMNS[source]} "
                        f"columns and {expected_header!r} in column "
                        f"{expected_index + 1}: {path.name}"
                    ),
                    fatal=True,
                )
            )
            continue

        if source == "base":
            for required_month in required_price_months(quote_month):
                expected = month_header(required_month)
                if expected not in table.headers:
                    fatal_issues.append(
                        Issue(
                            code="MISSING_HISTORY_MONTH",
                            message=f"base workbook is missing history column {expected!r}: {path.name}",
                            fatal=True,
                        )
                    )

        identified_sheets[source] = table
        row_issues.extend(_code_row_issues(source, table))

    return PrecheckResult(
        fatal_issues=tuple(fatal_issues),
        row_issues=tuple(row_issues),
        identified_sheets=identified_sheets,
    )


def _source_paths(paths: InputPaths) -> dict[str, Path]:
    return {
        "base": paths.base,
        "marketing": paths.marketing,
        "bop": paths.bop,
    }


def _is_expected_table(source: str, table: SheetTable) -> bool:
    expected_header, expected_index = CODE_HEADERS[source]
    return (
        table.column_count >= MINIMUM_COLUMNS[source]
        and normalize_text(table.header_values[expected_index]) == expected_header
    )


def _code_row_issues(source: str, table: SheetTable) -> list[Issue]:
    _, code_index = CODE_HEADERS[source]
    issues: list[Issue] = []
    code_rows: defaultdict[str, list[int]] = defaultdict(list)

    for row_number, row in enumerate(table.rows, start=2):
        value = row[code_index] if code_index < len(row) else None
        code = normalize_code(value)
        if not code:
            issues.append(
                Issue(
                    code="BLANK_MATERIAL_CODE",
                    message=f"{source} row {row_number} has a blank material code",
                    fatal=False,
                    row_number=row_number,
                )
            )
            continue
        code_rows[code].append(row_number)

    for code, row_numbers in code_rows.items():
        if len(row_numbers) < 2:
            continue
        issues.extend(
            Issue(
                code="DUPLICATE_MATERIAL_CODE",
                message=f"{source} row {row_number} repeats material code {code}",
                fatal=False,
                row_number=row_number,
            )
            for row_number in row_numbers
        )

    return issues

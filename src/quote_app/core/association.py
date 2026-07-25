from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

from quote_app.core.months import required_price_months
from quote_app.core.normalization import normalize_brand, normalize_code, normalize_text
from quote_app.core.precheck import month_header
from quote_app.domain.models import Issue, QuoteMonth, QuoteRow, WebQuery
from quote_app.excel.source_reader import SheetTable

FIXED_COLUMN_MAP = {
    "A": ("marketing", "M"),
    "B": ("marketing", "C"),
    "C": ("base", "A"),
    "D": ("marketing", "B"),
    "E": ("marketing", "E"),
    "F": ("bop", "Z"),
    "G": ("bop", "Y"),
    "H": ("marketing", "V"),
    "O": ("marketing", "X"),
    "AB": ("base", "I"),
    "AC": ("base", "J"),
    "AD": ("base", "K"),
    "AE": ("base", "L"),
    "AF": ("base", "M"),
    "AG": ("base", "B"),
}

WEB_QUERY_MAP = {
    "brand": ("marketing", "C"),
    "model_name": ("marketing", "E"),
    "ram": ("marketing", "AQ"),
    "storage": ("marketing", "AR"),
    "color": ("marketing", "AS"),
}

_MARKETING_FIELDS = tuple(
    dict.fromkeys(
        source_column
        for source, source_column in (*FIXED_COLUMN_MAP.values(), *WEB_QUERY_MAP.values())
        if source == "marketing"
    )
)
_BOP_FIELDS = tuple(
    source_column for source, source_column in FIXED_COLUMN_MAP.values() if source == "bop"
)


def build_index(
    records: Iterable[dict[str, Any]],
    key: str,
) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        normalized = normalize_code(record.get(key))
        if normalized:
            index[normalized].append(record)
    return dict(index)


def associate_records(
    base: Iterable[dict[str, Any]],
    marketing: Iterable[dict[str, Any]],
    bop: Iterable[dict[str, Any]],
    quote_month: QuoteMonth,
) -> list[QuoteRow]:
    base_records = tuple(base)
    marketing_index = build_index(marketing, "I")
    bop_index = build_index(bop, "K")
    history_start, previous_month = required_price_months(quote_month)
    history_start_header = month_header(history_start)
    previous_month_header = month_header(previous_month)
    rows: list[QuoteRow] = []

    for source_row_number, base_record in enumerate(base_records, start=2):
        material_code = normalize_code(base_record.get("A"))
        row = QuoteRow(
            source_row_number=source_row_number,
            material_code=material_code,
        )
        _copy_fixed_cells(row.cells, "base", base_record)
        row.cells["I"] = base_record.get(history_start_header)
        row.cells["J"] = base_record.get(previous_month_header)

        marketing_candidates = marketing_index.get(material_code, [])
        if not marketing_candidates:
            row.issues.append(
                _row_issue(
                    "MARKETING_NOT_FOUND",
                    f"base row {source_row_number} has no marketing match for {material_code}",
                    source_row_number,
                )
            )
        else:
            selected_marketing = _select_equivalent(marketing_candidates, _MARKETING_FIELDS)
            if selected_marketing is None:
                row.issues.append(
                    _row_issue(
                        "MARKETING_CONFLICT",
                        f"base row {source_row_number} has conflicting marketing matches "
                        f"for {material_code}",
                        source_row_number,
                    )
                )
            else:
                _copy_fixed_cells(row.cells, "marketing", selected_marketing)
                row.cells["B"] = normalize_brand(row.cells["B"])
                row.web_query = _build_web_query(selected_marketing)
                missing_web_fields = [
                    name
                    for name, (_, source_column) in WEB_QUERY_MAP.items()
                    if not normalize_text(selected_marketing.get(source_column))
                ]
                if missing_web_fields:
                    row.issues.append(
                        _row_issue(
                            "WEB_FIELDS_MISSING",
                            f"base row {source_row_number} is missing web query fields: "
                            f"{', '.join(missing_web_fields)}",
                            source_row_number,
                        )
                    )

        bop_candidates = bop_index.get(material_code, [])
        if not bop_candidates:
            row.cells["F"] = "申请配置"
            row.cells["G"] = "申请配置"
        else:
            selected_bop = _select_equivalent(bop_candidates, _BOP_FIELDS)
            if selected_bop is None:
                row.issues.append(
                    _row_issue(
                        "BOP_CONFLICT",
                        f"base row {source_row_number} has conflicting BOP matches "
                        f"for {material_code}",
                        source_row_number,
                    )
                )
            else:
                _copy_fixed_cells(row.cells, "bop", selected_bop)

        rows.append(row)

    return rows


def associate_rows(
    base: SheetTable,
    marketing: SheetTable,
    bop: SheetTable,
    quote_month: QuoteMonth,
) -> list[QuoteRow]:
    base_records = [dict(record) for record in base.records_by_column_letter()]
    for required_month in required_price_months(quote_month):
        header = month_header(required_month)
        column_index = base.headers.get(header)
        if column_index is None:
            continue
        column_letter = get_column_letter(column_index + 1)
        for record in base_records:
            record[header] = record.get(column_letter)

    return associate_records(
        base_records,
        [dict(record) for record in marketing.records_by_column_letter()],
        [dict(record) for record in bop.records_by_column_letter()],
        quote_month,
    )


def _copy_fixed_cells(
    cells: dict[str, Any],
    source_name: str,
    record: Mapping[str, Any],
) -> None:
    for output_column, (source, source_column) in FIXED_COLUMN_MAP.items():
        if source == source_name:
            cells[output_column] = record.get(source_column)


def _select_equivalent(
    candidates: Sequence[dict[str, Any]],
    relevant_fields: Sequence[str],
) -> dict[str, Any] | None:
    first = candidates[0]
    expected = tuple(first.get(field) for field in relevant_fields)
    if all(
        tuple(candidate.get(field) for field in relevant_fields) == expected
        for candidate in candidates
    ):
        return first
    return None


def _row_issue(code: str, message: str, row_number: int) -> Issue:
    return Issue(
        code=code,
        message=message,
        fatal=False,
        row_number=row_number,
    )


def _build_web_query(record: Mapping[str, Any]) -> WebQuery:
    brand_value = normalize_text(record.get(WEB_QUERY_MAP["brand"][1]))
    return WebQuery(
        brand=normalize_brand(brand_value) if brand_value else None,
        model_name=_optional_text(record.get(WEB_QUERY_MAP["model_name"][1])),
        ram=_optional_text(record.get(WEB_QUERY_MAP["ram"][1])),
        storage=_optional_text(record.get(WEB_QUERY_MAP["storage"][1])),
        color=_optional_text(record.get(WEB_QUERY_MAP["color"][1])),
    )


def _optional_text(value: Any) -> str | None:
    normalized = normalize_text(value)
    return normalized or None

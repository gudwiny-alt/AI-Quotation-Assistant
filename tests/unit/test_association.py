from pathlib import Path
from typing import Any

from quote_app.core.association import associate_records, associate_rows
from quote_app.domain.models import QuoteMonth, WebQuery
from quote_app.excel.source_reader import SheetTable


def test_base_rows_drive_count_order_and_all_exact_source_mappings() -> None:
    base: list[dict[str, Any]] = [
        {
            "A": " 9102 ",
            "B": "基地二",
            "I": "经理二",
            "J": "团队二",
            "K": "品类二",
            "L": "部门二",
            "M": "备注二",
            "2026年3月结算报价（元/台）": "无",
            "2026年7月结算报价（元/台）": 4299,
        },
        {
            "A": "9101",
            "B": "基地一",
            "I": "经理一",
            "J": "团队一",
            "K": "品类一",
            "L": "部门一",
            "M": "备注一",
            "2026年3月结算报价（元/台）": 3999,
            "2026年7月结算报价（元/台）": 3899,
        },
    ]
    marketing = [
        {
            "I": "9101",
            "M": "营销A1",
            "C": "荣耀",
            "B": "营销D1",
            "E": "HONOR 500",
            "V": "营销H1",
            "X": "营销O1",
            "AQ": "12GB",
            "AR": "256GB",
            "AS": "月光银",
        },
        {
            "I": "9102",
            "M": "营销A2",
            "C": "小米",
            "B": "营销D2",
            "E": "小米17",
            "V": "营销H2",
            "X": "营销O2",
            "AQ": "12GB",
            "AR": "256GB",
            "AS": "雪山粉",
        },
    ]
    bop = [{"K": 9101.0, "Y": "BOP-G1", "Z": "BOP-F1"}]

    rows = associate_records(base, marketing, bop, QuoteMonth(2026, 8))

    assert [row.material_code for row in rows] == ["9102", "9101"]
    assert [row.source_row_number for row in rows] == [2, 3]
    assert rows[0].cells == {
        "A": "营销A2",
        "B": "小米",
        "C": " 9102 ",
        "D": "营销D2",
        "E": "小米17",
        "F": "申请配置",
        "G": "申请配置",
        "H": "营销H2",
        "I": "无",
        "J": 4299,
        "O": "营销O2",
        "AB": "经理二",
        "AC": "团队二",
        "AD": "品类二",
        "AE": "部门二",
        "AF": "备注二",
        "AG": "基地二",
    }
    assert rows[0].web_query == WebQuery(
        brand="小米",
        model_name="小米17",
        ram="12GB",
        storage="256GB",
        color="雪山粉",
    )
    assert rows[1].web_query == WebQuery(
        brand="HONOR",
        model_name="HONOR 500",
        ram="12GB",
        storage="256GB",
        color="月光银",
    )
    assert rows[1].cells["B"] == "HONOR"
    assert rows[1].cells["F"] == "BOP-F1"
    assert rows[1].cells["G"] == "BOP-G1"
    assert rows[1].issues == []


def test_duplicate_base_rows_are_retained_independently() -> None:
    base: list[dict[str, Any]] = [
        {
            "A": "9101",
            "2026年3月结算报价（元/台）": 3999,
            "2026年7月结算报价（元/台）": 3899,
        },
        {
            "A": 9101.0,
            "2026年3月结算报价（元/台）": 3799,
            "2026年7月结算报价（元/台）": 3699,
        },
    ]
    marketing = [_complete_marketing("9101")]

    rows = associate_records(base, marketing, [], QuoteMonth(2026, 8))

    assert len(rows) == 2
    assert [row.material_code for row in rows] == ["9101", "9101"]
    assert [row.cells["I"] for row in rows] == [3999, 3799]
    assert [row.cells["J"] for row in rows] == [3899, 3699]


def test_missing_marketing_is_a_row_issue_and_missing_bop_is_normal_fallback() -> None:
    base = [
        {
            "A": "9109",
            "B": "仍可保留",
            "2026年3月结算报价（元/台）": 3999,
            "2026年7月结算报价（元/台）": 3899,
        }
    ]

    [row] = associate_records(base, [], [], QuoteMonth(2026, 8))

    assert row.cells["C"] == "9109"
    assert row.cells["AG"] == "仍可保留"
    assert row.cells["F"] == "申请配置"
    assert row.cells["G"] == "申请配置"
    assert _issue_codes(row) == {"MARKETING_NOT_FOUND"}
    assert all(not issue.fatal and issue.row_number == 2 for issue in row.issues)


def test_conflicting_candidates_are_not_selected_but_other_sources_are_retained() -> None:
    base = [
        {
            "A": "9101",
            "B": "基础字段",
            "2026年3月结算报价（元/台）": 3999,
            "2026年7月结算报价（元/台）": 3899,
        }
    ]
    marketing = [
        _complete_marketing("9101", color="黑色"),
        _complete_marketing(" 9101 ", color="白色"),
    ]
    bop: list[dict[str, Any]] = [
        {"K": "9101", "Y": "配置一", "Z": "资源一"},
        {"K": 9101.0, "Y": "配置二", "Z": "资源一"},
    ]

    [row] = associate_records(base, marketing, bop, QuoteMonth(2026, 8))

    assert _issue_codes(row) == {"MARKETING_CONFLICT", "BOP_CONFLICT"}
    assert row.cells["C"] == "9101"
    assert row.cells["AG"] == "基础字段"
    assert "B" not in row.cells
    assert "E" not in row.cells
    assert "F" not in row.cells
    assert "G" not in row.cells


def test_equivalent_candidates_are_safe_and_missing_web_fields_are_reported() -> None:
    base = [
        {
            "A": "9101",
            "2026年3月结算报价（元/台）": 3999,
            "2026年7月结算报价（元/台）": 3899,
        }
    ]
    incomplete = _complete_marketing("9101", brand=" vivo ", color="　")
    marketing = [incomplete, dict(incomplete, I=9101.0)]
    bop = [
        {"K": "9101", "Y": "已配置", "Z": "资源"},
        {"K": " 9101 ", "Y": "已配置", "Z": "资源"},
    ]

    [row] = associate_records(base, marketing, bop, QuoteMonth(2026, 8))

    assert row.cells["B"] == "维沃"
    assert row.cells["F"] == "资源"
    assert row.cells["G"] == "已配置"
    assert _issue_codes(row) == {"WEB_FIELDS_MISSING"}
    assert row.web_query == WebQuery(
        brand="维沃",
        model_name="型号",
        ram="12GB",
        storage="256GB",
        color=None,
    )


def test_sheet_tables_resolve_dynamic_month_headers_at_arbitrary_columns() -> None:
    base = _sheet_table(
        headers=(
            "集团一级库物料编码",
            "固定B",
            "无关列",
            "2025年8月结算报价（元/台）",
            "固定E",
            "固定F",
            "2025年12月结算报价（元/台）",
        ),
        rows=(("9101", "基础B", "忽略", 4099, None, None, 3999),),
    )
    marketing = _sheet_table(
        headers=tuple(f"字段{index}" for index in range(1, 46)),
        rows=(
            _row_with_columns(
                45,
                B="营销D",
                C="apple",
                E="iPhone",
                I="9101",
                M="营销A",
                V="营销H",
                X="营销O",
                AQ="8GB",
                AR="128GB",
                AS="黑色",
            ),
        ),
    )
    bop = _sheet_table(
        headers=tuple(f"字段{index}" for index in range(1, 27)),
        rows=(_row_with_columns(26, K="9101", Y="已配置", Z="资源"),),
    )

    [row] = associate_rows(base, marketing, bop, QuoteMonth(2026, 1))

    assert row.cells["I"] == 4099
    assert row.cells["J"] == 3999
    assert row.cells["B"] == "苹果"
    assert row.cells["F"] == "资源"
    assert row.cells["G"] == "已配置"


def _complete_marketing(
    code: Any,
    *,
    brand: str = "小米",
    color: str = "黑色",
) -> dict[str, Any]:
    return {
        "I": code,
        "M": "营销A",
        "C": brand,
        "B": "营销D",
        "E": "型号",
        "V": "营销H",
        "X": "营销O",
        "AQ": "12GB",
        "AR": "256GB",
        "AS": color,
    }


def _issue_codes(row: Any) -> set[str]:
    return {issue.code for issue in row.issues}


def _sheet_table(
    *,
    headers: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
) -> SheetTable:
    return SheetTable(
        path=Path("source.xlsx"),
        sheet_name="Sheet1",
        headers={value: index for index, value in enumerate(headers)},
        header_values=headers,
        column_count=len(headers),
        rows=rows,
    )


def _row_with_columns(length: int, **values: object) -> tuple[object, ...]:
    from openpyxl.utils import column_index_from_string  # type: ignore[import-untyped]

    row: list[object] = [None] * length
    for column, value in values.items():
        row[column_index_from_string(column) - 1] = value
    return tuple(row)

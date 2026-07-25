from pathlib import Path

import pytest
import msoffcrypto  # type: ignore[import-untyped]

from quote_app.core.precheck import month_header, precheck_inputs
from quote_app.domain.models import InputPaths, QuoteMonth
from quote_app.excel.source_reader import read_first_table
from tests.conftest import ValidInputs
from tests.factories.workbook_factory import (
    encrypt_workbook,
    save_empty_workbook,
    save_workbook,
)


def _paths(
    tmp_path: Path,
    *,
    base: Path | None = None,
    marketing: Path | None = None,
    bop: Path | None = None,
) -> InputPaths:
    valid_base = base or save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899]],
    )
    valid_marketing = marketing or save_workbook(
        tmp_path / "marketing.xlsx",
        [*[f"营销字段{index}" for index in range(1, 9)], "物料编码"],
        [[""] * 8 + ["9101"]],
    )
    valid_bop = bop or save_workbook(
        tmp_path / "bop.xlsx",
        [*[f"BOP字段{index}" for index in range(1, 11)], "集团一级库编码"],
        [[""] * 10 + ["9101"]],
    )
    return InputPaths(valid_base, valid_marketing, valid_bop, tmp_path)


def test_valid_august_inputs_identify_all_sources(valid_inputs: ValidInputs) -> None:
    result = precheck_inputs(valid_inputs.paths, QuoteMonth(2026, 8))

    assert result.fatal_issues == ()
    assert result.row_issues == ()
    assert set(result.identified_sheets) == {"base", "marketing", "bop"}
    assert result.identified_sheets["base"].rows[0][0] == "9101"


def test_missing_march_header_is_fatal(tmp_path: Path) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年7月结算报价（元/台）"],
        [["9101", 3999]],
    )

    result = precheck_inputs(_paths(tmp_path, base=base), QuoteMonth(2026, 8))

    assert any(issue.code == "MISSING_HISTORY_MONTH" for issue in result.fatal_issues)


def test_history_header_with_halfwidth_parentheses_is_not_an_exact_match(tmp_path: Path) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价(元/台)", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899]],
    )

    result = precheck_inputs(_paths(tmp_path, base=base), QuoteMonth(2026, 8))

    assert any(issue.code == "MISSING_HISTORY_MONTH" for issue in result.fatal_issues)


def test_history_header_with_surrounding_whitespace_is_not_an_exact_match(
    tmp_path: Path,
) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        [
            "集团一级库物料编码",
            " 2026年3月结算报价（元/台） ",
            "2026年7月结算报价（元/台）",
        ],
        [["9101", 3999, 3899]],
    )

    result = precheck_inputs(_paths(tmp_path, base=base), QuoteMonth(2026, 8))

    assert any(issue.code == "MISSING_HISTORY_MONTH" for issue in result.fatal_issues)


@pytest.mark.parametrize(
    ("source", "headers", "expected_code"),
    [
        ("base", ["物料编码"], "INVALID_SOURCE_TABLE"),
        (
            "marketing",
            [*[f"营销字段{index}" for index in range(1, 9)], "集团一级库编码"],
            "INVALID_SOURCE_TABLE",
        ),
        (
            "bop",
            [*[f"BOP字段{index}" for index in range(1, 11)], "物料编码"],
            "INVALID_SOURCE_TABLE",
        ),
    ],
)
def test_required_code_header_must_be_at_approved_position(
    tmp_path: Path,
    source: str,
    headers: list[str],
    expected_code: str,
) -> None:
    invalid = save_workbook(tmp_path / f"invalid-{source}.xlsx", headers, [["9101"] * len(headers)])

    result = precheck_inputs(_paths(tmp_path, **{source: invalid}), QuoteMonth(2026, 8))

    assert any(
        issue.code == expected_code and issue.source == source
        for issue in result.fatal_issues
    )
    assert source not in result.identified_sheets


def test_required_code_headers_are_normalized_before_validation(tmp_path: Path) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["　集团一级库物料编码　", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899]],
    )
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        [*[f"营销字段{index}" for index in range(1, 9)], "　物料编码　"],
        [[""] * 8 + ["9101"]],
    )
    bop = save_workbook(
        tmp_path / "bop.xlsx",
        [*[f"BOP字段{index}" for index in range(1, 11)], "　集团一级库编码　"],
        [[""] * 10 + ["9101"]],
    )

    result = precheck_inputs(
        InputPaths(base, marketing, bop, tmp_path),
        QuoteMonth(2026, 8),
    )

    assert result.fatal_issues == ()


def test_swapped_marketing_and_bop_files_are_both_fatal(valid_inputs: ValidInputs) -> None:
    original = valid_inputs.paths
    swapped = InputPaths(original.base, original.bop, original.marketing, original.output_dir)

    result = precheck_inputs(swapped, QuoteMonth(2026, 8))

    invalid_sources = {
        source
        for source in ("marketing", "bop")
        if any(
            issue.code == "INVALID_SOURCE_TABLE" and issue.source == source
            for issue in result.fatal_issues
        )
    }
    assert invalid_sources == {"marketing", "bop"}


@pytest.mark.parametrize("bad_contents", [b"not an xlsx archive", b""])
def test_unreadable_workbook_becomes_fatal_issue(tmp_path: Path, bad_contents: bytes) -> None:
    damaged = tmp_path / "damaged.xlsx"
    damaged.write_bytes(bad_contents)

    result = precheck_inputs(_paths(tmp_path, base=damaged), QuoteMonth(2026, 8))

    assert any(issue.code == "UNREADABLE_WORKBOOK" for issue in result.fatal_issues)


def test_missing_file_becomes_fatal_issue(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xlsx"

    result = precheck_inputs(_paths(tmp_path, marketing=missing), QuoteMonth(2026, 8))

    issue = next(issue for issue in result.fatal_issues if issue.code == "UNREADABLE_WORKBOOK")
    assert issue.message == "营销商品信息查询表文件无法读取：missing.xlsx"


def test_real_empty_workbook_becomes_fatal_issue(tmp_path: Path) -> None:
    empty = save_empty_workbook(tmp_path / "empty.xlsx")

    result = precheck_inputs(_paths(tmp_path, base=empty), QuoteMonth(2026, 8))

    issue = next(issue for issue in result.fatal_issues if issue.code == "UNREADABLE_WORKBOOK")
    assert issue.message == "基础表文件无法读取：empty.xlsx"
    assert all(
        detail not in issue.message
        for detail in ("ValueError", "WorkbookReadError", "first worksheet", "openpyxl")
    )


def test_real_encrypted_workbook_becomes_fatal_issue(tmp_path: Path) -> None:
    plain = save_workbook(
        tmp_path / "plain.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899]],
    )
    encrypted = encrypt_workbook(plain, tmp_path / "encrypted.xlsx")
    with encrypted.open("rb") as encrypted_file:
        assert msoffcrypto.OfficeFile(encrypted_file).is_encrypted()

    result = precheck_inputs(_paths(tmp_path, base=encrypted), QuoteMonth(2026, 8))

    issue = next(issue for issue in result.fatal_issues if issue.code == "UNREADABLE_WORKBOOK")
    assert issue.message == "基础表文件无法读取：encrypted.xlsx"
    assert all(
        detail not in issue.message
        for detail in (
            "BadZipFile",
            "ZIP",
            "OLE",
            "EncryptionInfo",
            "msoffcrypto",
            "openpyxl",
        )
    )


def test_too_few_columns_is_fatal_even_when_last_header_looks_valid(tmp_path: Path) -> None:
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        ["物料编码"],
        [["9101"]],
    )

    result = precheck_inputs(_paths(tmp_path, marketing=marketing), QuoteMonth(2026, 8))

    assert any(
        issue.code == "INVALID_SOURCE_TABLE" and issue.source == "marketing"
        for issue in result.fatal_issues
    )


def test_blank_and_duplicate_codes_are_row_issues_without_dropping_base_rows(
    tmp_path: Path,
) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899], ["", 3888, 3788], [" 9101 ", 3777, 3677]],
    )

    result = precheck_inputs(_paths(tmp_path, base=base), QuoteMonth(2026, 8))

    assert {(issue.code, issue.row_number) for issue in result.row_issues} == {
        ("DUPLICATE_MATERIAL_CODE", 2),
        ("BLANK_MATERIAL_CODE", 3),
        ("DUPLICATE_MATERIAL_CODE", 4),
    }
    assert len(result.identified_sheets["base"].rows) == 3


def test_row_issues_cover_fixed_code_columns_in_every_source(tmp_path: Path) -> None:
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        [*[f"营销字段{index}" for index in range(1, 9)], "物料编码"],
        [[""] * 8 + [""], [""] * 8 + ["9101"], [""] * 8 + ["9101"]],
    )
    bop = save_workbook(
        tmp_path / "bop.xlsx",
        [*[f"BOP字段{index}" for index in range(1, 11)], "集团一级库编码"],
        [[""] * 10 + [None], [""] * 10 + [9101.0], [""] * 10 + [" 9101 "]],
    )

    result = precheck_inputs(
        _paths(tmp_path, marketing=marketing, bop=bop),
        QuoteMonth(2026, 8),
    )

    messages = {
        (issue.code, issue.row_number, issue.source)
        for issue in result.row_issues
    }
    assert messages == {
        ("BLANK_MATERIAL_CODE", 2, "marketing"),
        ("DUPLICATE_MATERIAL_CODE", 3, "marketing"),
        ("DUPLICATE_MATERIAL_CODE", 4, "marketing"),
        ("BLANK_MATERIAL_CODE", 2, "bop"),
        ("DUPLICATE_MATERIAL_CODE", 3, "bop"),
        ("DUPLICATE_MATERIAL_CODE", 4, "bop"),
    }


def test_month_header_uses_unpadded_exact_chinese_format() -> None:
    assert month_header(QuoteMonth(2026, 3)) == "2026年3月结算报价（元/台）"


def test_source_table_exposes_rows_by_excel_column_letter(tmp_path: Path) -> None:
    path = save_workbook(
        tmp_path / "source.xlsx",
        ["集团一级库物料编码", "报价"],
        [["9101", 3999], ["9102", "无"]],
    )

    table = read_first_table(path)

    assert table.headers == {"集团一级库物料编码": 0, "报价": 1}
    assert table.records_by_column_letter() == (
        {"A": "9101", "B": 3999},
        {"A": "9102", "B": "无"},
    )


def test_precheck_business_messages_are_chinese_and_keep_machine_source(
    tmp_path: Path,
) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899], [" 9101 ", 3888, 3788], ["", 3777, 3677]],
    )
    invalid_marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        ["错误列"],
        [["9101"]],
    )

    result = precheck_inputs(
        _paths(tmp_path, base=base, marketing=invalid_marketing),
        QuoteMonth(2026, 8),
    )

    issues = (*result.fatal_issues, *result.row_issues)
    assert all(issue.source in {"base", "marketing"} for issue in issues)
    assert all(
        english not in issue.message
        for issue in issues
            for english in (
                "base workbook",
                "marketing workbook",
                "base row",
                "marketing row",
                "material code",
                "history column",
                "must have",
        )
    )
    assert any("基础表第2行重复物料编码9101" in issue.message for issue in issues)
    assert any("营销商品信息查询表至少需要9列" in issue.message for issue in issues)

from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from openpyxl.cell.cell import Cell  # type: ignore[import-untyped]
from openpyxl.drawing.image import Image as OpenpyxlImage  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from openpyxl.worksheet.worksheet import Worksheet  # type: ignore[import-untyped]
from openpyxl.xml.functions import tostring  # type: ignore[import-untyped]
from PIL import Image as PillowImage
import pytest

from quote_app.excel.template_builder import build_template


TEMPLATE_PATH = Path("resources/templates/quote_template.xlsx")
APPROVED_SOURCE_PATH = Path(
    os.environ.get(
        "QUOTE_APPROVED_SAMPLE",
        "/Users/yangguowei/Desktop/铺货报价系统/铺货报价需求书2026.7.24/"
        "2026年8月终端供货价报价表.xlsx",
    )
)
EXPECTED_HEADERS = (
    "终端类型（二级）",
    "品牌",
    "集团一级库物料编码",
    "集团一级库\n产品名称（型号）",
    "集团一级库\n市场通俗名称",
    "终端资源编码（B域）",
    "终端资源名称（B域）",
    "入中国移动总部库时间",
    "2026年3月结算报价（元/台）",
    "2026年7月结算报价（元/台）",
    "2026年8月结算报价（元/台）",
    "终端公司采购价",
    "终端公司全国采购系统均价",
    "货源情况（货源充足\\货源紧缺\\新品上市\\尾货期）",
    "集团一级库价格",
    "渠道买断价",
    "分销零售价",
    "京东自营或官网目前价格",
    "网址",
    "同配置机型",
    "同配置价格",
    "是否新增",
    "是否达到6个月需调价条件",
    "较上个月降价幅度",
    "6个月内降价幅度",
    "铺货价较采购价上浮比例（不高于4.5%）",
    "政企统谈价",
    "特殊情况备注",
    "采购类型\n（终端总部集采、自采）",
    "相关依据\n（总部文件及文号、合同名称及编码等）",
    "终端总部集采、省代自采合同价格",
    "总部文件/合同价格是否调价\n（时间、调整后价格）",
    "产品经理",
    "三网最低价",
    "京东自营/官旗价",
    "天猫官旗价价格",
    "品牌官网价价格",
    "京东自营/官旗价截图",
    "天猫官旗价截图",
    "品牌官网价截图",
)
EXPECTED_COLUMN_WIDTHS = {
    "A": 16.5,
    "H": 17.0803571428571,
    "K": 9.61607142857143,
    "N": 10.5089285714286,
    "X": 13.4196428571429,
    "AI": 27.5803571428571,
    "AL": 23.3303571428571,
    "AN": 13.0,
}
EXPECTED_A_TO_AN_STYLE_DIGEST = (
    "7f58e0c3f7013f61c7ebb7c69bae6d5ab8b0fb076aa10748f0735fae679f0b9f"
)


def _style_digest_for_first_two_rows(sheet: Worksheet) -> str:
    digest = sha256()
    for row in (1, 2):
        for column in range(1, 41):
            cell = sheet.cell(row, column)
            for style_component in (
                cell.font,
                cell.fill,
                cell.border,
                cell.alignment,
                cell.protection,
            ):
                digest.update(tostring(style_component.to_tree()))
            digest.update(cell.number_format.encode("utf-8"))
            digest.update(str(cell.pivotButton).encode())
            digest.update(str(cell.quotePrefix).encode())
    return digest.hexdigest()


def _style_semantics(cell: Cell) -> tuple[bytes | str | bool, ...]:
    return (
        tostring(cell.font.to_tree()),
        tostring(cell.fill.to_tree()),
        tostring(cell.border.to_tree()),
        tostring(cell.alignment.to_tree()),
        tostring(cell.protection.to_tree()),
        cell.number_format,
        cell.pivotButton,
        cell.quotePrefix,
    )


def _serialized(value: object) -> bytes:
    """Serialize an openpyxl serialisable value for semantic comparison."""
    assert hasattr(value, "to_tree")
    return tostring(value.to_tree())


@pytest.mark.parametrize("alias_kind", ["literal", "symlink", "hardlink"])
def test_build_template_rejects_source_destination_aliases_before_loading(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_text("not an xlsx file", encoding="utf-8")

    if alias_kind == "literal":
        destination = source
    elif alias_kind == "symlink":
        destination = tmp_path / "source-symlink.xlsx"
        try:
            destination.symlink_to(source)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symbolic links are unavailable: {error}")
    else:
        destination = tmp_path / "source-hardlink.xlsx"
        try:
            os.link(source, destination)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"hard links are unavailable: {error}")

    with pytest.raises(ValueError, match="源文件和目标文件不能是同一个文件"):
        build_template(source, destination)

    assert source.read_text(encoding="utf-8") == "not an xlsx file"


def test_build_script_cleans_explicit_source_into_explicit_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dirty-source.xlsx"
    destination = tmp_path / "nested" / "clean-template.xlsx"
    image_path = tmp_path / "cell-image.png"
    PillowImage.new("RGB", (2, 2), "red").save(image_path)

    source_workbook = Workbook()
    source_sheet = source_workbook.active
    source_sheet.title = "5G手机"
    source_sheet.append(["字段A", "字段B", "字段C"])
    source_sheet.append(["示例", "=1+1", '=_xlfn.DISPIMG("dirty",1)'])
    source_sheet.append(["说明", "必须删除", "必须删除"])
    source_sheet["A2"].font = Font(name="宋体", bold=True)
    source_sheet["A2"].fill = PatternFill("solid", fgColor="FFFF00")
    source_sheet["A2"].alignment = Alignment(horizontal="center", wrap_text=True)
    source_sheet["A2"].number_format = "0.00%"
    source_sheet.column_dimensions["A"].width = 21.5
    source_sheet.row_dimensions[1].height = 31.0
    source_sheet.row_dimensions[2].height = 27.0
    source_sheet.freeze_panes = "A2"
    source_sheet.sheet_view.showGridLines = False
    source_sheet.page_setup.orientation = "landscape"
    source_sheet.print_title_rows = "1:2"
    source_sheet.print_area = "A1:C3"

    hidden_sheet = source_workbook.create_sheet("隐藏数据")
    hidden_sheet.sheet_state = "hidden"
    wps_sheet = source_workbook.create_sheet("WpsReserved_CellImgList")
    wps_sheet.sheet_state = "veryHidden"
    wps_sheet.add_image(OpenpyxlImage(image_path), "A1")
    source_workbook.save(source)
    source_workbook.close()

    subprocess.run(
        [
            sys.executable,
            "scripts/build_quote_template.py",
            str(source),
            str(destination),
        ],
        check=True,
    )

    clean_workbook = load_workbook(destination, data_only=False)
    try:
        assert clean_workbook.sheetnames == ["5G手机"]
        clean_sheet = clean_workbook["5G手机"]
        assert clean_sheet.max_row == 2
        assert [clean_sheet.cell(1, column).value for column in range(1, 4)] == [
            "字段A",
            "字段B",
            "字段C",
        ]
        assert all(clean_sheet.cell(2, column).value is None for column in range(1, 4))
        assert clean_sheet["A2"].font.name == "宋体"
        assert clean_sheet["A2"].font.bold is True
        assert clean_sheet["A2"].fill.fill_type == "solid"
        assert clean_sheet["A2"].fill.fgColor.rgb == "00FFFF00"
        assert clean_sheet["A2"].alignment.horizontal == "center"
        assert clean_sheet["A2"].alignment.wrap_text is True
        assert clean_sheet["A2"].number_format == "0.00%"
        assert clean_sheet.column_dimensions["A"].width == 21.5
        assert clean_sheet.row_dimensions[1].height == 31.0
        assert clean_sheet.row_dimensions[2].height == 27.0
        assert clean_sheet.freeze_panes == "A2"
        assert clean_sheet.sheet_view.showGridLines is False
        assert clean_sheet.page_setup.orientation == "landscape"
        assert clean_sheet.print_title_rows == "$1:$2"
        assert str(clean_sheet.print_area) == "'5G手机'!$A$1:$C$3"
        assert not clean_sheet._images
    finally:
        clean_workbook.close()

    unchanged_source = load_workbook(source, data_only=False)
    try:
        assert unchanged_source.sheetnames == [
            "5G手机",
            "隐藏数据",
            "WpsReserved_CellImgList",
        ]
        assert unchanged_source["5G手机"]["A2"].value == "示例"
        assert unchanged_source["5G手机"]["A3"].value == "说明"
    finally:
        unchanged_source.close()

    with ZipFile(destination) as archive:
        assert not any(
            part == "xl/cellimages.xml"
            or part.startswith("xl/media/")
            or part.startswith("xl/drawings/")
            for part in archive.namelist()
        )


def test_load_clean_template_returns_editable_workbook() -> None:
    from quote_app.excel import load_clean_template

    workbook = load_clean_template(TEMPLATE_PATH)
    try:
        assert workbook.read_only is False
        assert workbook["5G手机"]["A1"].value == EXPECTED_HEADERS[0]
    finally:
        workbook.close()


def test_clean_template_has_only_header_and_empty_style_row() -> None:
    workbook = load_workbook(TEMPLATE_PATH, data_only=False)
    try:
        assert workbook.sheetnames == ["5G手机"]
        sheet = workbook["5G手机"]
        assert sheet.max_row == 2
        assert tuple(sheet.cell(1, column).value for column in range(1, 41)) == EXPECTED_HEADERS
        assert all(sheet.cell(2, column).value is None for column in range(1, 41))
        assert sheet["A3"].value is None
        assert not sheet._images
    finally:
        workbook.close()


def test_clean_template_preserves_all_approved_format_and_page_settings() -> None:
    assert APPROVED_SOURCE_PATH.is_file(), (
        "Approved quotation sample is required; set QUOTE_APPROVED_SAMPLE "
        "when it is stored elsewhere."
    )
    source_workbook = load_workbook(APPROVED_SOURCE_PATH, data_only=False)
    workbook = load_workbook(TEMPLATE_PATH, data_only=False)
    try:
        source_sheet = source_workbook["5G手机"]
        sheet = workbook["5G手机"]

        for column in range(1, 41):
            letter = source_sheet.cell(1, column).column_letter
            assert sheet.column_dimensions[letter].width == (
                source_sheet.column_dimensions[letter].width
            ), letter

        for row in (1, 2):
            source_dimension = source_sheet.row_dimensions[row]
            target_dimension = sheet.row_dimensions[row]
            assert (
                target_dimension.height,
                target_dimension.hidden,
                target_dimension.outlineLevel,
                target_dimension.collapsed,
                target_dimension.thickTop,
                target_dimension.thickBot,
                target_dimension.style_id,
            ) == (
                source_dimension.height,
                source_dimension.hidden,
                source_dimension.outlineLevel,
                source_dimension.collapsed,
                source_dimension.thickTop,
                source_dimension.thickBot,
                source_dimension.style_id,
            ), row

        for row in (1, 2):
            for column in range(1, 41):
                coordinate = sheet.cell(row, column).coordinate
                assert _style_semantics(sheet[coordinate]) == _style_semantics(
                    source_sheet[coordinate]
                ), coordinate

        assert _style_digest_for_first_two_rows(sheet) == EXPECTED_A_TO_AN_STYLE_DIGEST
        assert sheet.freeze_panes == source_sheet.freeze_panes
        assert tuple(map(str, sheet.merged_cells.ranges)) == tuple(
            str(item)
            for item in source_sheet.merged_cells.ranges
            if item.max_row <= 2
        )
        assert _serialized(sheet.sheet_view) == _serialized(source_sheet.sheet_view)
        assert _serialized(sheet.sheet_format) == _serialized(source_sheet.sheet_format)
        assert _serialized(sheet.sheet_properties) == _serialized(
            source_sheet.sheet_properties
        )
        assert _serialized(sheet.page_margins) == _serialized(source_sheet.page_margins)
        assert _serialized(sheet.page_setup) == _serialized(source_sheet.page_setup)
        assert _serialized(sheet.print_options) == _serialized(source_sheet.print_options)
        assert sheet.print_title_rows == source_sheet.print_title_rows
        assert sheet.print_title_cols == source_sheet.print_title_cols
        assert str(sheet.print_area) == str(source_sheet.print_area)
    finally:
        workbook.close()
        source_workbook.close()


def test_clean_template_package_has_no_images_or_wps_dispimg_content() -> None:
    with ZipFile(TEMPLATE_PATH) as archive:
        package_parts = set(archive.namelist())
        assert not any(
            part == "xl/cellimages.xml"
            or part.startswith("xl/media/")
            or part.startswith("xl/drawings/")
            for part in package_parts
        )

        xml_parts = {
            part
            for part in package_parts
            if part.endswith(".xml") or part.endswith(".rels")
        }
        for part in xml_parts:
            content = archive.read(part).lower()
            assert b"dispimg" not in content, part
            assert b"cellimage" not in content, part
            assert b"<drawing" not in content, part
            assert b"<legacydrawing" not in content, part
            if part.endswith(".rels"):
                assert b"/drawing" not in content, part
                assert b"/image" not in content, part
                assert b"drawings/" not in content, part
                assert b"media/" not in content, part

        content_types = archive.read("[Content_Types].xml").lower()
        assert b"cellimage" not in content_types
        assert b"drawing" not in content_types
        assert b"image/" not in content_types

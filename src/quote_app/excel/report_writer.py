from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import count
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterator

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]
from openpyxl.worksheet.worksheet import Worksheet  # type: ignore[import-untyped]

from quote_app.core.normalization import normalize_text
from quote_app.domain.models import InputPaths, QuoteMonth, QuoteRow
from quote_app.domain.statuses import RowStatus


SUPPORTED_BRANDS = frozenset(("HONOR", "华为", "维沃", "欧珀", "小米", "苹果", "ZTE中兴"))
CHANNEL_PENDING = "待人工补充"
STATUS_LABELS = {
    RowStatus.COMPLETED: "完成",
    RowStatus.PARTIAL: "部分完成",
    RowStatus.FAILED: "失败",
    RowStatus.UNSUPPORTED: "不支持",
}
STATUS_FILLS = {
    RowStatus.COMPLETED: PatternFill("solid", fgColor="70AD47"),
    RowStatus.PARTIAL: PatternFill("solid", fgColor="F4B183"),
    RowStatus.FAILED: PatternFill("solid", fgColor="F8696B"),
    RowStatus.UNSUPPORTED: PatternFill("solid", fgColor="F4B183"),
}
DETAIL_HEADERS = (
    "产品经理",
    "品牌",
    "集团一级库物料编码",
    "产品名称",
    "配置",
    "基础表关联状态",
    "营销表关联状态",
    "BOP关联状态",
    "京东渠道状态",
    "天猫渠道状态",
    "官网渠道状态",
    "最终状态",
    "失败步骤",
    "原因",
    "建议操作",
)
_HARD_ASSOCIATION_ISSUES = frozenset(("MARKETING_NOT_FOUND", "MARKETING_CONFLICT"))


@dataclass(frozen=True, slots=True)
class RunSummary:
    total_rows: int
    completed_rows: int
    partial_rows: int
    failed_rows: int
    unsupported_rows: int
    manual_supplement_rows: int


@dataclass(frozen=True, slots=True)
class ReportWriteRequest:
    quote_month: QuoteMonth
    rows: list[QuoteRow]
    output_dir: Path
    input_paths: InputPaths | None = None
    quote_path: Path | None = None
    run_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RowAssessment:
    row: QuoteRow
    status: RowStatus
    manual_supplement: bool
    manager: str
    brand: str
    base_state: str
    marketing_state: str
    bop_state: str
    channel_states: tuple[str, str, str]
    failed_step: str
    reason: str
    recommendation: str


def assess_rows(rows: list[QuoteRow]) -> list[RowAssessment]:
    """Classify core-only rows using one explicit source of report truth."""
    return [_assess_row(row) for row in rows]


def summarize_assessments(assessments: list[RowAssessment]) -> RunSummary:
    status_counts = Counter(assessment.status for assessment in assessments)
    return RunSummary(
        total_rows=len(assessments),
        completed_rows=status_counts[RowStatus.COMPLETED],
        partial_rows=status_counts[RowStatus.PARTIAL],
        failed_rows=status_counts[RowStatus.FAILED],
        unsupported_rows=status_counts[RowStatus.UNSUPPORTED],
        manual_supplement_rows=sum(
            assessment.manual_supplement for assessment in assessments
        ),
    )


def summarize_rows(rows: list[QuoteRow]) -> RunSummary:
    return summarize_assessments(assess_rows(rows))


def write_execution_report(request: ReportWriteRequest) -> Path:
    """Write a collision-safe report without modifying any source workbook."""
    output_dir = _ensure_output_directory(Path(request.output_dir))
    assessments = assess_rows(request.rows)
    summary = summarize_assessments(assessments)
    workbook = Workbook()
    temporary_path: Path | None = None
    try:
        overview = workbook.active
        overview.title = "运行总览"
        detail = workbook.create_sheet("处理明细")
        _write_overview(
            overview,
            request,
            assessments,
            summary,
            request.run_at or datetime.now(),
        )
        _write_detail(detail, assessments)
        temporary_path = _create_temporary_path(output_dir)
        try:
            workbook.save(temporary_path)
        except OSError:
            raise ValueError(f"执行报告无法写入输出目录：{output_dir}") from None
        workbook.close()
        return _publish_without_overwrite(
            temporary_path,
            output_dir,
            request.quote_month,
        )
    finally:
        workbook.close()
        if temporary_path is not None:
            _remove_temporary_file(temporary_path)


def _assess_row(row: QuoteRow) -> RowAssessment:
    issue_codes = {issue.code for issue in row.issues}
    brand = normalize_text(row.cells.get("B")) or "未识别"
    manager = normalize_text(row.cells.get("AB")) or "未分配"
    issue_reason = "；".join(f"{issue.code}：{issue.message}" for issue in row.issues)
    channel_states = _channel_states(row)

    if issue_codes & _HARD_ASSOCIATION_ISSUES:
        status = RowStatus.FAILED
        manual_supplement = False
        failed_step = "营销商品信息关联"
        reason = issue_reason
        recommendation = "核对营销商品信息查询表中的物料编码及重复记录后重新运行"
    elif brand not in SUPPORTED_BRANDS:
        status = RowStatus.UNSUPPORTED
        manual_supplement = True
        failed_step = "网站渠道自动处理"
        reason = (
            f"品牌“{brand}”不在首版支持的7个品牌范围内"
            + (f"；{issue_reason}" if issue_reason else "")
        )
        recommendation = "人工补充 AI:AN 的价格、链接和截图，并反馈新增品牌规则"
    elif all(state == "已完成" for state in channel_states) and not issue_codes:
        status = RowStatus.COMPLETED
        manual_supplement = False
        failed_step = ""
        reason = "核心关联及三个网站渠道均已完成"
        recommendation = "无需操作"
    else:
        status = RowStatus.PARTIAL
        manual_supplement = True
        failed_step = "网站渠道自动处理"
        reason = issue_reason or "核心表格关联已完成，网站价格及截图尚未处理"
        recommendation = "人工补充 AI:AN，或在后续浏览器自动化版本中继续处理"

    return RowAssessment(
        row=row,
        status=status,
        manual_supplement=manual_supplement,
        manager=manager,
        brand=brand,
        base_state="已关联",
        marketing_state=_marketing_state(issue_codes),
        bop_state=_bop_state(row, issue_codes),
        channel_states=channel_states,
        failed_step=failed_step,
        reason=reason,
        recommendation=recommendation,
    )


def _marketing_state(issue_codes: set[str]) -> str:
    if "MARKETING_NOT_FOUND" in issue_codes:
        return "未找到"
    if "MARKETING_CONFLICT" in issue_codes:
        return "记录冲突"
    if "WEB_FIELDS_MISSING" in issue_codes:
        return "已关联（网站字段不完整）"
    return "已关联"


def _bop_state(row: QuoteRow, issue_codes: set[str]) -> str:
    if "BOP_CONFLICT" in issue_codes:
        return "记录冲突"
    if row.cells.get("F") == "申请配置" and row.cells.get("G") == "申请配置":
        return "无需匹配（申请配置）"
    return "已关联"


def _channel_states(row: QuoteRow) -> tuple[str, str, str]:
    return (
        _channel_state(row, "AI", "AL"),
        _channel_state(row, "AJ", "AM"),
        _channel_state(row, "AK", "AN"),
    )


def _channel_state(row: QuoteRow, price_column: str, evidence_column: str) -> str:
    if normalize_text(row.cells.get(price_column)) and normalize_text(
        row.cells.get(evidence_column)
    ):
        return "已完成"
    return CHANNEL_PENDING


def _write_overview(
    sheet: Worksheet,
    request: ReportWriteRequest,
    assessments: list[RowAssessment],
    summary: RunSummary,
    run_at: datetime,
) -> None:
    sheet.append(["资金物流平台铺货报价执行报告"])
    sheet.append(["报价月份", f"{request.quote_month.year}年{request.quote_month.month:02d}月"])
    sheet.append(["运行时间", run_at.strftime("%Y-%m-%d %H:%M:%S")])
    for label, path in _metadata_rows(request):
        sheet.append([label, str(path) if path is not None else "未提供"])
    sheet.append([])
    sheet.append(["汇总指标", "数量"])
    for label, value in (
        ("报价总行数", summary.total_rows),
        ("处理完成", summary.completed_rows),
        ("部分完成", summary.partial_rows),
        ("处理失败", summary.failed_rows),
        ("不支持品牌", summary.unsupported_rows),
        ("待人工补充", summary.manual_supplement_rows),
    ):
        sheet.append([label, value])

    sheet.append([])
    sheet.append(["品牌汇总"])
    sheet.append(["品牌", "总行数", "完成", "部分完成", "失败", "不支持", "待人工补充"])
    _append_breakdown(sheet, assessments, "brand")

    sheet.append([])
    sheet.append(["产品经理汇总"])
    sheet.append(
        ["产品经理", "总行数", "完成", "部分完成", "失败", "不支持", "待人工补充"]
    )
    _append_breakdown(sheet, assessments, "manager")

    sheet.append([])
    sheet.append(["渠道状态汇总"])
    sheet.append(["渠道", "已完成", "待人工补充", "失败"])
    for channel_index, channel in enumerate(("京东", "天猫", "官网")):
        states = Counter(
            assessment.channel_states[channel_index] for assessment in assessments
        )
        sheet.append(
            [
                channel,
                states["已完成"],
                states[CHANNEL_PENDING],
                states["失败"],
            ]
        )

    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.column_dimensions["A"].width = 24
    sheet.column_dimensions["B"].width = 72
    for column in ("C", "D", "E", "F", "G"):
        sheet.column_dimensions[column].width = 14
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet["A1"].font = Font(bold=True, size=16, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="4472C4")
    for row in range(1, sheet.max_row + 1):
        if sheet.cell(row, 1).value in (
            "汇总指标",
            "品牌",
            "产品经理",
            "渠道",
        ):
            for cell in sheet[row]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="5B9BD5")


def _metadata_rows(request: ReportWriteRequest) -> tuple[tuple[str, Path | None], ...]:
    inputs = request.input_paths
    return (
        ("基础表", inputs.base if inputs else None),
        ("营销商品信息查询表", inputs.marketing if inputs else None),
        ("BOP资源信息表", inputs.bop if inputs else None),
        ("报价表输出", request.quote_path),
    )


def _append_breakdown(
    sheet: Worksheet,
    assessments: list[RowAssessment],
    field: str,
) -> None:
    groups: defaultdict[str, list[RowAssessment]] = defaultdict(list)
    for assessment in assessments:
        groups[getattr(assessment, field)].append(assessment)
    for name in sorted(groups):
        group = groups[name]
        counts = Counter(item.status for item in group)
        sheet.append(
            [
                name,
                len(group),
                counts[RowStatus.COMPLETED],
                counts[RowStatus.PARTIAL],
                counts[RowStatus.FAILED],
                counts[RowStatus.UNSUPPORTED],
                sum(item.manual_supplement for item in group),
            ]
        )


def _write_detail(sheet: Worksheet, assessments: list[RowAssessment]) -> None:
    sheet.append(list(DETAIL_HEADERS))
    for assessment in assessments:
        row = assessment.row
        sheet.append(
            [
                assessment.manager,
                assessment.brand,
                row.material_code,
                row.cells.get("E"),
                row.cells.get("G"),
                assessment.base_state,
                assessment.marketing_state,
                assessment.bop_state,
                *assessment.channel_states,
                STATUS_LABELS[assessment.status],
                assessment.failed_step,
                assessment.reason,
                assessment.recommendation,
            ]
        )
        status_cell = sheet.cell(sheet.max_row, 12)
        status_cell.fill = STATUS_FILLS[assessment.status]

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:O{max(1, sheet.max_row)}"
    sheet.sheet_view.showGridLines = False
    widths = (16, 14, 22, 24, 18, 20, 24, 24, 16, 16, 16, 14, 22, 52, 52)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def _ensure_output_directory(output_dir: Path) -> Path:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError(f"输出目录无法创建或使用：{output_dir}") from None
    if not output_dir.is_dir():
        raise ValueError(f"输出目录无法创建或使用：{output_dir}")
    return output_dir


def _create_temporary_path(output_dir: Path) -> Path:
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=".report-",
            suffix=".tmp.xlsx",
            dir=output_dir,
            delete=False,
        ) as temporary_file:
            return Path(temporary_file.name)
    except OSError:
        raise ValueError(f"执行报告无法写入输出目录：{output_dir}") from None


def _candidate_output_paths(
    output_dir: Path,
    quote_month: QuoteMonth,
) -> Iterator[Path]:
    stem = f"{quote_month.year}年{quote_month.month:02d}月报价执行报告"
    yield output_dir / f"{stem}.xlsx"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    yield output_dir / f"{stem}-{timestamp}.xlsx"
    for suffix in count(2):
        yield output_dir / f"{stem}-{timestamp}-{suffix}.xlsx"


def _publish_without_overwrite(
    temporary_path: Path,
    output_dir: Path,
    quote_month: QuoteMonth,
) -> Path:
    for candidate in _candidate_output_paths(output_dir, quote_month):
        try:
            os.link(temporary_path, candidate)
        except FileExistsError:
            continue
        except OSError:
            raise ValueError(f"执行报告无法安全发布到输出目录：{output_dir}") from None
        return candidate
    raise AssertionError("unreachable")


def _remove_temporary_file(temporary_path: Path) -> None:
    try:
        temporary_path.unlink(missing_ok=True)
    except OSError:
        raise ValueError(f"执行报告临时文件无法清理：{temporary_path}") from None

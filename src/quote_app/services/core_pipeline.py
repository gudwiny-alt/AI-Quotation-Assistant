from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quote_app.core.association import associate_rows
from quote_app.core.precheck import precheck_inputs
from quote_app.domain.models import InputPaths, Issue, QuoteMonth, QuoteRow
from quote_app.excel.quote_writer import QuoteWriteRequest, write_quote_workbook
from quote_app.excel.report_writer import (
    ReportWriteRequest,
    RunSummary,
    summarize_rows,
    write_execution_report,
)
from quote_app.resources import bundled_resource_path


DEFAULT_TEMPLATE_PATH = bundled_resource_path("resources/templates/quote_template.xlsx")


@dataclass(frozen=True, slots=True)
class CoreRunResult:
    quote_path: Path
    report_path: Path
    summary: RunSummary
    rows: list[QuoteRow]


class CorePipelineError(ValueError):
    """Stable business exception raised before output generation."""

    def __init__(
        self,
        issues: tuple[Issue, ...],
        heading: str = "输入预检查未通过",
    ) -> None:
        self.issues = issues
        messages = "；".join(issue.message for issue in issues)
        super().__init__(f"{heading}：{messages}")


def run_core_pipeline(
    paths: InputPaths,
    quote_month: QuoteMonth,
    template_path: str | Path = DEFAULT_TEMPLATE_PATH,
) -> CoreRunResult:
    precheck = precheck_inputs(paths, quote_month)
    if precheck.fatal_issues:
        raise CorePipelineError(precheck.fatal_issues)

    rows = associate_rows(
        precheck.identified_sheets["base"],
        precheck.identified_sheets["marketing"],
        precheck.identified_sheets["bop"],
        quote_month,
    )
    rows = [row for row in rows if row.material_code]
    if not rows:
        raise CorePipelineError(
            (
                Issue(
                    code="NO_QUOTABLE_ROWS",
                    message="基础表没有可报价的非空物料编码",
                    fatal=True,
                ),
            )
        )

    _merge_base_precheck_issues(rows, precheck.row_issues)
    try:
        quote_path = write_quote_workbook(
            QuoteWriteRequest(
                quote_month=quote_month,
                rows=rows,
                template_path=template_path,
                output_dir=paths.output_dir,
            )
        )
    except Exception:
        raise _output_error(
            "OUTPUT_PAIR_FAILED",
            "报价表生成失败，未产生本次报价输出和执行报告",
        ) from None

    summary = summarize_rows(rows)
    try:
        report_path = write_execution_report(
            ReportWriteRequest(
                quote_month=quote_month,
                rows=rows,
                output_dir=paths.output_dir,
                input_paths=paths,
                quote_path=quote_path,
            )
        )
    except Exception:
        try:
            quote_path.unlink(missing_ok=True)
        except OSError:
            raise _output_error(
                "ROLLBACK_FAILED",
                (
                    "执行报告生成失败，且本次报价表无法自动回滚；"
                    f"请手工删除孤立文件：{quote_path}"
                ),
            ) from None
        raise _output_error(
            "OUTPUT_PAIR_FAILED",
            "执行报告生成失败，本次报价表已自动回滚，未留下不完整输出",
        ) from None

    return CoreRunResult(
        quote_path=quote_path,
        report_path=report_path,
        summary=summary,
        rows=rows,
    )


def _output_error(code: str, message: str) -> CorePipelineError:
    return CorePipelineError(
        (
            Issue(
                code=code,
                message=message,
                fatal=True,
            ),
        ),
        heading="报价输出未完成",
    )


def _merge_base_precheck_issues(
    rows: list[QuoteRow],
    row_issues: tuple[Issue, ...],
) -> None:
    base_issues_by_row: dict[int, list[Issue]] = {}
    for issue in row_issues:
        if issue.row_number is None or issue.source != "base":
            continue
        base_issues_by_row.setdefault(issue.row_number, []).append(issue)
    for row in rows:
        row.issues.extend(base_issues_by_row.get(row.source_row_number, ()))

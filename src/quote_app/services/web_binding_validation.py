from __future__ import annotations

from quote_app.core.normalization import normalize_brand, normalize_code, normalize_text
from quote_app.domain.models import QuoteRow
from quote_app.services.web_pipeline import QueryKey
from quote_app.sites.matching import normalize_product_text
from quote_app.tasks.models import WebsiteChannel, WebsiteTask


SUPPORTED_WEB_BRANDS = frozenset(
    ("HONOR", "华为", "维沃", "欧珀", "小米", "苹果", "ZTE中兴")
)


def validate_website_task_bindings(
    rows: tuple[QuoteRow, ...] | list[QuoteRow],
    tasks: tuple[WebsiteTask, ...],
) -> dict[str, WebsiteTask]:
    """Reject website tasks that do not bind to this exact quotation snapshot."""
    tasks_by_id: dict[str, WebsiteTask] = {}
    row_channels: set[tuple[int, WebsiteChannel]] = set()
    batch_run_id: str | None = None
    for task in tasks:
        if task.task_id in tasks_by_id:
            raise ValueError("website tasks contain a duplicate task_id")
        if batch_run_id is None:
            batch_run_id = task.run_id
        elif task.run_id != batch_run_id:
            raise ValueError("website tasks must share one run_id")
        row_index = task.output_row_number - 2
        if row_index < 0 or row_index >= len(rows):
            raise ValueError("website task output row is outside quotation rows")
        row = rows[row_index]
        if (
            task.source_row_number != row.source_row_number
            or normalize_code(task.material_code)
            != normalize_code(row.material_code)
        ):
            raise ValueError("website task does not bind to its quotation row")
        row_query_fields = _row_query_fields(row)
        report_brand = normalize_product_text(
            normalize_brand(row.cells.get("B")),
        )
        if (
            report_brand not in SUPPORTED_WEB_BRANDS
            or row_query_fields[0] not in SUPPORTED_WEB_BRANDS
        ):
            raise ValueError("报价行品牌不支持网站自动处理")
        if report_brand != row_query_fields[0]:
            raise ValueError("报价行显示品牌与网站查询品牌不一致")
        if (
            normalize_product_text(normalize_text(row.cells.get("E")))
            != row_query_fields[1]
        ):
            raise ValueError("报价行显示型号与网站查询型号不一致")
        if _task_query_fields(task) != row_query_fields:
            raise ValueError("网站任务查询字段与报价行不一致")
        row_channel = (task.output_row_number, task.channel)
        if row_channel in row_channels:
            raise ValueError("website tasks contain a duplicate row/channel")
        row_channels.add(row_channel)
        tasks_by_id[task.task_id] = task
    return tasks_by_id


def _task_query_fields(task: WebsiteTask) -> tuple[str, str, str, str, str]:
    key = QueryKey.from_task(task)
    return key.brand, key.model_name, key.ram, key.storage, key.color


def _row_query_fields(row: QuoteRow) -> tuple[str, str, str, str, str]:
    query = row.web_query
    brand_text = normalize_product_text(query.brand or "")
    return (
        normalize_product_text(normalize_brand(brand_text)),
        normalize_product_text(query.model_name or ""),
        normalize_product_text(query.ram or ""),
        normalize_product_text(query.storage or ""),
        normalize_product_text(query.color or ""),
    )

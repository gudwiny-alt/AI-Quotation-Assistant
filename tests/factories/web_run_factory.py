from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from quote_app.domain.models import QuoteMonth, QuoteRow, WebQuery
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceRectangle,
    EvidenceState,
)
from quote_app.services.web_to_excel import (
    WebToExcelRequest,
    WebToExcelResult,
    write_web_results_to_excel,
)
from quote_app.tasks.models import (
    BusinessOutcome,
    TaskState,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
)

TEMPLATE_PATH = Path("resources/templates/quote_template.xlsx")
CHANNELS = (
    WebsiteChannel.JD,
    WebsiteChannel.TMALL,
    WebsiteChannel.OFFICIAL,
)
_EVIDENCE_STATES = {
    BusinessOutcome.PRICE_FOUND: EvidenceState.NORMAL,
    BusinessOutcome.NO_MODEL: EvidenceState.NO_MODEL,
    BusinessOutcome.CAPACITY_UNAVAILABLE: EvidenceState.CAPACITY_UNAVAILABLE,
    BusinessOutcome.COLOR_UNAVAILABLE: EvidenceState.COLOR_UNAVAILABLE,
    BusinessOutcome.SOLD_OUT: EvidenceState.SOLD_OUT,
}
_COLORS = {
    WebsiteChannel.JD: (210, 40, 40),
    WebsiteChannel.TMALL: (240, 100, 20),
    WebsiteChannel.OFFICIAL: (20, 90, 210),
}


@dataclass(frozen=True, slots=True)
class WebRunFixture:
    quote_path: Path
    report_path: Path
    rows: tuple[QuoteRow, ...]
    tasks: tuple[WebsiteTask, ...]
    results: tuple[WebsiteResult, ...]
    observations: tuple[WebsiteObservationCheckpoint, ...]


def make_quote_row(
    output_row_number: int = 2,
    *,
    material_code: str | None = None,
    brand: str = "小米",
    model_name: str = "小米 15",
    ram: str = "12GB",
    storage: str = "256GB",
    color: str = "黑色",
) -> QuoteRow:
    source_row_number = output_row_number
    code = material_code or f"910{output_row_number - 1}"
    return QuoteRow(
        source_row_number=source_row_number,
        material_code=code,
        cells={
            "A": "智能手机",
            "B": brand,
            "C": code,
            "E": model_name,
            "F": "已配置",
            "G": f"{ram}+{storage}",
            "I": 4599,
            "J": 4499,
            "AG": f"经理{output_row_number - 1}",
        },
        web_query=WebQuery(
            brand=brand,
            model_name=model_name,
            ram=ram,
            storage=storage,
            color=color,
        ),
    )


def make_tasks(
    rows: Sequence[QuoteRow],
    *,
    run_id: str = "run-web-fixture",
) -> tuple[WebsiteTask, ...]:
    tasks: list[WebsiteTask] = []
    for output_row_number, row in enumerate(rows, start=2):
        query = row.web_query
        assert query.brand
        assert query.model_name
        assert query.ram
        assert query.storage
        assert query.color
        for channel in CHANNELS:
            tasks.append(
                WebsiteTask(
                    task_id=f"{run_id}-{output_row_number}-{channel.value}",
                    run_id=run_id,
                    source_row_number=row.source_row_number,
                    output_row_number=output_row_number,
                    material_code=row.material_code,
                    brand=query.brand,
                    model_name=query.model_name,
                    ram=query.ram,
                    storage=query.storage,
                    color=query.color,
                    channel=channel,
                )
            )
    return tuple(tasks)


def make_evidence(
    directory: Path,
    task: WebsiteTask,
    outcome: BusinessOutcome,
    *,
    size: tuple[int, int] = (240, 120),
) -> EvidenceRecord:
    path = directory / f"{task.task_id}-{outcome.value}.png"
    Image.new("RGB", size, _COLORS[task.channel]).save(path)
    annotations: tuple[EvidenceRectangle, ...] = ()
    if outcome is BusinessOutcome.SOLD_OUT:
        annotations = (
            EvidenceRectangle(
                role="stock_status",
                x=10,
                y=10,
                width=80,
                height=30,
            ),
        )
    return EvidenceRecord(
        state=_EVIDENCE_STATES[outcome],
        path=path,
        sha256=sha256(path.read_bytes()).hexdigest(),
        pixel_width=size[0],
        pixel_height=size[1],
        captured_at=datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc),
        validation_code="CAPTURE_OK",
        annotations=annotations,
    )


def make_business_result(
    directory: Path,
    task: WebsiteTask,
    *,
    outcome: BusinessOutcome = BusinessOutcome.PRICE_FOUND,
    price: Decimal | None = None,
    url: str | None = None,
) -> WebsiteResult:
    resolved_price = (
        price
        if outcome is BusinessOutcome.PRICE_FOUND
        else None
    )
    if outcome is BusinessOutcome.PRICE_FOUND and resolved_price is None:
        resolved_price = Decimal("4399")
    return WebsiteResult(
        task_id=task.task_id,
        state=TaskState.SUCCEEDED,
        outcome=outcome,
        price=resolved_price,
        url=url or f"https://{task.channel.value}.example/product",
        evidence=make_evidence(directory, task, outcome),
        diagnostic_path=None,
        error_code=None,
        error_message=None,
    )


def make_technical_result(
    task: WebsiteTask,
    *,
    code: str = "PAGE_TIMEOUT",
    message: str = "页面在限定时间内未稳定",
) -> WebsiteResult:
    return WebsiteResult(
        task_id=task.task_id,
        state=TaskState.TECHNICAL_FAILURE,
        outcome=None,
        price=None,
        url=None,
        evidence=None,
        diagnostic_path=Path(f"/tmp/{task.task_id}-diagnostic.png"),
        error_code=code,
        error_message=message,
    )


def make_observation_checkpoint(
    task: WebsiteTask,
    *,
    outcome: BusinessOutcome = BusinessOutcome.PRICE_FOUND,
    price: Decimal | None = None,
    url: str | None = None,
) -> WebsiteObservationCheckpoint:
    resolved_price = price if outcome is BusinessOutcome.PRICE_FOUND else None
    if outcome is BusinessOutcome.PRICE_FOUND and resolved_price is None:
        resolved_price = Decimal("4399")
    return WebsiteObservationCheckpoint(
        task_id=task.task_id,
        outcome=outcome,
        price=resolved_price,
        url=url or f"https://{task.channel.value}.example/product",
        observed_at=datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc),
    )


def run_web_fixture(
    directory: Path,
    rows: Sequence[QuoteRow],
    tasks: Sequence[WebsiteTask],
    results: Sequence[WebsiteResult],
    *,
    observations: Sequence[WebsiteObservationCheckpoint] = (),
) -> WebRunFixture:
    output = write_web_results_to_excel(
        WebToExcelRequest(
            quote_month=QuoteMonth(2026, 8),
            rows=tuple(rows),
            tasks=tuple(tasks),
            results=tuple(results),
            observations=tuple(observations),
            output_dir=directory,
            template_path=TEMPLATE_PATH,
            run_at=datetime(2026, 7, 26, 9, 30),
        )
    )
    return _fixture(output, tasks, results, observations)


def run_three_channel_fixture(directory: Path) -> WebRunFixture:
    row = make_quote_row()
    tasks = make_tasks((row,))
    prices = {
        WebsiteChannel.JD: Decimal("4499"),
        WebsiteChannel.TMALL: Decimal("4399"),
        WebsiteChannel.OFFICIAL: Decimal("4399"),
    }
    urls = {
        WebsiteChannel.JD: "https://jd.example/product",
        WebsiteChannel.TMALL: "https://tmall.example/product",
        WebsiteChannel.OFFICIAL: "https://official.example/product",
    }
    results = tuple(
        make_business_result(
            directory,
            task,
            price=prices[task.channel],
            url=urls[task.channel],
        )
        for task in tasks
    )
    return run_web_fixture(directory, (row,), tasks, results)


def results_from_executor(
    directory: Path,
    prices: dict[WebsiteChannel, Decimal],
) -> Callable[[WebsiteTask], WebsiteResult]:
    def execute(task: WebsiteTask) -> WebsiteResult:
        return make_business_result(
            directory,
            task,
            price=prices[task.channel],
            url=f"https://{task.channel.value}.example/product",
        )

    return execute


def with_missing_evidence(result: WebsiteResult, path: Path) -> WebsiteResult:
    assert result.evidence is not None
    return replace(result, evidence=replace(result.evidence, path=path))


def _fixture(
    output: WebToExcelResult,
    tasks: Sequence[WebsiteTask],
    results: Sequence[WebsiteResult],
    observations: Sequence[WebsiteObservationCheckpoint],
) -> WebRunFixture:
    return WebRunFixture(
        quote_path=output.quote_path,
        report_path=output.report_path,
        rows=output.rows,
        tasks=tuple(tasks),
        results=tuple(results),
        observations=tuple(observations),
    )

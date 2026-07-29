from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from quote_app.core.normalization import normalize_brand
from quote_app.sites.matching import normalize_product_text
from quote_app.tasks.models import (
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteTask,
)

WebsiteExecutor = Callable[[WebsiteTask], WebsiteResult]
_COMPLETED_STATES = frozenset(
    (TaskState.SUCCEEDED, TaskState.TECHNICAL_FAILURE)
)


@dataclass(frozen=True, slots=True)
class QueryKey:
    brand: str
    channel: WebsiteChannel
    model_name: str
    ram: str
    storage: str
    color: str

    @classmethod
    def from_task(cls, task: WebsiteTask) -> QueryKey:
        if not isinstance(task, WebsiteTask):
            raise TypeError("task must be a WebsiteTask")
        brand_text = normalize_product_text(task.brand)
        return cls(
            brand=normalize_product_text(normalize_brand(brand_text)),
            channel=task.channel,
            model_name=normalize_product_text(task.model_name),
            ram=normalize_product_text(task.ram),
            storage=normalize_product_text(task.storage),
            color=normalize_product_text(task.color),
        )


@dataclass(slots=True)
class SameRunQueryCache:
    """Exact completed-result cache whose lifetime is one runner invocation."""

    _run_id: str | None = field(default=None, init=False)
    _results: dict[QueryKey, WebsiteResult] = field(
        default_factory=dict,
        init=False,
    )

    def get_or_execute(
        self,
        task: WebsiteTask,
        executor: WebsiteExecutor,
    ) -> WebsiteResult:
        if not isinstance(task, WebsiteTask):
            raise TypeError("task must be a WebsiteTask")
        if not callable(executor):
            raise TypeError("executor must be callable")
        if self._run_id is None:
            self._run_id = task.run_id
        elif task.run_id != self._run_id:
            raise ValueError("same-run query cache cannot cross run IDs")

        key = QueryKey.from_task(task)
        cached = self._results.get(key)
        if cached is not None:
            return replace(cached, task_id=task.task_id)

        result = executor(task)
        if not isinstance(result, WebsiteResult):
            raise TypeError("executor must return a WebsiteResult")
        if result.task_id != task.task_id:
            raise ValueError("executor result task_id does not match the task")
        if result.state in _COMPLETED_STATES:
            self._results[key] = result
        return result

    def __len__(self) -> int:
        return len(self._results)


def execute_with_same_run_cache(
    tasks: Sequence[WebsiteTask],
    executor: WebsiteExecutor,
) -> tuple[WebsiteResult, ...]:
    cache = SameRunQueryCache()
    return tuple(cache.get_or_execute(task, executor) for task in tasks)

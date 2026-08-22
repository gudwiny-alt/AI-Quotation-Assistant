from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

from quote_app.tasks.models import BusinessOutcome
from tests.contract.test_official_honor_live import _live_case, _live_honor_html


def test_honor_official_baseline_uses_same_page_search_then_enters_detail(
    official_case: Any,
) -> None:
    """The baseline never delegates a search transition to the site submit button."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html().replace(
            'data-action="search"',
            'data-action="search" data-requires-enter="true"',
        ),
    )

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert page.presses == []
    assert observation.url.endswith(".html")


def test_honor_official_baseline_keeps_detail_sku_and_price_verification(
    official_case: Any,
) -> None:
    """The proven detail path must still select the requested SKU before pricing."""
    adapter, task, page = _live_case(
        official_case,
        html=_live_honor_html(),
    )
    task = replace(task, ram="12GB", storage="512GB", color="天青釉")

    observation = adapter.observe(task, cast(Any, page))

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("4499.00")
    assert observation.semantic_state.capacity == "12GB+512GB"
    assert observation.semantic_state.color == "天青釉"
    assert page.option_clicks == ["honor-version", "honor-color"]

from quote_app.core.months import required_price_months, shift_month
from quote_app.domain.models import QuoteMonth


def test_august_history_months_are_march_and_july() -> None:
    six_month_start, previous = required_price_months(QuoteMonth(2026, 8))
    assert six_month_start == QuoteMonth(2026, 3)
    assert previous == QuoteMonth(2026, 7)


def test_shift_month_crosses_year_boundary() -> None:
    assert shift_month(QuoteMonth(2026, 1), -5) == QuoteMonth(2025, 8)

from datetime import date

from quote_app.domain.models import QuoteMonth


def test_quote_month_defaults_to_current_natural_month() -> None:
    month = QuoteMonth.current(today=date(2026, 8, 18))
    assert (month.year, month.month) == (2026, 8)


def test_quote_month_rejects_month_13() -> None:
    try:
        QuoteMonth(2026, 13)
    except ValueError:
        pass
    else:
        raise AssertionError("QuoteMonth accepted month 13")

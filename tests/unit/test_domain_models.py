from datetime import date

from quote_app.domain.models import QuoteMonth, QuoteRow, WebQuery


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


def test_quote_row_keeps_backward_compatible_empty_typed_web_query() -> None:
    row = QuoteRow(2, "00009101", {"C": "00009101"})

    assert row.web_query == WebQuery()
    assert row.web_query.brand is None
    assert row.web_query.model_name is None
    assert row.web_query.ram is None
    assert row.web_query.storage is None
    assert row.web_query.color is None

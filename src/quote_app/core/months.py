from quote_app.domain.models import QuoteMonth


def shift_month(value: QuoteMonth, delta: int) -> QuoteMonth:
    zero_based = value.year * 12 + value.month - 1 + delta
    year, month_index = divmod(zero_based, 12)
    return QuoteMonth(year, month_index + 1)


def required_price_months(value: QuoteMonth) -> tuple[QuoteMonth, QuoteMonth]:
    return shift_month(value, -5), shift_month(value, -1)

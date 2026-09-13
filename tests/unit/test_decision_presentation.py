from types import SimpleNamespace

from quote_app.desktop_decision import channel_presentation, money_preview


def test_technical_failure_keeps_observed_price_but_marks_collection_failed() -> None:
    presentation = channel_presentation(
        [
            SimpleNamespace(
                price="2090.0001",
                outcome="price_found",
                state="technical_failure",
                outcome_label="已获取价格",
            )
        ]
    )

    assert presentation.price == "¥2,090.0001"
    assert presentation.status == "采集失败"


def test_successful_price_collection_still_requires_review() -> None:
    presentation = channel_presentation(
        [
            SimpleNamespace(
                price="2090.0001",
                outcome="price_found",
                state="succeeded",
                outcome_label="已获取价格",
            )
        ]
    )

    assert presentation.price == "¥2,090.0001"
    assert presentation.status == "待复核"


def test_channel_without_record_is_clearly_waiting_for_price() -> None:
    presentation = channel_presentation([])

    assert presentation.price == "—"
    assert presentation.status == "待取价"


def test_money_preview_preserves_exact_decimal_precision() -> None:
    assert money_preview("2090.0001") == "¥2,090.0001"


def test_money_preview_rejects_unbounded_numeric_expansion() -> None:
    assert money_preview("1e9999999") == "1e9999999"
    assert money_preview("1e301") == "1e301"
    assert money_preview("9" * 1001) == "9" * 1001

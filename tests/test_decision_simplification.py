from decimal import Decimal
import json

from tests.test_review_support import session as fixture_session, check

import pytest


@pytest.fixture
def session(tmp_path):
    return fixture_session.__wrapped__(tmp_path)


def test_history_and_each_known_ceiling_pass_without_manual_review(session):
    product = session.products[0]
    product.context["history_prices"] = json.dumps([["2026-06", "2300"], ["2026-07", "2200"]])
    assert check(session, "E03").status == "通过"
    assert check(session, "E04").status == "通过"
    assert check(session, "E08").status == "通过"
    product.context["warehouse_price"] = "1500到2000"
    assert check(session, "E04").status == "通过"
    assert check(session, "E08").status == "未通过"


def test_missing_history_and_explicit_first_quote_are_distinct(session):
    p = session.products[0]
    p.context["first_quote_date"] = ""
    assert check(session, "E03").status == "待补充"
    p.context["first_quote_date"] = "2026-09-02"
    assert check(session, "E03").status == "不适用"
    p.context["history_prices"] = json.dumps([["2026-08", "2000"]])
    assert check(session, "E03").status == "未通过"


def test_profit_trial_distinguishes_margin_from_markup():
    from quote_app.services.review_support import profit_trial

    trial = profit_trial("4200", "4400")
    assert trial["difference"] == Decimal("200")
    assert abs(trial["margin"] - Decimal("4.545454545454545454545454545")) < Decimal("1e-25")
    assert trial["markup"] > Decimal("4.5")
    assert profit_trial("4200", "4389")["markup"] == Decimal("4.5")
    assert profit_trial("4200", "4000")["margin"] == Decimal("-5")
    for bad in ("", "0", "NaN", "1e999999"):
        assert profit_trial("4200", bad) is None
        assert profit_trial(bad, "4400") is None


def test_exact_source_identity_passes_without_manual_confirmation(session):
    assert check(session, "A02").status == "通过"
    assert not any(c.code == "A03" for c in session.evaluate(session.products[0]))
    session._source_known[session.products[0].id] = False
    assert check(session, "A02").status == "待补充"


def test_verified_external_ceiling_updates_price_guidance(session, tmp_path, monkeypatch):
    from quote_app.desktop_state import TaskRow
    from quote_app.services.screenshot_review import Recognition
    from quote_app.services.review_support import price_ceiling
    import quote_app.services.review_support as support

    p = session.products[0]
    p.title, p.specification = "荣耀Magic8", "512GB"
    p.channels = []
    for channel, column, url in (
        ("jd", "AI", "https://item.jd.com/1"),
        ("tmall", "AJ", "https://detail.tmall.com/1"),
        ("official", "AK", "https://www.honor.com/1"),
    ):
        path = tmp_path / f"{channel}.png"
        path.touch()
        p.channels.append(
            TaskRow(
                channel,
                channel=channel,
                price="1900",
                state="succeeded",
                outcome="price_found",
                evidence_path=path,
                url=url,
                material_code=p.material_code,
                source_row_number=2,
            )
        )
        session._rows[p.output_row][column] = 1900
    monkeypatch.setattr(
        support,
        "request_scan",
        lambda _: Recognition(
            "done", "荣耀官方旗舰店\n京东荣耀自营旗舰店\n荣耀Magic8 512GB\n售价¥1900", 1600, 1000
        ),
    )
    checks = session.evaluate(p)
    assert next(c for c in checks if c.code == "E09").status == "未通过"
    assert price_ceiling(p, checks)[0] == 1900
    assert "已核验" in price_ceiling(p, checks)[1]
    assert price_ceiling(p)[0] == 2090

from decimal import Decimal
import pytest
from quote_app.desktop_state import TaskRow
from quote_app.services.review_support import price_ceiling
from tests.test_review_support import session as base_session, check


@pytest.fixture
def session(tmp_path):
    return base_session.__wrapped__(tmp_path)


def test_collected_price_does_not_wait_for_image_audit(session):
    p = session.products[0]
    p.values.update(K="4389", L="6000", Q="6000")
    p.context["warehouse_price"] = "6000"
    p.channels = [TaskRow("jd", channel="jd", price="4999", outcome="price_found", state="succeeded"),
                  TaskRow("tmall", channel="tmall", price="5499", outcome="price_found", state="technical_failure")]
    assert check(session, "E09").status == "通过"
    assert "4999" in check(session, "E09").comparison
    assert any(c.status != "通过" for c in session.evaluate(p) if c.channel)
    assert price_ceiling(p)[0] == 4999
    p.values["K"] = "4999.0000001"
    assert check(session, "E09").status == "未通过"


def test_no_price_is_not_automatic_pass(session):
    p = session.products[0]
    p.channels = [TaskRow("jd", channel="jd", price="100", state="technical_failure")]
    assert check(session, "E09").status == "待补充"


@pytest.mark.parametrize("first,price,k,status,limit", [
    ("2026-04-01", "", "2090", "不适用", None),
    ("2026-03-31", "2200", "2090", "通过", Decimal("2090")),
    ("2026-03-31", "2200", "2090.000001", "未通过", Decimal("2090")),
    ("2025-09-30", "2200", "1980", "通过", Decimal("1980")),
    ("2025-09-30", "2200", "1980.000001", "未通过", Decimal("1980")),
    ("2026-01-05", "", "2090", "待补充", None),
])
def test_periodic_price_uses_first_amount_and_quote_month(session, first, price, k, status, limit):
    p = session.products[0]
    p.context.update(first_quote_date=first, first_quote_price=price, qualification_note="")
    p.values["K"] = k
    c = check(session, "E07")
    assert c.status == status
    if limit is not None:
        assert str(limit) in c.comparison
        assert price_ceiling(p)[0] <= limit
    elif status == "待补充":
        assert "首次报价金额" in c.reason


def test_first_amount_persists_without_changing_extra_excel_cells(session):
    p = session.products[0]
    p.context.update(first_quote_date="2026-01-05", first_quote_price="4459")
    session.save(p)
    from quote_app.services.review_support import ReviewSession
    reopened = ReviewSession(session.model, session.month)
    assert reopened.products[0].context["first_quote_price"] == "4459"


def test_technical_checks_are_actionable_not_placeholders(session):
    checks = session.evaluate(session.products[0])
    assert not any(c.code == "D04" for c in checks)
    assert check(session, "F04").status == "不适用"
    assert check(session, "F05").status == "通过"
    assert "SHA256" not in check(session, "F05").comparison
    assert check(session, "F05").evidence_paths == (session.quote_path,)
    # Fixture has a modified Z formula: must detect it, never blindly pass.
    assert check(session, "F03").status == "未通过"
    assert "Z2" in check(session, "F03").comparison


def test_known_formula_template_checks_automatically(session):
    from quote_app.core.formulas import formula_cells
    p = session.products[0]
    session._rows[p.output_row].update(formula_cells(p.output_row))
    assert check(session, "F03").status == "通过"
    assert "Excel" in check(session, "F03").reason


def test_decision_confirmation_is_separate_from_final_image_audit(session):
    p = session.products[0]
    session._sources[p.id].cells['J'] = 2090
    p.channels = [TaskRow('jd', channel='jd', price='2200', state='succeeded', outcome='price_found')]
    session.confirm_product(p)
    assert p.id in session._confirmed
    assert any(c.status != '通过' for c in session.evaluate(p) if c.channel)
    with pytest.raises(ValueError, match='只能导出草稿'):
        session.export_report(final=True)


def test_periodic_exception_requires_real_reason_and_attachment(session, tmp_path):
    p = session.products[0]
    p.context.update(first_quote_date='2026-01-05', first_quote_price='2000')
    p.values['K'] = '2000'
    assert check(session, 'E07').status == '未通过'
    p.values['AO'] = '厂家申请本月不降价'
    assert check(session, 'E07').status == '未通过'
    attachment = tmp_path / '厂家说明.txt'
    attachment.write_text('厂家依据')
    p.attachments = [attachment]
    c = check(session, 'E07')
    assert c.status == '待复核' and c.human_reviewable
    p.values['K'] = '1900'
    assert check(session, 'E07').status == '通过'

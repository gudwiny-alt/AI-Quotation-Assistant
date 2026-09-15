import json
import pytest
from tests.test_review_support import session as base_session
from quote_app.desktop_state import TaskRow
from quote_app.services.ai_summary import build_snapshot


@pytest.fixture
def session(tmp_path):
    return base_session.__wrapped__(tmp_path)


def test_snapshot_readonly_and_stable_with_precise_business_calculations(session):
    before = session.quote_path.read_bytes()
    p = session.products[0]
    original_channels = p.channels
    one = build_snapshot(session)
    assert build_snapshot(session) == one
    assert session.quote_path.read_bytes() == before
    assert not session.store_path.exists()
    assert p.channels is original_channels
    product = one["products"][0]
    assert product["trial"]["markup"] == "4.500"
    assert product["trial"]["difference"] == "90"
    assert product["ceiling"]["amount"] == "2090.000"
    assert product["written"] is False
    p.values["K"] = "2080"
    assert build_snapshot(session)["snapshot_id"] != one["snapshot_id"]


def test_snapshot_distinguishes_missing_prices_and_never_sends_paths(session):
    p = session.products[0]
    p.channels.append(
        TaskRow(
            "test",
            channel="jd",
            outcome="no_model",
            evidence_path=session.quote_path,
            url="https://jd.com/item?token=SECRET",
        )
    )
    snap = build_snapshot(session)
    encoded = json.dumps(snap, ensure_ascii=False)
    assert str(session.quote_path.parent) not in encoded
    assert "SECRET" not in encoded
    assert snap["coverage"]["channel_records"] == 1
    assert snap["coverage"]["price_found"] == 0
    assert snap["products"][0]["channels"][0]["price"] is None
    assert snap["products"][0]["channels"][0]["outcome"] == "no_model"


def test_snapshot_blocks_external_changes_and_running_batch(session):
    session.model.running = True
    with pytest.raises(ValueError, match="运行"):
        build_snapshot(session)
    session.model.running = False
    session.quote_path.write_bytes(session.quote_path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="变化"):
        build_snapshot(session)


def test_periodic_projection_reuses_current_rule_and_retains_conditions(session):
    p = session.products[0]
    p.context.update(first_quote_date="2026-04-01", first_quote_price="2100")
    snap = build_snapshot(session)
    product = snap["products"][0]
    assert product["periodic"]["reduction"] == "0"
    assert product["next_month"]["periodic_limit"] == "1995.00"
    assert product["next_month"]["conditions"]
    assert snap["sources"] and len(snap["checks"]) > 0


@pytest.mark.parametrize(
    "local_path",
    [
        "/Volumes/业务共享/张三/报价 图.png",
        "/home/user/private note.txt",
        r"\\server\张三\报价.png",
        r"C:\Users\张三\价格 图.png",
    ],
)
def test_free_text_omits_external_and_network_paths(session, local_path):
    session.products[0].values["AP"] = "说明：" + local_path + "；厂家无法调价"
    encoded = json.dumps(build_snapshot(session), ensure_ascii=False)
    assert "张三" not in encoded and "private note" not in encoded
    assert "厂家无法调价" in encoded

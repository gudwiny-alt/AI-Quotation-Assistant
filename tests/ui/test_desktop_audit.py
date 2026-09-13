import os
import tkinter as tk
from types import SimpleNamespace

import pytest

from quote_app.desktop_audit import build_audit_snapshot


def check(product_id, category, status, code="A01"):
    return SimpleNamespace(
        id=f"{product_id}-{code}",
        product_id=product_id,
        category=category,
        status=status,
        code=code,
    )


def product(product_id):
    return SimpleNamespace(
        id=product_id,
        title=f"商品 {product_id}",
        specification="12GB / 256GB",
        material_code=f"M-{product_id}",
    )


def test_category_filter_keeps_full_batch_summary_and_category_counts():
    """Break caught: selecting a category incorrectly shrinks the header statistics."""
    products = [product("p1"), product("p2")]
    checks = [
        check("p1", "商品分类与关联", "通过", "A01"),
        check("p1", "截图内容核验", "待复核", "C01"),
        check("p2", "商品分类与关联", "未通过", "A02"),
        check("p2", "截图内容核验", "不适用", "C02"),
        check("p2", "截图内容核验", "未检查", "C03"),
    ]

    snapshot = build_audit_snapshot(products, checks, category="商品分类与关联")

    assert snapshot.summary == {
        "products": 2,
        "checks": 5,
        "通过": 1,
        "需处理": 2,
        "未检查": 1,
        "不适用": 1,
    }
    assert snapshot.category_counts["商品分类与关联"] == {
        "通过": 1,
        "未通过": 1,
        "待复核": 0,
        "待补充": 0,
        "未检查": 0,
        "不适用": 0,
    }
    assert [row.product.id for row in snapshot.rows] == ["p1", "p2"]
    assert [item.code for item in snapshot.visible_checks("p1")] == ["A01"]


def test_exception_filter_keeps_one_product_row_and_includes_unchecked_work():
    """Break caught: unchecked items disappear from the exception queue or duplicate a product."""
    products = [product("p1"), product("p2"), product("p3")]
    checks = [
        check("p1", "商品分类与关联", "通过", "A01"),
        check("p1", "商品分类与关联", "待补充", "A02"),
        check("p1", "截图内容核验", "未通过", "C01"),
        check("p2", "商品分类与关联", "通过", "A01"),
        check("p3", "商品分类与关联", "未检查", "A01"),
    ]

    snapshot = build_audit_snapshot(products, checks, exceptions_only=True)

    assert [row.product.id for row in snapshot.rows] == ["p1", "p3"]
    first = snapshot.rows[0]
    assert first.counts["待补充"] == 1
    assert first.counts["未通过"] == 1
    assert first.overall == "需处理"
    assert snapshot.rows[1].overall == "未检查"


def test_running_empty_batch_never_claims_all_passed():
    """Break caught: an empty or running session is presented as a successful audit."""
    empty = build_audit_snapshot([], [], running=False)
    running = build_audit_snapshot([], [], running=True)

    assert empty.batch_state == "empty"
    assert running.batch_state == "running"
    assert empty.summary["通过"] == 0
    assert running.summary["通过"] == 0


@pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Needs native desktop"
)
def test_native_audit_selection_persists_and_category_filter_keeps_header_counts():
    """Break caught: rebuilding the native audit view loses selection or shrinks totals."""
    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    try:
        parent = tk.Frame(root)
        parent.pack(fill="both", expand=True)
        products = [product("p1"), product("p2")]
        checks = [
            check("p1", "商品分类与关联", "通过", "A01"),
            check("p1", "截图内容核验", "待复核", "C01"),
            check("p2", "商品分类与关联", "未通过", "A02"),
        ]
        for item in checks:
            item.title = item.code
            item.comparison = "对照依据"
            item.reason = "判断原因"
            item.evidence_paths = ()
            item.human_reviewable = item.status == "待复核"
        session = SimpleNamespace(
            products=products,
            running=False,
            all_checks=lambda: checks,
            export_report=lambda final=False: None,
        )
        workbench = SimpleNamespace(review_product_id="p2", show_page=lambda page: None)

        from quote_app.desktop_audit import AuditView

        view = AuditView(workbench, parent, session)
        root.update()
        view._choose_category("截图内容核验")
        root.update()

        assert workbench.review_product_id == "p2"
        assert view.metric_values["checks"].cget("text") == "3"
        assert view.scope_label.cget("text") == "当前范围：截图内容核验"
        total = view.metric_values['checks'].cget('text')
        checks[1].channel = 'jd'
        workbench.review_product_id = 'p1'
        view._choose_group('jd')
        root.update()
        assert [c.id for c in view._checks_by_list_index] == [checks[1].id]
        assert view.metric_values['checks'].cget('text') == total
        assert '京东' in view.product_summary.cget('text')
        view._choose_group('base')
        assert [c.id for c in view._checks_by_list_index] == [checks[0].id]
        extra = SimpleNamespace(**vars(checks[1]))
        extra.id, extra.product_id = 'p2-channel', 'p2'
        checks.append(extra)
        view._choose_category('商品分类与关联')
        view._choose_group('jd')
        view.product_list.selection_set('p2')
        view._select_product()
        assert [c.id for c in view._checks_by_list_index] == ['p2-channel']
        assert not errors
    finally:
        root.destroy()


@pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Needs native desktop"
)
def test_native_audit_read_failure_stays_unchecked_and_disables_final_export():
    """Break caught: an all_checks error is overwritten by a green no-issues state."""
    root = tk.Tk()
    try:
        parent = tk.Frame(root)
        parent.pack(fill="both", expand=True)
        session = SimpleNamespace(
            products=[product("p1")],
            running=False,
            all_checks=lambda: (_ for _ in ()).throw(OSError("报告读取失败")),
        )
        workbench = SimpleNamespace(review_product_id=None, show_page=lambda page: None)

        from quote_app.desktop_audit import AuditView

        view = AuditView(workbench, parent, session)
        root.update()

        assert "读取失败" in view.final_state.cget("text")
        assert "未检查" in view.final_state.cget("text")
        assert view.metric_values["checks"].cget("text") == "—"
        assert "结果不可用" in view.summary_extra.cget("text")
        assert view.product_list.records["p1"]["values"][2] == "未检查"
        view.exceptions_only = True
        view.refresh()
        assert view.product_list.get_children() == ("p1",)
        assert view.final_state.cget("fg") != "#0B9975"
        assert view.final_button.cget("state") == "disabled"
    finally:
        root.destroy()


@pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Needs native desktop"
)
def test_native_audit_rows_keep_failed_count_red_and_label_single_product_scope():
    """Break caught: failures merge into generic pending counts and all-check scope is ambiguous."""
    root = tk.Tk()
    try:
        parent = tk.Frame(root)
        parent.pack(fill="both", expand=True)
        products = [product("p1")]
        checks = [
            check("p1", "商品分类与关联", "未通过", "A01"),
            check("p1", "商品分类与关联", "待复核", "A02"),
        ]
        for item in checks:
            item.title = item.code
            item.comparison = "对照依据"
            item.reason = "判断原因"
            item.evidence_paths = ()
            item.human_reviewable = item.status == "待复核"
        session = SimpleNamespace(products=products, running=False, all_checks=lambda: checks)
        workbench = SimpleNamespace(review_product_id="p1", show_page=lambda page: None)

        from quote_app.desktop_audit import AuditView

        view = AuditView(workbench, parent, session)
        view._choose_category("商品分类与关联")
        view._toggle_all_checks()
        root.update()

        product_row = view.product_list.records["p1"]
        failed_check = view.check_list.records["p1-A01"]
        assert product_row["values"][1] == "0过  1未  1待"
        assert product_row["badges"][2] == "red"
        assert failed_check["badges"][2] == "red"
        assert view.product_summary.cget("text").startswith("单品范围：全部检查")
    finally:
        root.destroy()

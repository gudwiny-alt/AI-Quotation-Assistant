"""Isolated post-generation native QA: synthetic workbook, no collection/network."""

from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import tempfile
import tkinter as tk

from openpyxl import load_workbook

from quote_app.desktop_state import TaskRow
from quote_app.desktop_ui import DesktopWorkbench
from quote_app.domain.models import QuoteRow, WebQuery
from quote_app.paths import build_app_paths


def main():
    root = tk.Tk()
    home = Path(tempfile.mkdtemp(prefix="review-native-qa-"))
    app = SimpleNamespace(root=root, app_paths=build_app_paths("Darwin", home=home))
    for name in ("base", "marketing", "bop", "output_dir", "year", "month", "brand_mode"):
        setattr(
            app,
            name + "_var",
            tk.StringVar(
                root,
                value={
                    "year": "2026",
                    "month": "9",
                    "brand_mode": "全品牌",
                    "output_dir": str(home),
                }.get(name, ""),
            ),
        )

    def forbidden(*_args):
        raise AssertionError("Visual fixture must never run business actions")

    for name in (
        "run",
        "continue_current_task",
        "cancel_manual_action",
        "open_output_directory",
        "open_login_browser",
        "check_readiness",
        "_choose_file",
        "_choose_directory",
    ):
        setattr(app, name, forbidden)
    errors = []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    view = DesktopWorkbench(
        app, title="报价与稽核 · 隔离布局测试", credit="仅供原生界面校验", modes=("全品牌",)
    )
    view.root.geometry("1440x1040+30+50")
    book = load_workbook("resources/templates/quote_template.xlsx")
    sheet = book["5G手机"]
    sheet["K1"] = "2026年9月结算报价（元/台）"
    products = (
        ("Redmi K70 5G", "小米", 2199),
        ("Redmi Note 13 5G", "小米", 1199),
        ("华为畅享 90 Pro Max", "华为", 2299),
        ("荣耀 Magic8", "荣耀", 5499),
    )
    quotes, tasks = [], []
    for i, (title, brand, price) in enumerate(products, 2):
        cells = {
            "C": f"QA-{i}",
            "D": title,
            "E": title,
            "F": "12GB / 256GB / 墨羽",
            "O": price + 200,
            "J": price - 100,
            "AK": price + 200,
            "AJ": price + 100,
            "AI": price,
            "K": price - 99,
            "L": 2000 if i == 2 else price - 180,
            "M": price,
            "P": price - 100,
            "Q": price + 400,
        }
        for key, value in cells.items():
            sheet[f"{key}{i}"] = value
        sheet[f"Z{i}"] = f"=K{i}/L{i}-1"
        quotes.append(
            QuoteRow(i, f"QA-{i}", cells, web_query=WebQuery(brand, title, "12GB", "256GB", "墨羽"))
        )
        for channel, delta in (("jd", 0), ("tmall", 100), ("official", 200)):
            tasks.append(
                TaskRow(
                    f"qa-{i}-{channel}",
                    title,
                    channel,
                    str(price + delta),
                    "price_found",
                    "succeeded",
                    "pending",
                    specification="12GB / 256GB / 墨羽",
                    source_row_number=i,
                    material_code=f"QA-{i}",
                )
            )
    path = home / "2026年9月报价_布局测试.xlsx"
    book.save(path)
    view.model.quote_path = path
    view.model.quote_rows = tuple(quotes)
    view.model.rows = tasks
    view.model.summary = "隔离测试数据 · 非业务报价结果"
    view.show_page("decision")
    session = view._review_session
    for i, product in enumerate(session.products):
        product.context.update(
            category="手机",
            stock="在库",
            entry_date="2026-03-27" if i != 1 else "2025-03-01",
            first_quote_date="2026-07-01",
            qualification_note="原生布局测试输入，非真实业务依据",
        )
    output = Path("/tmp/quotation-review-qa")
    output.mkdir(exist_ok=True)
    print(str(output), flush=True)

    def capture(name):
        root.update()
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        match = next(
            (w for w in windows if w.get("kCGWindowName") == "报价与稽核 · 隔离布局测试"), None
        )
        if match:
            subprocess.run(
                ["screencapture", "-x", "-o", "-l", str(match["kCGWindowNumber"]), str(output / name)],
                check=True,
            )
        else:
            errors.append("QA window not found for screenshot")

    def decision():
        view._review_view.select(session.products[0].id)
        view.subheading.configure(text="布局测试数据 · 报价决策智能辅助")
        root.after(700, lambda: capture("01-decision.png"))
        root.after(1600, youfu)

    def youfu():
        view._review_view.select(session.products[1].id)
        root.after(700, lambda: capture("02-youfu.png"))
        root.after(1600, audit)

    def audit():
        view.show_page("audit")
        view.subheading.configure(text="布局测试数据 · 六类全批次稽核与单品联动")
        root.after(700, lambda: capture("03-audit.png"))
        root.after(1600, finish)

    def finish():
        (output / "result.json").write_text(
            json.dumps({"errors": errors, "workbook": str(path)}, ensure_ascii=False)
        )
        root.destroy()

    root.after(500, decision)
    root.mainloop()


if __name__ == "__main__":
    main()

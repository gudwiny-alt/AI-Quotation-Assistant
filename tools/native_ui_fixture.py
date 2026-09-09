"""Isolated native visual QA. Synthetic data is visibly marked; no business work runs."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import tkinter as tk
from tkinter import messagebox

from quote_app.desktop_ui import DesktopWorkbench
from quote_app.desktop_state import TaskRow
from quote_app.domain.models import QuoteRow, WebQuery, Issue
from quote_app.paths import build_app_paths


class FixtureWorkbench(DesktopWorkbench):
    def show_page(self, page):
        super().show_page(page)
        self.subheading.configure(text="布局测试数据 · " + self.subheading.cget("text"))


def main():
    root = tk.Tk()
    fixture_home = Path(tempfile.mkdtemp(prefix="quotation-visual-fixture-"))
    app = SimpleNamespace(root=root, app_paths=build_app_paths("Darwin", home=fixture_home))
    for name in ("base", "marketing", "bop", "output_dir", "year", "month", "brand_mode"):
        value = {
            "year": "2026",
            "month": "9",
            "brand_mode": "全品牌",
            "output_dir": str(fixture_home),
        }.get(name, "")
        setattr(app, name + "_var", tk.StringVar(root, value=value))

    def no_business(*args):
        messagebox.showinfo(
            "布局测试", "这是隔离的界面测试窗口，不会执行业务或连接网站。", parent=root
        )

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
        setattr(app, name, no_business)
    view = FixtureWorkbench(
        app,
        title="布局测试数据 · 非业务运行",
        credit="仅用于原生界面布局校验",
        modes=("全品牌", "荣耀"),
    )
    view.begin_run()
    products = (
        ("华为畅享 90 Pro Max", "华为", "256GB", "曜金黑", 2199),
        ("荣耀 500 Pro", "荣耀", "256GB", "月光银", 2599),
        ("vivo X300", "vivo", "512GB", "星辰蓝", 3999),
        ("OPPO Reno 15", "OPPO", "256GB", "流光白", 2499),
        ("小米 16", "小米", "512GB", "远山青", 4299),
        ("iPhone 17", "苹果", "256GB", "黑色", 5499),
    )
    rows, quotes = [], []
    for index, (model, brand, storage, color, price) in enumerate(products):
        for ci, channel in enumerate(("official", "tmall", "jd")):
            complete = index < 4
            rows.append(
                TaskRow(
                    f"fixture-{index}-{channel}",
                    model,
                    channel,
                    str(price + ci * 50) if index != 5 else "",
                    "price_found" if index != 5 else "",
                    "succeeded" if complete else "waiting_for_login" if index == 5 else "running",
                    "complete" if complete else "pending",
                    specification=f"{storage} / {color}",
                    error="CAPTURE_PERMISSION" if index == 4 and channel == "jd" else "",
                )
            )
        quotes.append(
            QuoteRow(
                index + 2,
                f"TEST-{index + 1:04d}",
                cells={"AK": price, "AJ": price + 50, "AI": price + 100, "AH": price, "S": "官网"},
                web_query=WebQuery(brand=brand, model_name=model, storage=storage, color=color),
                issues=[Issue("CAPTURE_PERMISSION", "测试异常：截图待补，已保存阶段价格", False)]
                if index == 4
                else [],
            )
        )
    view.model.rows = rows
    view.model.quote_rows = tuple(quotes)
    view.model.running = False
    view.model.summary = "布局测试数据 · 仅用于检查界面，不代表业务运行结果"
    view.append_log("布局测试：官网阶段价格与截图状态示例")
    view.append_log("布局测试：天猫任务等待登录验证")
    view.append_log("布局测试：京东部分渠道截图待补")
    view.show_overview_tab("tasks")
    root.mainloop()


if __name__ == "__main__":
    main()

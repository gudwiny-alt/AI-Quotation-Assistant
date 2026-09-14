"""Shared real-screenshot replacement flow for decision makers and auditors."""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
from .desktop_controls import SoftSelect, SoftEntry, SoftButton


def open_evidence_editor(parent, session, product, on_saved, *, channel="official"):
    if session.running:
        messagebox.showinfo("请稍候", "任务运行中，请等待采集结束后补充截图。", parent=parent)
        return None
    dialog = tk.Toplevel(parent)
    dialog.title("补充修正渠道取价")
    dialog.transient(parent.winfo_toplevel())
    dialog.configure(bg="white")
    dialog.resizable(False, False)
    shell = tk.Frame(dialog, bg="white", padx=20, pady=16)
    shell.pack(fill="both", expand=True)

    def label(text, **kwargs):
        return tk.Label(
            shell, text=text, bg="white", fg="#172849", font=("PingFang SC", -15), **kwargs
        )

    label(product.title + " · " + product.specification, wraplength=620, justify="left").pack(
        anchor="w"
    )
    labels = {"官网": "official", "天猫": "tmall", "京东": "jd"}
    selected = tk.StringVar(dialog, next((k for k, v in labels.items() if v == channel), "官网"))
    row = tk.Frame(shell, bg="white")
    row.pack(fill="x", pady=10)
    tk.Label(row, text="修正渠道", bg="white", fg="#172849").pack(side="left", padx=(0, 10))
    SoftSelect(row, textvariable=selected, values=tuple(labels), width=140).pack(side="left")
    file_label = label("请选择真实商品页面截图（PNG / JPEG）", wraplength=620)
    file_label.pack(anchor="w")
    preview = tk.Label(shell, bg="#F4F8FD", width=620, height=270)
    # Explicit frame avoids text-unit dimensions before the first image is loaded.
    frame = tk.Frame(shell, bg="#F4F8FD", width=620, height=270)
    frame.pack(pady=8)
    frame.pack_propagate(False)
    preview.destroy()
    preview = tk.Label(frame, text="原始截图预览", bg="#F4F8FD", fg="#7385A4")
    preview.pack(fill="both", expand=True)
    state = {"path": None, "image": None}
    old = next((t for t in product.channels if t.channel == channel), None)
    price = tk.StringVar(dialog, old.price if old else "")
    url = tk.StringVar(dialog, old.url if old else "")
    for title, variable in [("渠道价格（元）", price), ("商品来源链接", url)]:
        label(title).pack(anchor="w")
        SoftEntry(shell, textvariable=variable, width=620).pack(fill="x", pady=(3, 7))
    if old and old.evidence_path and old.evidence_path.is_file():
        state["path"] = old.evidence_path
        file_label.configure(text=old.evidence_path.name + "（已有截图，可替换）")

    def switch_channel(*_):
        record = next((t for t in product.channels if t.channel == labels[selected.get()]), None)
        price.set(record.price if record else "")
        url.set(record.url if record else "")
        state["path"] = (
            record.evidence_path
            if record and record.evidence_path and record.evidence_path.is_file()
            else None
        )
        preview.configure(image="", text="请选择商品截图")
        state["image"] = None
        if state["path"]:
            try:
                show_image(state["path"])
            except (ValueError, OSError):
                state["path"] = None
        file_label.configure(
            text=state["path"].name if state["path"] else "请选择真实商品页面截图（PNG / JPEG）"
        )
        save_button.configure(state="normal" if state["path"] else "disabled")

    selected.trace_add("write", switch_channel)
    reason = tk.StringVar(dialog, "自动采集异常，人工核对后补充")
    label("处理说明").pack(anchor="w")
    SoftEntry(shell, textvariable=reason, width=620).pack(fill="x", pady=(4, 8))
    label(
        "核对截图中的机型、规格及价格后填写。来源链接请粘贴商品页面完整网址。\n保存后同步更新截图、价格、链接和最低价，并重新检查相关规则。",
        wraplength=620,
        justify="left",
    ).pack(anchor="w")
    actions = tk.Frame(shell, bg="white")
    actions.pack(fill="x", pady=(14, 0))

    def show_image(path):
        from .services.review_evidence import image_bytes

        image_bytes(path)
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((620, 270), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image, master=dialog)
        state.update(path=Path(path), image=photo)
        preview.configure(image=photo, text="")
        file_label.configure(text=Path(path).name)
        save_button.configure(state="normal")

    def choose():
        name = filedialog.askopenfilename(
            parent=dialog, title="选择真实商品页面截图", filetypes=[("截图", "*.png *.jpg *.jpeg")]
        )
        if name:
            try:
                show_image(name)
            except (ValueError, OSError) as error:
                messagebox.showerror("截图不可用", str(error), parent=dialog)

    def save():
        if state["path"] is None:
            return
        try:
            session.repair_channel(
                product, labels[selected.get()], state["path"], price.get(), url.get(), reason.get()
            )
        except (ValueError, OSError) as error:
            messagebox.showerror("渠道取价未保存", str(error), parent=dialog)
            return
        dialog.destroy()
        on_saved()
        messagebox.showinfo(
            "渠道取价已更新",
            "截图、渠道价格和来源链接已写入报价表，最低价已更新。正在重新稽核，请重新确认受影响的商品报价。",
            parent=parent,
        )

    SoftButton(actions, text="选择截图", command=choose).pack(side="left")
    save_button = SoftButton(actions, text="确认商品与渠道并保存", command=save, primary=True)
    save_button.pack(side="right")
    save_button.configure(state="normal" if state["path"] else "disabled")
    if state["path"]:
        try:
            show_image(state["path"])
        except (ValueError, OSError):
            state["path"] = None
            save_button.configure(state="disabled")
    dialog.grab_set()
    return dialog

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
    dialog.title("补充／替换截图")
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
    tk.Label(row, text="替换渠道", bg="white", fg="#172849").pack(side="left", padx=(0, 10))
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
    reason = tk.StringVar(dialog, "补充清晰、无遮挡的商品页面截图")
    label("处理说明").pack(anchor="w")
    SoftEntry(shell, textvariable=reason, width=620).pack(fill="x", pady=(4, 8))
    label(
        "确认商品与渠道后保存。原图保留，新图同步写入报价表并重新稽核。\n本操作不改价格；新图价格不一致时，请重新取价并试算。",
        wraplength=620,
        justify="left",
    ).pack(anchor="w")
    actions = tk.Frame(shell, bg="white")
    actions.pack(fill="x", pady=(14, 0))

    def choose():
        name = filedialog.askopenfilename(
            parent=dialog, title="选择真实商品页面截图", filetypes=[("截图", "*.png *.jpg *.jpeg")]
        )
        if not name:
            return
        try:
            from .services.review_evidence import image_bytes

            image_bytes(name)
            with Image.open(name) as source:
                image = source.convert("RGB")
                image.thumbnail((620, 270), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=dialog)
            state.update(path=Path(name), image=photo)
            preview.configure(image=photo, text="")
            file_label.configure(text=Path(name).name)
            save_button.configure(state="normal")
        except (ValueError, OSError) as error:
            messagebox.showerror("截图不可用", str(error), parent=dialog)

    def save():
        if state["path"] is None:
            return
        try:
            session.replace_evidence(product, labels[selected.get()], state["path"], reason.get())
        except (ValueError, OSError) as error:
            messagebox.showerror("截图未保存", str(error), parent=dialog)
            return
        dialog.destroy()
        on_saved()
        messagebox.showinfo(
            "截图已更新",
            "新截图已写入对应商品和渠道，原图已保留。正在重新检查截图内容；如价格不同，请按检查提示重新取价并试算。",
            parent=parent,
        )

    SoftButton(actions, text="选择截图", command=choose).pack(side="left")
    save_button = SoftButton(actions, text="确认商品与渠道并保存", command=save, primary=True)
    save_button.pack(side="right")
    save_button.configure(state="disabled")
    dialog.grab_set()
    return dialog

"""Blue-white form controls retaining native text editing and existing Tk variables."""

from __future__ import annotations

from datetime import date
import tkinter as tk
from tkinter import scrolledtext

from quote_app.desktop_widgets import (
    Artwork,
    BLUE,
    FONT,
    INK,
    LINE,
    MUTED,
    PALE,
    WHITE,
    SlimScrollbar,
    SoftButton,
    rounded,
)


class SoftScrolledText(scrolledtext.ScrolledText):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self.frame.configure(bg=kwargs.get("bg", WHITE))
        self.vbar.destroy()
        self.vbar = SlimScrollbar(self.frame, orient="vertical", command=self.yview)
        self.vbar.pack(side="right", fill="y")
        self.configure(yscrollcommand=self.vbar.set)


class SoftEntry(tk.Frame):
    """Rounded field with a real Entry inside, preserving IME and clipboard support."""

    def __init__(self, parent, *, textvariable, placeholder="", icon=None, width=300):
        super().__init__(parent, bg=parent.cget("bg"), width=width, height=42)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.variable = textvariable
        self.border = tk.Canvas(self, bg=self.cget("bg"), highlightthickness=0)
        self.border._decorative_card = True
        self.border.place(x=0, y=0, relwidth=1, relheight=1)
        self.picture = icon
        left = 40 if icon else 13
        self.entry = tk.Entry(
            self,
            textvariable=textvariable,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=WHITE,
            fg=INK,
            insertbackground=BLUE,
            selectbackground=PALE,
            selectforeground=INK,
            font=(FONT, 12),
        )
        self.entry.place(x=left, y=8, relwidth=1, width=-left - 13, height=26)
        self.hint = tk.Label(
            self, text=placeholder, bg=WHITE, fg=MUTED, font=(FONT, 11), anchor="w"
        )
        self.hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.bind("<Configure>", self._draw)
        self.entry.bind("<FocusIn>", self._draw, add="+")
        self.entry.bind("<FocusOut>", self._draw, add="+")
        self.entry.bind(
            "<Command-a>" if self.tk.call("tk", "windowingsystem") == "aqua" else "<Control-a>",
            self._select_all,
        )
        self.trace = self.variable.trace_add("write", self._draw)
        self.bind("<Destroy>", self._destroy, add="+")
        self._draw()

    def _select_all(self, _event):
        self.entry.selection_range(0, "end")
        self.entry.icursor("end")
        return "break"

    def _destroy(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self.trace)

    def _draw(self, *_args):
        focused = self.focus_get() is self.entry
        self.border.delete("all")
        rounded(
            self.border,
            1,
            1,
            max(2, self.winfo_width() - 1),
            41,
            fill=WHITE,
            outline=BLUE if focused else LINE,
            radius=8,
        )
        if self.picture:
            self.border.create_image(22, 21, image=self.picture)
        if not self.variable.get() and not focused and self.hint.cget("text"):
            left = 40 if self.picture else 13
            self.hint.place(x=left, y=8, relwidth=1, width=-left - 13, height=26)
            self.hint.lift()
        else:
            self.hint.place_forget()


class SoftSelect(SoftButton):
    """Read-only selector with a styled, keyboard-operable local popup."""

    def __init__(self, parent, *, textvariable, values, state="readonly", width=180, icon=None):
        self.variable, self.values = textvariable, tuple(values)
        self.control_width = width
        self.popup = None
        self._destroying = False
        self._outside_binding = None
        self._focus_check = None
        self.leading = icon
        self.art = Artwork(parent.winfo_toplevel())
        self.chevron = self.art.get("ui-icons/chevron-down-muted", 16)
        super().__init__(parent, text=textvariable.get(), command=self.open_popup, state=state)
        self.bind("<Down>", lambda e: self.open_popup())
        self.bind("<Escape>", lambda e: self.close_popup())
        self.bind("<FocusOut>", self._defer_focus_check, add="+")
        self.trace = self.variable.trace_add("write", self._sync)
        self.bind("<Destroy>", self._destroy, add="+")

    def _measure(self):
        super()._measure()
        tk.Canvas.configure(self, width=self.control_width, height=42)

    def _sync(self, *_args):
        self.configure(text=self.variable.get())

    def _draw(self, _event=None):
        if self._destroying:
            return
        self.delete("all")
        w = self.winfo_width()
        disabled = self.options["state"] == "disabled"
        rounded(
            self,
            1,
            1,
            max(2, w - 1),
            41,
            fill="#F5F7FA" if disabled else WHITE,
            outline=BLUE if self.focus_get() is self or self.popup else LINE,
            radius=8,
        )
        x = 14
        if self.leading:
            self.create_image(x, 21, image=self.leading, anchor="w")
            x += self.leading.width() + 8
        text = self.options["text"]
        while text and self._font.measure(text) > w - x - 36:
            text = text[:-1]
        if text != self.options["text"]:
            text = text[:-1] + "…" if text else ""
        self.create_text(
            x, 21, text=text, anchor="w", font=self._font, fill=MUTED if disabled else INK
        )
        if self.chevron:
            self.create_image(w - 19, 21, image=self.chevron)

    def _make_popup(self):
        top = self.winfo_toplevel()
        previous = getattr(top, "_form_popup_owner", None)
        if previous and previous is not self:
            previous.close_popup(restore=False)
        top._form_popup_owner = self
        self.popup = tk.Toplevel(top)
        self.popup.withdraw()
        self.popup.overrideredirect(True)
        self.popup.transient(top)
        self.popup.configure(bg=LINE, padx=1, pady=1)
        self.popup.bind("<Escape>", lambda e: self.close_popup())
        self.popup.bind("<FocusOut>", self._defer_focus_check)
        self._outside_binding = top.bind("<ButtonPress-1>", self._outside, add="+")
        return self.popup

    def _present_popup(self, width, height, focus):
        popup = self.popup
        x = max(0, min(self.winfo_rootx(), self.winfo_screenwidth() - width))
        y = self.winfo_rooty() + self.winfo_height() + 5
        if y + height > self.winfo_screenheight() - 10:
            y = max(0, self.winfo_rooty() - height - 5)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.deiconify()
        popup.lift()
        focus.focus_set()
        self._draw()

    def open_popup(self):
        if self.options["state"] == "disabled":
            return
        if self.popup:
            self.close_popup()
            return
        popup = self._make_popup()
        body = tk.Frame(popup, bg=WHITE, padx=7, pady=7)
        body.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            body,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=WHITE,
            fg=INK,
            selectbackground=PALE,
            selectforeground=BLUE,
            exportselection=False,
            font=(FONT, 11),
            height=min(8, max(1, len(self.values))),
            selectborderwidth=3,
            activestyle="none",
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        for value in self.values:
            self.listbox.insert("end", value)
        if self.variable.get() in self.values:
            index = self.values.index(self.variable.get())
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.listbox.see(index)
        if len(self.values) > 8:
            bar = SlimScrollbar(body, command=self.listbox.yview)
            bar.pack(side="right", fill="y")
            self.listbox.configure(yscrollcommand=bar.set)
        self.listbox.bind("<ButtonRelease-1>", self._clicked_option)
        self.listbox.bind("<Return>", self._keyboard_option)
        self.listbox.bind("<space>", self._keyboard_option)
        # Let Tk measure the configured rows, font and selection padding together.
        popup.update_idletasks()
        self._present_popup(max(self.winfo_width(), 160), popup.winfo_reqheight(), self.listbox)

    def _clicked_option(self, event):
        index = self.listbox.nearest(event.y)
        bounds = self.listbox.bbox(index)
        if bounds and 0 <= event.y < self.listbox.winfo_height():
            padding = int(self.listbox.cget("selectborderwidth"))
            _, y, _, height = bounds
            if y - padding <= event.y < y + height + padding:
                self.choose(self.values[index])

    def _keyboard_option(self, _event):
        selection = self.listbox.curselection()
        if selection:
            self.choose(self.values[selection[0]])
        return "break"

    def choose(self, value):
        if value not in self.values:
            return
        self.variable.set(value)
        self.close_popup()
        self.event_generate("<<ComboboxSelected>>", when="tail")

    def _inside(self, widget, ancestor):
        while widget is not None:
            if widget is ancestor:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _outside(self, event):
        if not self._inside(event.widget, self) and not self._inside(event.widget, self.popup):
            self.close_popup(restore=False)

    def _defer_focus_check(self, _event):
        if self._destroying or not self.popup:
            return
        if self._focus_check:
            self.after_cancel(self._focus_check)
        self._focus_check = self.after_idle(self._check_focus)

    def _check_focus(self):
        self._focus_check = None
        focus = self.focus_get()
        if self.popup and focus is not self and not self._inside(focus, self.popup):
            self.close_popup(restore=False)

    def close_popup(self, restore=True):
        top = self.master.winfo_toplevel()
        if getattr(top, "_form_popup_owner", None) is self:
            top._form_popup_owner = None
        if self._focus_check:
            self.after_cancel(self._focus_check)
            self._focus_check = None
        if self._outside_binding:
            top.unbind("<ButtonPress-1>", self._outside_binding)
            self._outside_binding = None
        if self.popup:
            popup, self.popup = self.popup, None
            popup.destroy()
            if restore and self.winfo_exists():
                # Closing a borderless popup on macOS can deactivate its parent window.
                # Restore only for explicit selection/cancel, never external focus loss.
                self.focus_force()
        self._draw()

    def _destroy(self, event):
        if event.widget is self:
            self._destroying = True
            self.close_popup(restore=False)
            self.variable.trace_remove("write", self.trace)


class MonthPicker(SoftSelect):
    """One visible year/month field; edits remain drafts until a month is chosen."""

    def __init__(self, parent, *, yearvariable, monthvariable):
        self.yearvariable, self.monthvariable = yearvariable, monthvariable
        display = tk.StringVar(parent, self._display())
        art = Artwork(parent.winfo_toplevel())
        super().__init__(
            parent,
            textvariable=display,
            values=(),
            width=190,
            icon=art.get("ui-icons/calendar-blue", 20),
        )
        self._year_trace = yearvariable.trace_add("write", self._update_display)
        self._month_trace = monthvariable.trace_add("write", self._update_display)
        self.bind("<Destroy>", self._remove_date_traces, add="+")

    def _display(self):
        return f"{self.yearvariable.get()}年{self.monthvariable.get()}月"

    def _update_display(self, *_args):
        self.variable.set(self._display())

    def _remove_date_traces(self, event):
        if event.widget is self:
            self.yearvariable.trace_remove("write", self._year_trace)
            self.monthvariable.trace_remove("write", self._month_trace)

    def open_popup(self):
        if self.options["state"] == "disabled":
            return
        if self.popup:
            self.close_popup()
            return
        popup = self._make_popup()
        body = tk.Frame(popup, bg=WHITE, padx=14, pady=12)
        body.pack(fill="both", expand=True)
        self.draft_year = tk.StringVar(popup, self.yearvariable.get())
        row = tk.Frame(body, bg=WHITE)
        row.pack(fill="x")
        tk.Label(row, text="年份", bg=WHITE, fg=MUTED, font=(FONT, 11)).pack(
            side="left", padx=(0, 10)
        )
        self.year_entry = SoftEntry(row, textvariable=self.draft_year, width=135)
        self.year_entry.pack(side="left")
        tk.Label(body, text="选择月份", bg=WHITE, fg=MUTED, font=(FONT, 10)).pack(
            anchor="w", pady=(12, 7)
        )
        grid = tk.Frame(body, bg=WHITE)
        grid.pack(fill="x")
        for month in range(1, 13):
            col = (month - 1) % 4
            grid.columnconfigure(col, weight=1, uniform="months")
            selected = str(month) == self.monthvariable.get()
            control = SoftButton(
                grid,
                text=f"{month}月",
                command=lambda m=month: self.choose_month(m),
                primary=selected,
                padding=(9, 7),
            )
            control.grid(row=(month - 1) // 4, column=col, sticky="ew", padx=3, pady=3)
        self.error = tk.Label(body, text="", bg=WHITE, fg="#C77912", font=(FONT, 10))
        self.error.pack(anchor="w", pady=(6, 0))
        self._present_popup(300, 270, self.year_entry.entry)
        self.year_entry.entry.bind(
            "<Return>",
            lambda e: self.choose_month(
                int(self.monthvariable.get())
                if self.monthvariable.get().isdigit()
                else date.today().month
            ),
        )

    def choose_month(self, month):
        year = self.draft_year.get().strip()
        if not year.isascii() or not year.isdigit() or not 1 <= int(year) <= 9999:
            self.error.configure(text="请输入 1–9999 之间的年份")
            return
        if not 1 <= month <= 12:
            return
        self.yearvariable.set(str(int(year)))
        self.monthvariable.set(str(month))
        self.close_popup()
        self.event_generate("<<ComboboxSelected>>", when="tail")


class DateSelect(tk.Frame):
    """Three coordinated native-styled selects backed by one optional ISO date."""
    def __init__(self, parent, *, textvariable):
        from calendar import monthrange
        self._monthrange = monthrange
        super().__init__(parent, bg=parent.cget('bg'), borderwidth=0)
        self.variable = textvariable
        self.year, self.month, self.day = (tk.StringVar(self, p) for p in ('年', '月', '日'))
        self._changing = False
        self.controls = []
        for col, (var, values, width) in enumerate(((self.year, tuple(str(n) for n in range(date.today().year, 1899, -1)), 95),
                (self.month, tuple(str(n) for n in range(1, 13)), 75),
                (self.day, tuple(str(n) for n in range(1, 32)), 75))):
            self.columnconfigure(col, weight=1)
            control = SoftSelect(self, textvariable=var, values=values, width=width)
            control.grid(row=0, column=col, sticky='ew', padx=(0, 5 if col < 2 else 0))
            self.controls.append(control)
        self.day_control = self.controls[2]
        self._traces = [(var, var.trace_add('write', self._selected)) for var in (self.year, self.month, self.day)]
        self._traces.append((self.variable, self.variable.trace_add('write', self._external)))
        self.bind('<Destroy>', self._cleanup, add='+')
        self._external()

    @property
    def incomplete(self):
        return not self.variable.get() and any(v.get().isdigit() for v in (self.year, self.month, self.day))

    def _cleanup(self, event):
        if event.widget is self:
            for var, token in self._traces:
                var.trace_remove('write', token)

    def _external(self, *_):
        if self._changing:
            return
        self._changing = True
        try:
            try:
                value = date.fromisoformat(self.variable.get())
                parts = tuple(str(n) for n in (value.year, value.month, value.day))
            except ValueError:
                parts = ('年', '月', '日')
            for var, part in zip((self.year, self.month, self.day), parts):
                var.set(part)
            self._days()
        finally:
            self._changing = False

    def _days(self):
        count = self._monthrange(int(self.year.get()), int(self.month.get()))[1] if self.year.get().isdigit() and self.month.get().isdigit() else 31
        self.day_control.values = tuple(str(n) for n in range(1, count + 1))
        if self.day.get().isdigit() and int(self.day.get()) > count:
            self.day.set(str(count))

    def _selected(self, *_):
        if self._changing:
            return
        self._changing = True
        try:
            self._days()
            parts = [v.get() for v in (self.year, self.month, self.day)]
            self.variable.set(date(*(int(p) for p in parts)).isoformat() if all(p.isdigit() for p in parts) else '')
        finally:
            self._changing = False

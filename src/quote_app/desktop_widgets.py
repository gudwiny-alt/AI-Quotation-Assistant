"""Local artwork and native display components; no collection or pricing logic."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import sys
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from quote_app.resources import bundled_resource_path
from quote_app.desktop_scrolling import bind_scrolling, scroll_canvas, wheel_pixels

FONT = ".AppleSystemUIFont" if sys.platform == "darwin" else "Microsoft YaHei UI"
INK, MUTED, BLUE = "#132443", "#72829D", "#2468F5"
LINE, PALE, WHITE = "#DFE7F3", "#EAF1FF", "#FFFFFF"
TONES = {
    "green": ("#E6F8F1", "#0B9975"),
    "orange": ("#FFF3E3", "#C77912"),
    "blue": ("#E9F1FF", BLUE),
    "muted": ("#F0F4F9", MUTED),
}


def rounded(canvas, x1, y1, x2, y2, *, fill, outline="", radius=8, **kw):
    """Rounded UI surface, not an icon or an image replacement."""
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    return canvas.create_polygon(
        x1 + r,
        y1,
        x2 - r,
        y1,
        x2,
        y1,
        x2,
        y1 + r,
        x2,
        y2 - r,
        x2,
        y2,
        x2 - r,
        y2,
        x1 + r,
        y2,
        x1,
        y2,
        x1,
        y2 - r,
        x1,
        y1 + r,
        x1,
        y1,
        smooth=True,
        splinesteps=20,
        fill=fill,
        outline=outline,
        **kw,
    )


def brand_key(name: str) -> str | None:
    name = name.casefold()
    for key, aliases in (
        ("huawei", ("华为", "huawei")),
        ("honor", ("荣耀", "honor")),
        ("oppo", ("oppo",)),
        ("vivo", ("vivo",)),
        ("xiaomi", ("小米", "xiaomi", "redmi", "红米")),
        ("apple", ("苹果", "apple", "iphone")),
    ):
        if any(alias in name for alias in aliases):
            return key
    return None


class Artwork:
    """Decode once per display size, retain Tk images for this window only."""

    def __init__(self, root):
        self.root = root
        self.cache = {}

    def get(self, name, size):
        key = (name, size)
        if key not in self.cache:
            path = bundled_resource_path("assets/" + name + ".png")
            try:
                with Image.open(path) as source:
                    source = source.convert("RGBA")
                    source.thumbnail((size, size), Image.Resampling.LANCZOS)
                    self.cache[key] = ImageTk.PhotoImage(source, master=self.root)
            except (OSError, tk.TclError):
                self.cache[key] = None
        return self.cache[key]

    def channel(self, channel, model="", size=24):
        key = brand_key(model) if channel == "official" else channel
        return self.get("ui-brands/" + key, size) if key else self.get("ui-icons/globe-blue", size)

    def phone(self, size=48):
        return self.get("ui-media/phone-illustration", size)


class SoftButton(tk.Canvas):
    """Rounded keyboard-operable button with the controller's existing configure API."""

    def __init__(self, parent, *, text, command, primary=False, **kwargs):
        self.options = dict(
            text=text,
            command=command,
            state=kwargs.pop("state", "normal"),
            style="Primary.TButton" if primary else "Workbench.TButton",
            padding=kwargs.pop("padding", (14, 8)),
            image=kwargs.pop("image", ""),
        )
        self.hover = False
        self._font = tkfont.Font(
            root=parent, family=FONT, size=11, weight="bold" if primary else "normal"
        )
        super().__init__(
            parent,
            bg=parent.cget("bg"),
            highlightthickness=0,
            borderwidth=0,
            takefocus=True,
            **kwargs,
        )
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self.focus_set())
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<space>", lambda e: self.invoke())
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)
        self._measure()

    def _measure(self):
        padding = self.options["padding"]
        px, py = (padding, padding) if isinstance(padding, int) else padding[:2]
        self._font.configure(
            weight="bold" if self.options["style"] == "Primary.TButton" else "normal"
        )
        image = self.options["image"]
        iw = image.width() + 8 if hasattr(image, "width") else 0
        super().configure(
            width=self._font.measure(self.options["text"]) + 2 * px + iw + 4,
            height=max(30, self._font.metrics("linespace") + 2 * py),
        )
        self._draw()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        own = {key: kwargs.pop(key) for key in tuple(kwargs) if key in self.options}
        self.options.update(own)
        if kwargs:
            super().configure(**kwargs)
        if own:
            self._measure()

    config = configure

    def cget(self, key):
        return self.options[key] if key in self.options else super().cget(key)

    __getitem__ = cget

    def invoke(self):
        if self.options["state"] != "disabled":
            return self.options["command"]()
        return None

    def _release(self, event):
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _draw(self, _event=None):
        self.delete("all")
        primary = self.options["style"] == "Primary.TButton"
        disabled = self.options["state"] == "disabled"
        fill = (
            ("#E8EFFC" if primary else "#F7F9FC")
            if disabled
            else ("#1556DA" if self.hover else BLUE)
            if primary
            else (PALE if self.hover else "#F6F9FF")
        )
        fg = "#96A7C1" if disabled else WHITE if primary else BLUE
        edge = BLUE if not disabled and (primary or self.focus_get() is self) else LINE
        w, h = self.winfo_width(), self.winfo_height()
        rounded(self, 1, 1, max(2, w - 1), max(2, h - 1), fill=fill, outline=edge, width=1)
        image = self.options["image"]
        iw = image.width() + 8 if hasattr(image, "width") and not disabled else 0
        tw = self._font.measure(self.options["text"])
        x = (w - tw - iw) / 2
        if iw:
            self.create_image(x, h / 2, image=image, anchor="w")
        self.create_text(
            x + iw, h / 2, text=self.options["text"], anchor="w", fill=fg, font=self._font
        )


class RichTable(tk.Frame):
    """Virtualised native rows with photos and status pills, using stable record IDs.

    Only visible rows are painted. The small Treeview-compatible selection API
    lets controllers keep their existing record lookup and read-only actions.
    """

    def __init__(self, parent, columns, *, rowheight=60):
        super().__init__(parent, bg=WHITE)
        self.columns = columns
        self.rowheight = rowheight
        self.records = {}
        self.selected = ()
        self._pending = None
        self._region = None
        self.body_font = tkfont.Font(root=parent, family=FONT, size=11)
        self.small_font = tkfont.Font(root=parent, family=FONT, size=10)
        self.bold_font = tkfont.Font(root=parent, family=FONT, size=11, weight="bold")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.header = tk.Canvas(self, height=40, width=1, bg=WHITE, highlightthickness=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.canvas = tk.Canvas(
            self, width=1, height=250, bg=WHITE, highlightthickness=0, takefocus=True
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.scrollbar = SlimScrollbar(self, orient="vertical", command=self._scroll)
        self.scrollbar.grid(row=1, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.canvas.bind("<Configure>", self._schedule)
        self.canvas.bind("<Button-1>", self._click)
        bind_scrolling(self.canvas, self._wheel)
        bind_scrolling(self.header, self._wheel)
        self.canvas.bind("<Up>", lambda e: self._step(-1))
        self.canvas.bind("<Down>", lambda e: self._step(1))
        self.canvas.bind("<Home>", lambda e: self._step(-len(self.records)))
        self.canvas.bind("<End>", lambda e: self._step(len(self.records)))
        self.bind("<Destroy>", self._destroy, add="+")

    def _destroy(self, event):
        if event.widget is self and self._pending:
            self.after_cancel(self._pending)
            self._pending = None

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._paint)

    def _scrolled(self, first, last):
        self.scrollbar.set(first, last)
        self._schedule()

    def _scroll(self, *args):
        self.canvas.yview(*args)
        self._schedule()

    def _wheel(self, event, *, precise=False):
        return scroll_canvas(self.canvas, event, precise=precise)

    def get_children(self):
        return tuple(self.records)

    def selection(self):
        return self.selected

    def selection_set(self, iid):
        if isinstance(iid, (tuple, list)):
            iid = iid[0] if iid else None
        selection = (iid,) if iid in self.records else ()
        if self.selected != selection:
            self.selected = selection
            self._schedule()
            self.event_generate("<<TreeviewSelect>>", when="tail")

    def insert(self, _parent, _index, *, iid, values, tags=(), **visual):
        self.records[iid] = dict(values=values, tags=tags, **visual)
        self._schedule()
        return iid

    def delete(self, iid):
        self.records.pop(iid, None)
        if iid in self.selected:
            self.selected = ()
        self._schedule()

    def _click(self, event):
        self.canvas.focus_set()
        index = int(self.canvas.canvasy(event.y) // self.rowheight)
        keys = self.get_children()
        if 0 <= index < len(keys):
            self.selection_set(keys[index])
        return "break"

    def _step(self, direction):
        keys = self.get_children()
        if not keys:
            return "break"
        old = keys.index(self.selected[0]) if self.selected else -1
        index = max(0, min(len(keys) - 1, old + direction))
        self.selection_set(keys[index])
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        y = index * self.rowheight
        if y < top:
            self.canvas.yview_moveto(y / (len(keys) * self.rowheight))
        elif y + self.rowheight > bottom:
            self.canvas.yview_moveto(
                (y + self.rowheight - self.canvas.winfo_height()) / (len(keys) * self.rowheight)
            )
        return "break"

    def _fit(self, value, width, font):
        text = str(value)
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""

    def _paint(self):
        self._pending = None
        c = self.canvas
        width, height = c.winfo_width(), c.winfo_height()
        c.delete("all")
        self.header.delete("all")
        widths = [w for _, w in self.columns]
        # Preserve space for source, price and badges, while allowing long names
        # to truncate; the full name remains available in the selected detail.
        available = max(1, width)
        widths[0] = max(110, available - sum(widths[1:]))
        if sum(widths) > available:
            scale = available / sum(widths)
            widths = [w * scale for w in widths]
        x = 0
        rounded(self.header, 0, 0, width, 39, fill="#F0F5FC")
        for (title, _), w in zip(self.columns, widths):
            self.header.create_text(
                x + 10,
                20,
                anchor="w",
                text=self._fit(title, w - 16, self.bold_font),
                font=self.bold_font,
                fill=INK,
            )
            x += w
        total = len(self.records) * self.rowheight
        region = (0, 0, width, max(height, total))
        if region != self._region:
            self._region = region
            c.configure(scrollregion=region, yscrollincrement=1)
        first = max(0, int(c.canvasy(0) // self.rowheight))
        end = min(len(self.records), first + height // self.rowheight + 2)
        keys = tuple(self.records)
        for index in range(first, end):
            iid = keys[index]
            row = self.records[iid]
            y = index * self.rowheight
            selected = iid in self.selected
            if selected:
                rounded(c, 0, y + 3, width - 1, y + self.rowheight - 3, fill=PALE)
            else:
                c.create_line(
                    7, y + self.rowheight - 1, width - 7, y + self.rowheight - 1, fill="#EDF2F8"
                )
            x = 0
            for col, (value, w) in enumerate(zip(row["values"], widths)):
                px = x + 10
                image = row.get("image") if col == 0 else row.get("icons", {}).get(col)
                if image:
                    c.create_image(px, y + self.rowheight / 2, anchor="w", image=image)
                    px += image.width() + 9
                badge = row.get("badges", {}).get(col)
                if badge:
                    bg, fg = TONES[badge]
                    tw = min(w - 14, self.small_font.measure(str(value)) + 20)
                    rounded(
                        c,
                        x + 5,
                        y + self.rowheight / 2 - 14,
                        x + 5 + tw,
                        y + self.rowheight / 2 + 14,
                        fill=bg,
                        radius=14,
                    )
                    c.create_text(
                        x + 15,
                        y + self.rowheight / 2,
                        text=self._fit(value, tw - 20, self.small_font),
                        anchor="w",
                        font=self.small_font,
                        fill=fg,
                    )
                elif col == 0 and row.get("subtitle"):
                    c.create_text(
                        px,
                        y + self.rowheight / 2 - 10,
                        text=self._fit(value, x + w - px - 6, self.bold_font),
                        anchor="w",
                        font=self.bold_font,
                        fill=INK,
                    )
                    c.create_text(
                        px,
                        y + self.rowheight / 2 + 11,
                        text=self._fit(row["subtitle"], x + w - px - 6, self.small_font),
                        anchor="w",
                        font=self.small_font,
                        fill=MUTED,
                    )
                else:
                    c.create_text(
                        px,
                        y + self.rowheight / 2,
                        text=self._fit(value, x + w - px - 6, self.body_font),
                        anchor="w",
                        font=self.body_font,
                        fill=BLUE if col in row.get("emphasis", ()) else INK,
                    )
                x += w


def product_subtitle(name, specification=""):
    return specification or (
        re.search(r"\d+\s*(?:GB|TB)", name, re.I).group(0)
        if re.search(r"\d+\s*(?:GB|TB)", name, re.I)
        else "产品示意"
    )


class StatusPill(tk.Canvas):
    """Compact read-only badge, sized to its current status text."""

    def __init__(self, parent, text="—", tone="muted"):
        self.text, self.tone = text, tone
        self.font = tkfont.Font(root=parent, family=FONT, size=10)
        super().__init__(parent, height=27, width=80, bg=WHITE, highlightthickness=0)
        self.bind("<Configure>", self._draw)
        self.configure(text=text, tone=tone)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        self.text = kwargs.pop("text", self.text)
        self.tone = kwargs.pop("tone", self.tone)
        if kwargs:
            super().configure(**kwargs)
        super().configure(width=self.font.measure(self.text) + 24)
        self._draw()

    config = configure

    def cget(self, key):
        return self.text if key == "text" else super().cget(key)

    def _draw(self, _event=None):
        self.delete("all")
        bg, fg = TONES[self.tone]
        rounded(self, 0, 0, self.winfo_width(), 26, fill=bg, radius=13)
        self.create_text(self.winfo_width() / 2, 13, text=self.text, font=self.font, fill=fg)


class IconMedallion(tk.Canvas):
    """Circular UI background for an existing bundled library icon."""

    def __init__(self, parent, image, size, bg=WHITE, tone="blue"):
        self.picture = image
        self.size = size + 18
        self.tone = tone
        super().__init__(parent, width=self.size, height=self.size, bg=bg, highlightthickness=0)
        self.bind("<Configure>", self._draw)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        self.picture = kwargs.pop("image", self.picture)
        self.tone = kwargs.pop("tone", self.tone)
        if kwargs:
            super().configure(**kwargs)
        self._draw()

    config = configure

    def _draw(self, _event=None):
        self.delete("all")
        size = self.size
        self.create_oval(
            1, 1, size - 1, size - 1, fill=TONES.get(self.tone, TONES["blue"])[0], outline=""
        )
        if self.picture:
            self.create_image(size / 2, size / 2, image=self.picture)


def numeric_price(value):
    """Presentation comparison only; unknown outcomes such as 无 are not prices."""
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


class SlimScrollbar(tk.Canvas):
    """Arrow-free native scrollbar; an empty track consumes no painted space."""

    def __init__(self, parent, *, orient="vertical", command):
        self.orient, self.command = orient, command
        self.first, self.last, self.hover, self.offset = 0.0, 1.0, False, 0.0
        super().__init__(
            parent,
            bg=parent.cget("bg"),
            highlightthickness=0,
            borderwidth=0,
            width=12 if orient == "vertical" else 1,
            height=12 if orient == "horizontal" else 1,
        )
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        bind_scrolling(self, self._wheel)

    def _wheel(self, event, *, precise=False):
        dx, dy = wheel_pixels(self, event, precise=precise)
        pixels = dy if self.orient == "vertical" else dx or dy
        span = self.last - self.first
        length, _thumb, _start = self._geometry()
        if pixels and length > 0 and span < 1:
            fraction = self.first + pixels * span / length
            self.command("moveto", max(0.0, min(1 - span, fraction)))
        return "break"

    def set(self, first, last):
        self.first = max(0.0, min(1.0, float(first)))
        self.last = max(self.first, min(1.0, float(last)))
        self._draw()

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _geometry(self):
        length = self.winfo_height() if self.orient == "vertical" else self.winfo_width()
        span = self.last - self.first
        thumb = min(length, max(24, length * span))
        start = self.first / (1 - span) * (length - thumb) if span < 1 else 0
        return length, thumb, start

    def _draw(self, _event=None):
        self.delete("all")
        if self.last - self.first >= 0.99999:
            return
        _length, thumb, start = self._geometry()
        color = "#92A6C4" if self.hover else "#CBD6E6"
        inset = 2 if self.hover else 3
        if self.orient == "vertical":
            rounded(self, inset, start, 12 - inset, start + thumb, fill=color, radius=4)
        else:
            rounded(self, start, inset, start + thumb, 12 - inset, fill=color, radius=4)

    def _position(self, event):
        return event.y if self.orient == "vertical" else event.x

    def _press(self, event):
        _length, thumb, start = self._geometry()
        position = self._position(event)
        self.offset = position - start if start <= position <= start + thumb else thumb / 2
        self._drag(event)

    def _drag(self, event):
        length, thumb, _start = self._geometry()
        if length <= thumb:
            return
        limit = 1 - (self.last - self.first)
        fraction = (self._position(event) - self.offset) / (length - thumb) * limit
        self.command("moveto", max(0.0, min(limit, fraction)))

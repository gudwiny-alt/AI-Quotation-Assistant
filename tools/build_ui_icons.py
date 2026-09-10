"""Optional asset build tool; requires CairoSVG and libcairo. App uses bundled PNGs."""

from pathlib import Path
import urllib.request
import cairosvg

out = Path("assets/ui-icons")
names = {
    "search": "search",
    "calendar": "calendar",
    "chevron-down": "chevron-down",
    "box": "box",
    "layout-dashboard": "layout-dashboard",
    "chart-no-axes-column-increasing": "chart-bar",
    "file-text": "file-text",
    "shield-check": "shield-check",
    "history": "history",
    "settings": "settings",
    "monitor": "device-desktop",
    "folder-open": "folder-open",
    "files": "files",
    "circle-check": "circle-check",
    "clock": "clock",
    "globe": "world",
    "image": "photo",
    "database": "database",
    "upload": "upload",
    "download": "download",
    "arrow-right": "arrow-right",
    "triangle-alert": "alert-triangle",
    "smartphone": "device-mobile",
}
base = "https://raw.githubusercontent.com/tabler/tabler-icons/v3.44.0/"
colors = {
    "blue": "#2468F5",
    "muted": "#536788",
    "white": "#FFFFFF",
    "green": "#0B9975",
    "orange": "#D88119",
}
for local, source in names.items():
    svg = (
        urllib.request.urlopen(base + "icons/outline/" + source + ".svg", timeout=30)
        .read()
        .decode()
    )
    (out / (local + ".svg")).write_text(svg)
    for name, color in colors.items():
        if name == "muted" and local in {"search", "calendar", "chevron-down"}:
            color = "#72829D"
        cairosvg.svg2png(
            bytestring=svg.replace("currentColor", color).encode(),
            write_to=str(out / (local + "-" + name + ".png")),
            output_width=96,
            output_height=96,
        )
    print(local, flush=True)
(out / "LICENSE").write_bytes(urllib.request.urlopen(base + "LICENSE", timeout=30).read())
(out / "README.md").write_text(
    "# UI icons\n\nTabler Icons v3.44.0 (MIT). Original vectors: https://github.com/tabler/tabler-icons/tree/v3.44.0/icons/outline\n\nPNG variants rendered from the supplied SVGs in the application palette, 96 × 96 pixels. Source names mapped to application names in tools/build_ui_icons.py. No production network request is used to display icons.\n"
)

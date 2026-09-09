# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821 — build names are injected by PyInstaller
"""Independent directory-based preview bundle; leaves the stable application intact."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


PROJECT_ROOT = Path(SPECPATH).parent
APP_NAME = "铺货报价工作台-UI精修预览"
TEMPLATE_PATH = "resources/templates/quote_template.xlsx"
CATALOG_PATH = "resources/sites/catalog.json"

datas = [
    (
        str(PROJECT_ROOT / TEMPLATE_PATH),
        "resources/templates",
    ),
    (
        str(PROJECT_ROOT / CATALOG_PATH),
        "resources/sites",
    ),
]
for asset_dir in ("ui-icons", "ui-brands", "ui-media"):
    datas.append((str(PROJECT_ROOT / "assets" / asset_dir), "assets/" + asset_dir))
datas += collect_data_files("playwright")

a = Analysis(
    [str(PROJECT_ROOT / "src" / "quote_app" / "__main__.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PIL.Image",
        "openpyxl",
        "tkinter",
        "quote_app.sites.jd",
        "quote_app.sites.tmall",
        "quote_app.sites.official",
    ]
    + collect_submodules("quote_app.sites.official_brands"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name=APP_NAME)
if sys.platform == "darwin":
    app = BUNDLE(
        coll, name=f"{APP_NAME}.app", bundle_identifier="com.fjmobile.quotation.ui-preview"
    )

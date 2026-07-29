# -*- mode: python ; coding: utf-8 -*-
"""Shared PyInstaller definition for the Mac pilot and Windows portable build."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


PROJECT_ROOT = Path(SPECPATH).parent
APP_NAME = "福建移动铺货报价助手"
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
datas += collect_data_files('playwright')

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
    ],
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
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        name=f"{APP_NAME}.app",
        bundle_identifier="com.fjmobile.quotation",
    )
else:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        name=APP_NAME,
    )

# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821
"""Isolated Mac/Windows build of the decision/audit enhancement."""
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
APP_NAME = "报价决策智能体-决策稽核修复版"
datas = [(str(PROJECT_ROOT / "resources/templates/quote_template.xlsx"), "resources/templates"),
         (str(PROJECT_ROOT / "resources/sites/catalog.json"), "resources/sites")]
for asset_dir in ("ui-icons", "ui-brands", "ui-media"):
    datas.append((str(PROJECT_ROOT / "assets" / asset_dir), "assets/" + asset_dir))
datas += collect_data_files("playwright")
a = Analysis([str(PROJECT_ROOT / "src/quote_app/__main__.py")],
             pathex=[str(PROJECT_ROOT / "src")], binaries=[], datas=datas,
             hiddenimports=["PIL.Image", "openpyxl", "tkinter", "Foundation", "objc", "quote_app.sites.jd",
                            "quote_app.sites.tmall", "quote_app.sites.official"]
                            + collect_submodules("quote_app.sites.official_brands"),
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME, debug=False,
          bootloader_ignore_signals=False, strip=False, upx=True, console=False,
          disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name=APP_NAME)
if sys.platform == "darwin":
    app = BUNDLE(coll, name=APP_NAME + ".app", bundle_identifier="com.fjmobile.quotation.review-recovery")

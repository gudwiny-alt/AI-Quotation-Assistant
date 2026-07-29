from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_shared_capture_modules_import_without_pyobjc_frameworks() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ | {"PYTHONPATH": str(source_root)}
    program = """
import builtins

blocked_prefixes = ("Quartz", "AppKit", "ApplicationServices", "objc")
original_import = builtins.__import__

def blocked_pyobjc_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.startswith(blocked_prefixes):
        raise ImportError(f"blocked PyObjC framework import: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = blocked_pyobjc_import

import quote_app.app
import quote_app.evidence.macos
import quote_app.evidence.macos_native
import quote_app.evidence.macos_runtime
import quote_app.evidence.platform
"""

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=source_root.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

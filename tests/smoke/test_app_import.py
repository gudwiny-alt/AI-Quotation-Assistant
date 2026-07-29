from __future__ import annotations

import importlib
import sys
from unittest.mock import Mock, patch


def test_importing_app_does_not_create_a_tk_window() -> None:
    sys.modules.pop("quote_app.app", None)
    with patch("tkinter.Tk", new=Mock()) as tk_constructor:
        module = importlib.import_module("quote_app.app")

    assert "首次使用" in module.BETA_NOTICE
    assert "登录" in module.BETA_NOTICE
    tk_constructor.assert_not_called()

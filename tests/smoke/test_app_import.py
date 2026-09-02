from __future__ import annotations

import importlib
import sys
from unittest.mock import Mock, patch


def test_importing_app_does_not_create_a_tk_window() -> None:
    sys.modules.pop("quote_app.app", None)
    with patch("tkinter.Tk", new=Mock()) as tk_constructor:
        module = importlib.import_module("quote_app.app")

    assert "执行顺序：品牌官网、天猫、京东" in module.BETA_NOTICE
    assert "继续当前任务" in module.BETA_NOTICE
    tk_constructor.assert_not_called()

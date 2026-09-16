import os
from pathlib import Path

import pytest

from quote_app.browser.channel_detection import BrowserChoice, BrowserNotFoundError
from quote_app.services.readiness import ReadinessCheck, ReadinessItem
from tests.ui.test_desktop_workbench import workbench as base_workbench

pytestmark = pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Requires desktop WindowServer"
)


@pytest.fixture
def workbench(tmp_path):
    yield from base_workbench.__wrapped__(tmp_path)


def test_settings_shows_real_status_and_keeps_it_across_navigation(workbench, monkeypatch):
    from quote_app import desktop_ui

    monkeypatch.setattr(desktop_ui, "detect_browser_choice", lambda: BrowserChoice(
        "chrome", Path("/test/Chrome"), (Path("/test/Chrome"),)
    ))
    view = workbench
    # A busy Chrome profile must not look like a missing Chrome installation.
    view.update_readiness(ReadinessCheck((
        ReadinessItem("browser_profile", "占用", False),
        ReadinessItem("screen_capture", "录屏", True),
        ReadinessItem("accessibility", "辅助功能", False),
    )))
    view.show_page("settings")
    assert {k: w.cget("text") for k, w in view.environment_status_labels.items()} == {
        "chrome": "已检测", "screen_capture": "已授权", "accessibility": "未授权"
    }
    assert all(w.cget("image") for w in view.environment_status_icons.values())
    view.show_page("overview")
    view.show_page("settings")
    assert view.environment_status_labels["screen_capture"].cget("text") == "已授权"
    view.update_readiness(None)
    assert view.environment_status_labels["screen_capture"].cget("text") == "检测失败"
    assert view.environment_status_labels["accessibility"].cget("text") == "检测失败"


def test_unknown_or_missing_capabilities_never_show_authorized(workbench, monkeypatch):
    from quote_app import desktop_ui

    def missing():
        raise BrowserNotFoundError("missing Chrome")

    monkeypatch.setattr(desktop_ui, "detect_browser_choice", missing)
    workbench.show_page("settings")
    assert all(w.cget("text") == "未检测" for w in workbench.environment_status_labels.values())
    workbench.update_readiness(ReadinessCheck(()))
    assert workbench.environment_status_labels["chrome"].cget("text") == "未检测到"
    assert workbench.environment_status_labels["screen_capture"].cget("text") != "已授权"
    assert workbench.environment_status_labels["accessibility"].cget("text") != "已授权"

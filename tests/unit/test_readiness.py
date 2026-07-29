from __future__ import annotations

from pathlib import Path

import pytest

from quote_app.paths import build_app_paths


def _paths(tmp_path: Path):
    return build_app_paths("Darwin", home=tmp_path / "user")


def test_macos_missing_screen_permission_blocks_readiness(tmp_path: Path) -> None:
    from quote_app.services.readiness import check_runtime_readiness

    check = check_runtime_readiness(
        _paths(tmp_path),
        platform_name="Darwin",
        screen_permission=lambda: False,
        accessibility_permission=lambda: True,
        profile_available=lambda _profile: True,
    )

    assert check.ready is False
    assert check.messages == ("未开启屏幕与系统音频录制权限",)


def test_macos_missing_accessibility_blocks_readiness(tmp_path: Path) -> None:
    from quote_app.services.readiness import check_runtime_readiness

    check = check_runtime_readiness(
        _paths(tmp_path),
        platform_name="Darwin",
        screen_permission=lambda: True,
        accessibility_permission=lambda: False,
        profile_available=lambda _profile: True,
    )

    assert check.ready is False
    assert check.messages == ("未开启辅助功能权限",)


def test_ready_items_use_positive_labels_while_messages_keep_failure_detail(
    tmp_path: Path,
) -> None:
    from quote_app.services.readiness import check_runtime_readiness

    check = check_runtime_readiness(
        _paths(tmp_path),
        platform_name="Darwin",
        screen_permission=lambda: True,
        accessibility_permission=lambda: True,
        profile_available=lambda _profile: True,
    )

    assert check.ready is True
    assert check.messages == ()
    assert [item.label for item in check.items] == [
        "程序专用 Chrome 未被占用",
        "屏幕与系统音频录制权限",
        "辅助功能权限",
    ]


def test_windows_excludes_macos_permission_items(tmp_path: Path) -> None:
    from quote_app.services.readiness import check_runtime_readiness

    check = check_runtime_readiness(
        _paths(tmp_path),
        platform_name="Windows",
        profile_available=lambda _profile: True,
    )

    assert check.ready is True
    assert {item.key for item in check.items}.isdisjoint(
        {"screen_capture", "accessibility"}
    )


def test_profile_in_use_is_reported_without_opening_a_browser(tmp_path: Path) -> None:
    from quote_app.services.readiness import check_runtime_readiness

    def unavailable(_profile: Path) -> bool:
        return False

    check = check_runtime_readiness(
        _paths(tmp_path),
        platform_name="Windows",
        profile_available=unavailable,
    )

    assert check.ready is False
    assert check.messages == ("程序专用 Chrome 正在运行，请先完全退出 Chrome",)


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_readiness_rejects_non_boolean_permission_provider(
    tmp_path: Path,
    value: object,
) -> None:
    from quote_app.services.readiness import check_runtime_readiness

    with pytest.raises(ValueError, match="screen_permission"):
        check_runtime_readiness(
            _paths(tmp_path),
            platform_name="Darwin",
            screen_permission=lambda: value,
            accessibility_permission=lambda: True,
            profile_available=lambda _profile: True,
        )

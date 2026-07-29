from pathlib import Path


def test_app_paths_keep_browser_login_and_task_state_outside_replaceable_program_files(
    tmp_path: Path,
) -> None:
    from quote_app.paths import build_app_paths

    paths = build_app_paths("Darwin", home=tmp_path)

    assert paths.data_dir == tmp_path / "Library" / "Application Support" / "福建移动铺货报价助手"
    assert paths.browser_profile.parent == paths.data_dir
    assert paths.task_database.parent == paths.data_dir
    assert paths.evidence_dir.parent == paths.data_dir


def test_windows_app_paths_use_the_per_user_local_appdata_location(tmp_path: Path) -> None:
    from quote_app.paths import build_app_paths

    paths = build_app_paths("Windows", home=tmp_path)

    assert paths.data_dir == tmp_path / "AppData" / "Local" / "福建移动铺货报价助手"

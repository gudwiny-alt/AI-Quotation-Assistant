from __future__ import annotations

from pathlib import Path

from quote_app.browser.channel_detection import BrowserChoice


def test_open_login_browser_prepares_all_channel_profiles_and_opens_login_channels(
    tmp_path: Path,
) -> None:
    from quote_app.browser.login import JD_LOGIN_URL, TMALL_LOGIN_URL, open_login_browser
    from quote_app.browser.session import PROFILE_MARKER_NAME

    executable = tmp_path / "Google Chrome"
    choice = BrowserChoice("chrome", executable, (executable,))
    commands: list[list[str]] = []

    open_login_browser(
        tmp_path / "program-profile",
        browser_choice=choice,
        launch=commands.append,
    )

    assert (tmp_path / "program-profile" / PROFILE_MARKER_NAME).is_file()
    assert (tmp_path / "program-profile-official" / PROFILE_MARKER_NAME).is_file()
    assert (tmp_path / "program-profile-jd" / PROFILE_MARKER_NAME).is_file()
    assert commands == [
        [
            str(executable),
            f"--user-data-dir={tmp_path / 'program-profile'}",
            "--new-window",
            TMALL_LOGIN_URL,
        ],
        [
            str(executable),
            f"--user-data-dir={tmp_path / 'program-profile-jd'}",
            "--new-window",
            JD_LOGIN_URL,
        ],
    ]


def test_open_login_browser_rejects_an_unmarked_existing_profile(tmp_path: Path) -> None:
    from quote_app.browser.login import open_login_browser
    from quote_app.browser.session import ProfileValidationError

    profile = tmp_path / "not-program-profile"
    profile.mkdir()
    (profile / "unrelated-file").write_text("not a browser profile", encoding="utf-8")

    try:
        open_login_browser(profile, launch=lambda _command: None)
    except ProfileValidationError as error:
        assert "专用浏览器资料目录" in str(error)
    else:
        raise AssertionError("unsafe profile should not be opened")


def test_browser_session_reports_a_human_action_when_chrome_owns_the_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quote_app.browser import session

    profile = tmp_path / "program-profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("localhost-4321")
    monkeypatch.setattr(session, "_process_exists", lambda pid: pid == 4321)

    try:
        session.ensure_browser_profile_available(profile)
    except session.BrowserProfileInUseError as error:
        assert "退出 Google Chrome" in str(error)
    else:
        raise AssertionError("a live Chrome profile must not be handed to Playwright")

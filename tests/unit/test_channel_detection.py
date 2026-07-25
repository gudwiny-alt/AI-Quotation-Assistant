from __future__ import annotations

from pathlib import Path

import pytest

from quote_app.browser.channel_detection import (
    BrowserNotFoundError,
    detect_browser_choice,
)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"browser")
    return path


def test_windows_prefers_existing_chrome_over_edge(tmp_path: Path) -> None:
    chrome = _executable(tmp_path / "Chrome" / "chrome.exe")
    edge = _executable(tmp_path / "Edge" / "msedge.exe")

    choice = detect_browser_choice(
        platform_name="Windows",
        candidates={"chrome": (chrome,), "edge": (edge,)},
    )

    assert choice.browser_name == "chrome"
    assert choice.executable_path == chrome.resolve()


def test_windows_falls_back_to_edge_when_chrome_candidates_do_not_exist(
    tmp_path: Path,
) -> None:
    missing_user_chrome = tmp_path / "user" / "chrome.exe"
    missing_system_chrome = tmp_path / "system" / "chrome.exe"
    user_edge = _executable(tmp_path / "user" / "msedge.exe")
    system_edge = _executable(tmp_path / "system" / "msedge.exe")

    choice = detect_browser_choice(
        platform_name="Windows",
        candidates={
            "chrome": (missing_user_chrome, missing_system_chrome),
            "edge": (user_edge, system_edge),
        },
    )

    assert choice.browser_name == "edge"
    assert choice.executable_path == user_edge.resolve()
    assert choice.searched_paths == (
        missing_user_chrome.resolve(),
        missing_system_chrome.resolve(),
        user_edge.resolve(),
    )


def test_mac_accepts_existing_google_chrome_executable(tmp_path: Path) -> None:
    chrome = _executable(
        tmp_path / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    )

    choice = detect_browser_choice(
        platform_name="Darwin",
        candidates={"chrome": (chrome,)},
    )

    assert choice.browser_name == "chrome"
    assert choice.executable_path == chrome.resolve()


def test_default_mac_candidates_check_system_then_user_chrome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    system_chrome = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    ).resolve()
    user_chrome = (
        home
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    ).resolve()
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: path.resolve() == user_chrome,
    )

    choice = detect_browser_choice(
        platform_name="Darwin",
        environ={},
        home_dir=home,
    )

    assert choice.executable_path == user_chrome
    assert choice.searched_paths == (system_chrome, user_chrome)


def test_existing_directory_is_not_accepted_as_an_executable(tmp_path: Path) -> None:
    directory = tmp_path / "chrome.exe"
    directory.mkdir()

    with pytest.raises(BrowserNotFoundError) as captured:
        detect_browser_choice(
            platform_name="Windows",
            candidates={"chrome": (directory,), "edge": ()},
        )

    assert captured.value.searched_paths == (directory.resolve(),)
    assert str(directory.resolve()) in str(captured.value)


def test_default_windows_candidates_include_user_and_system_chrome_and_edge(
    tmp_path: Path,
) -> None:
    local = tmp_path / "Local"
    program_files = tmp_path / "Program Files"
    program_files_x86 = tmp_path / "Program Files (x86)"
    system_edge = _executable(
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    )

    choice = detect_browser_choice(
        platform_name="Windows",
        environ={
            "LOCALAPPDATA": str(local),
            "PROGRAMFILES": str(program_files),
            "PROGRAMFILES(X86)": str(program_files_x86),
        },
    )

    assert choice.executable_path == system_edge.resolve()
    assert choice.searched_paths[:3] == (
        (local / "Google" / "Chrome" / "Application" / "chrome.exe").resolve(),
        (
            program_files
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        ).resolve(),
        (
            program_files_x86
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        ).resolve(),
    )
    assert (
        local / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    ).resolve() in choice.searched_paths


def test_unsupported_platform_reports_a_stable_error() -> None:
    with pytest.raises(BrowserNotFoundError, match="unsupported platform"):
        detect_browser_choice(platform_name="Plan9")

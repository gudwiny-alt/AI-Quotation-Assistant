from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from quote_app.browser.channel_detection import (
    BrowserNotFoundError,
    detect_browser_choice,
)
from quote_app.browser.profile_lock import BrowserProfileLock
from quote_app.browser.session import PersistentBrowserSession, ProfileValidationError


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><html><body>persistent fixture</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def _local_http_origin() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = server.server_address
        raw_host = address[0]
        host = raw_host.decode() if isinstance(raw_host, bytes) else raw_host
        port = address[1]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeContext:
    def __init__(
        self,
        *,
        pages: list[_FakePage] | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.pages = list(pages or [_FakePage()])
        self.close_error = close_error
        self.close_calls = 0
        self.new_page_calls = 0

    def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.raise_closed_on_title = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def title(self) -> str:
        if self.raise_closed_on_title:
            raise RuntimeError("Target page, context or browser has been closed")
        return "fixture"


class _FakeChromium:
    def __init__(
        self,
        context: _FakeContext,
        startup_error: Exception | None = None,
    ) -> None:
        self.context = context
        self.startup_error = startup_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def launch_persistent_context(
        self, profile_dir: str, **kwargs: Any
    ) -> _FakeContext:
        self.calls.append((profile_dir, kwargs))
        if self.startup_error is not None:
            raise self.startup_error
        return self.context


class _FakePlaywright:
    def __init__(
        self,
        context: _FakeContext,
        startup_error: Exception | None = None,
    ) -> None:
        self.chromium = _FakeChromium(context, startup_error)
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeManager:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self.playwright = playwright
        self.start_calls = 0

    def start(self) -> _FakePlaywright:
        self.start_calls += 1
        return self.playwright


def _browser_choice(tmp_path: Path):
    executable = tmp_path / "chrome"
    executable.write_bytes(b"chrome")
    return detect_browser_choice(
        platform_name="Darwin",
        candidates={"chrome": (executable,)},
    )


def _fake_session(
    tmp_path: Path,
    *,
    profile: Path | None = None,
    context: _FakeContext | None = None,
    startup_error: Exception | None = None,
) -> tuple[PersistentBrowserSession, _FakeManager, _FakeContext]:
    fake_context = context or _FakeContext()
    playwright = _FakePlaywright(fake_context, startup_error)
    manager = _FakeManager(playwright)
    session = PersistentBrowserSession(
        profile or tmp_path / "profile",
        browser_choice=_browser_choice(tmp_path),
        playwright_factory=lambda: manager,
    )
    return session, manager, fake_context


def test_context_startup_failure_stops_playwright_and_releases_lock(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("browser failed to start")
    profile = tmp_path / "profile"
    session, manager, _ = _fake_session(
        tmp_path,
        profile=profile,
        startup_error=failure,
    )

    with pytest.raises(RuntimeError, match="browser failed to start"):
        session.start()

    assert manager.start_calls == 1
    assert manager.playwright.stop_calls == 1
    assert manager.playwright.chromium.calls == [
        (
            str(profile.resolve()),
            {
                "executable_path": str((tmp_path / "chrome").resolve()),
                "headless": False,
                "viewport": None,
                "args": [
                    "--start-maximized",
                    "--force-renderer-accessibility",
                ],
                "ignore_default_args": ["--no-sandbox"],
            },
        )
    ]
    marker = json.loads(
        (profile / ".quotation-browser-profile.json").read_text(encoding="utf-8")
    )
    assert marker == {
        "magic": "fujian-mobile-quotation-browser-profile",
        "schema_version": 1,
    }
    with BrowserProfileLock(profile):
        pass
    session.close()
    session.close()


def test_beta_launch_args_and_startup_preflight_run_before_page_use(
    tmp_path: Path,
) -> None:
    context = _FakeContext()
    initial_session, manager, fake_context = _fake_session(
        tmp_path,
        context=context,
    )
    received_contexts: list[_FakeContext] = []

    session = PersistentBrowserSession(
        tmp_path / "beta-profile",
        browser_choice=_browser_choice(tmp_path),
        playwright_factory=initial_session._playwright_factory,
        launch_args=(
            "--window-position=48,72",
            "--window-size=1024,640",
        ),
        startup_preflight=received_contexts.append,
    )

    with session as started:
        assert received_contexts == [fake_context]
        assert started.page_for("official:HONOR") is fake_context.pages[0]

    assert fake_context is context
    assert manager.playwright.chromium.calls == [
        (
            str((tmp_path / "beta-profile").resolve()),
            {
                "executable_path": str((tmp_path / "chrome").resolve()),
                "headless": False,
                "viewport": None,
                "args": [
                    "--window-position=48,72",
                    "--window-size=1024,640",
                    "--force-renderer-accessibility",
                ],
                "ignore_default_args": ["--no-sandbox"],
            },
        )
    ]


def test_startup_preflight_failure_closes_browser_resources(
    tmp_path: Path,
) -> None:
    context = _FakeContext()
    initial_session, manager, _ = _fake_session(
        tmp_path,
        context=context,
    )

    def fail_preflight(_context: _FakeContext) -> None:
        raise RuntimeError("startup window is not unique")

    session = PersistentBrowserSession(
        tmp_path / "beta-profile",
        browser_choice=_browser_choice(tmp_path),
        playwright_factory=initial_session._playwright_factory,
        startup_preflight=fail_preflight,
    )

    with pytest.raises(RuntimeError, match="startup window is not unique"):
        session.start()

    assert context.close_calls == 1
    assert manager.playwright.stop_calls == 1
    with BrowserProfileLock(tmp_path / "beta-profile"):
        pass


def test_nonempty_unmarked_profile_is_rejected_before_playwright_starts(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "existing-profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"user browser data")
    session, manager, _ = _fake_session(tmp_path, profile=profile)

    with pytest.raises(
        ProfileValidationError,
        match="不是本程序创建的专用浏览器资料目录",
    ):
        session.start()

    assert manager.start_calls == 0
    assert not (profile / ".quotation-browser-profile.json").exists()
    assert not (profile / ".quotation-browser-profile.lock").exists()
    with BrowserProfileLock(profile):
        pass


@pytest.mark.parametrize(
    "marker_text",
    [
        "{not-json",
        '{"magic":"other-program","schema_version":1}',
        '{"magic":"fujian-mobile-quotation-browser-profile","schema_version":2}',
    ],
)
def test_invalid_profile_marker_is_rejected_before_playwright_starts(
    tmp_path: Path,
    marker_text: str,
) -> None:
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    (profile / ".quotation-browser-profile.json").write_text(
        marker_text,
        encoding="utf-8",
    )
    session, manager, _ = _fake_session(tmp_path, profile=profile)

    with pytest.raises(ProfileValidationError, match="专用浏览器资料目录标记无效"):
        session.start()

    assert manager.start_calls == 0
    assert {path.name for path in profile.iterdir()} == {
        ".quotation-browser-profile.json"
    }
    with BrowserProfileLock(profile):
        pass


def test_valid_marker_allows_reopen_after_browser_files_exist(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    first, first_manager, _ = _fake_session(tmp_path, profile=profile)
    first.start()
    first.close()
    (profile / "Default").mkdir()
    (profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    residue = profile / ".quotation-browser-profile.json.abcdefgh.tmp"
    residue.write_text("stale", encoding="utf-8")

    reopened, reopened_manager, _ = _fake_session(tmp_path, profile=profile)
    reopened.start()
    reopened.close()

    assert first_manager.start_calls == 1
    assert reopened_manager.start_calls == 1
    assert not residue.exists()


def test_stale_exact_lock_file_is_recovered_and_marker_is_created(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    stale_lock = profile / ".quotation-browser-profile.lock"
    stale_lock.write_bytes(b"\0")
    session, manager, _ = _fake_session(tmp_path, profile=profile)

    session.start()
    session.close()

    assert manager.start_calls == 1
    assert stale_lock.exists()
    assert json.loads(
        (profile / ".quotation-browser-profile.json").read_text(encoding="utf-8")
    ) == {
        "magic": "fujian-mobile-quotation-browser-profile",
        "schema_version": 1,
    }


def test_marker_temp_residue_is_cleaned_only_after_lock_and_profile_recovers(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    residue = profile / ".quotation-browser-profile.json.abcd1234.tmp"
    residue.write_text("partial marker", encoding="utf-8")
    session, manager, _ = _fake_session(tmp_path, profile=profile)

    session.start()
    session.close()

    assert manager.start_calls == 1
    assert not residue.exists()
    assert (profile / ".quotation-browser-profile.json").is_file()


@pytest.mark.parametrize(
    "untrusted_name",
    [
        ".quotation-browser-profile.lock.bak",
        ".quotation-browser-profile.json.tmp",
        ".quotation-browser-profile.json.abcd1234.tmp.other",
    ],
)
def test_similar_but_non_program_profile_files_are_rejected_without_mutation(
    tmp_path: Path,
    untrusted_name: str,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    untrusted = profile / untrusted_name
    untrusted.write_text("user data", encoding="utf-8")
    (profile / ".quotation-browser-profile.lock").write_bytes(b"\0")
    (profile / ".quotation-browser-profile.json.abcdefgh.tmp").write_text(
        "partial",
        encoding="utf-8",
    )
    before = {path.name: path.read_bytes() for path in profile.iterdir()}
    session, manager, _ = _fake_session(tmp_path, profile=profile)

    with pytest.raises(
        ProfileValidationError,
        match="不是本程序创建的专用浏览器资料目录",
    ):
        session.start()

    assert manager.start_calls == 0
    assert {path.name: path.read_bytes() for path in profile.iterdir()} == before


def test_active_process_lock_is_rejected_then_stale_file_recovers(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    residue = profile / ".quotation-browser-profile.json.abcdefgh.tmp"
    residue.write_text("partial", encoding="utf-8")
    package_root = Path(__file__).parents[2] / "src"
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from quote_app.browser.profile_lock import BrowserProfileLock\n"
        "lock = BrowserProfileLock(Path(sys.argv[1]))\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(package_root)
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(profile)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    session, manager, _ = _fake_session(tmp_path, profile=profile)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError, match="already in use"):
            session.start()
        assert manager.start_calls == 0
        assert not (profile / ".quotation-browser-profile.json").exists()
        assert residue.exists()

        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0

        session.start()
        session.close()
        assert manager.start_calls == 1
        assert (profile / ".quotation-browser-profile.json").is_file()
        assert not residue.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_concurrent_session_cannot_overwrite_initialized_profile_marker(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    first, _, _ = _fake_session(tmp_path, profile=profile)
    second, second_manager, _ = _fake_session(tmp_path, profile=profile)
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            second.start()
        assert second_manager.start_calls == 0
        assert json.loads(
            (profile / ".quotation-browser-profile.json").read_text(encoding="utf-8")
        ) == {
            "magic": "fujian-mobile-quotation-browser-profile",
            "schema_version": 1,
        }
    finally:
        first.close()


def test_page_for_reuses_family_isolates_families_and_replaces_closed_page(
    tmp_path: Path,
) -> None:
    session, _, context = _fake_session(tmp_path)
    session.start()
    try:
        jd = session.page_for("jd")
        assert session.page_for("jd") is jd
        tmall = session.page_for("tmall")
        assert tmall is not jd
        jd.close()
        replacement = session.page_for("jd")
        assert replacement is not jd
        assert replacement is not tmall
        assert context.new_page_calls == 2
    finally:
        session.close()


def test_automation_page_uses_a_fresh_tab_instead_of_a_restored_profile_tab(
    tmp_path: Path,
) -> None:
    session, _, context = _fake_session(tmp_path)
    session.start()
    try:
        restored = context.pages[0]
        automation = session.automation_page()

        assert automation is not restored
        assert context.new_page_calls == 1
        assert session.automation_page() is automation
    finally:
        session.close()


def test_automation_page_is_reused_and_unassigned_pages_are_closed(
    tmp_path: Path,
) -> None:
    session, _, context = _fake_session(tmp_path)
    session.start()
    try:
        original = session.automation_page()
        popup = context.new_page()

        closed = session.close_unassigned_pages()

        assert closed == 2
        assert session.automation_page() is original
        assert context.pages[0].is_closed()
        assert popup.is_closed()
    finally:
        session.close()


def test_automation_page_restarts_the_persistent_session_when_browser_is_closed(
    tmp_path: Path,
) -> None:
    session, manager, context = _fake_session(tmp_path)
    session.start()
    try:
        original = session.automation_page()
        original.raise_closed_on_title = True

        replacement = session.automation_page()

        assert replacement is not original
        assert manager.start_calls == 2
        assert context.new_page_calls == 2
    finally:
        session.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    session, manager, context = _fake_session(tmp_path)
    session.start()

    session.close()
    session.close()

    assert context.close_calls == 1
    assert manager.playwright.stop_calls == 1
    with BrowserProfileLock(tmp_path / "profile"):
        pass


def test_non_owner_thread_cannot_use_or_close_session(tmp_path: Path) -> None:
    session, _, _ = _fake_session(tmp_path)
    session.start()
    errors: list[str] = []

    def use_from_other_thread() -> None:
        for action in (lambda: session.page_for("jd"), session.close):
            try:
                action()
            except RuntimeError as error:
                errors.append(str(error))

    worker = threading.Thread(target=use_from_other_thread)
    worker.start()
    worker.join(timeout=5)
    try:
        assert not worker.is_alive()
        assert errors == [
            "Playwright browser session must be used on its creating thread",
            "Playwright browser session must be used on its creating thread",
        ]
    finally:
        session.close()


def test_context_close_error_still_stops_playwright_and_releases_profile_lock(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("context close failed")
    context = _FakeContext(close_error=failure)
    session, manager, _ = _fake_session(tmp_path, context=context)
    session.start()

    with pytest.raises(RuntimeError, match="context close failed"):
        session.close()

    assert context.close_calls == 1
    assert manager.playwright.stop_calls == 1
    with BrowserProfileLock(tmp_path / "profile"):
        pass
    session.close()


def test_cookie_and_local_storage_survive_close_and_reopen(tmp_path: Path) -> None:
    try:
        choice = detect_browser_choice()
    except BrowserNotFoundError as error:
        pytest.skip(str(error))

    profile = tmp_path / "persistent-profile"
    with _local_http_origin() as origin:
        with PersistentBrowserSession(profile, browser_choice=choice) as first:
            page = first.page_for("fixture")
            page.goto(origin)
            page.evaluate(
                """() => {
                    document.cookie =
                        "quotation_login=remembered; path=/; max-age=3600; SameSite=Lax";
                    localStorage.setItem("quotation-session", "remembered");
                }"""
            )

        with PersistentBrowserSession(profile, browser_choice=choice) as reopened:
            page = reopened.page_for("fixture")
            page.goto(origin)
            assert page.evaluate("() => document.cookie") == (
                "quotation_login=remembered"
            )
            assert page.evaluate(
                "() => localStorage.getItem('quotation-session')"
            ) == "remembered"

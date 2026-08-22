from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from quote_app.browser.channel_detection import BrowserChoice, detect_browser_choice
from quote_app.browser.profile_lock import BrowserProfileLock, PROFILE_LOCK_NAME

PROFILE_MARKER_NAME = ".quotation-browser-profile.json"
PROFILE_MARKER_MAGIC = "fujian-mobile-quotation-browser-profile"
PROFILE_MARKER_SCHEMA_VERSION = 1
AUTOMATION_PAGE_KEY = "quotation-automation"
_PROFILE_MARKER_TEMP_PATTERN = re.compile(
    rf"^{re.escape(PROFILE_MARKER_NAME)}\.[a-z0-9_]{{8}}\.tmp$"
)
_CHROME_SINGLETON_LOCK_NAME = "SingletonLock"
_CHROME_SINGLETON_PID_PATTERN = re.compile(r"-(\d+)$")


class ProfileValidationError(RuntimeError):
    pass


class BrowserProfileInUseError(RuntimeError):
    """The dedicated Chrome process has not been fully quit yet."""


def ensure_browser_profile_available(profile_dir: Path) -> None:
    """Reject only a live Chrome process holding this exact profile directory."""
    profile = Path(profile_dir).expanduser().resolve()
    singleton_lock = profile / _CHROME_SINGLETON_LOCK_NAME
    if not singleton_lock.is_symlink():
        return
    try:
        target = os.readlink(singleton_lock)
    except OSError:
        return
    match = _CHROME_SINGLETON_PID_PATTERN.search(target)
    if match is None:
        return
    if _process_exists(int(match.group(1))):
        raise BrowserProfileInUseError(
            "程序专用 Chrome 仍在运行。请在菜单栏选择“Google Chrome → 退出 Google Chrome”（⌘Q），等待数秒后重试。"
        )


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_closed_browser_error(error: BaseException) -> bool:
    message = str(error).lower()
    return (
        "target page, context or browser has been closed" in message
        or "browser has been closed" in message
    )


def prepare_dedicated_profile(profile_dir: Path) -> Path:
    """Create or validate the application-owned browser profile before Chrome opens it."""
    normalized = Path(profile_dir).expanduser().resolve()
    _preflight_dedicated_profile(normalized)
    ensure_browser_profile_available(normalized)
    with BrowserProfileLock(normalized) as profile_lock:
        _ensure_dedicated_profile(normalized, lock_name=profile_lock.lock_path.name)
    return normalized


class PersistentBrowserSession:
    """One visible installed-browser context backed by a locked local profile."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        browser_choice: BrowserChoice | None = None,
        playwright_factory: Callable[[], Any] = sync_playwright,
        launch_args: tuple[str, ...] | None = None,
        startup_preflight: Callable[[Any], None] | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.browser_choice = browser_choice
        self._playwright_factory = playwright_factory
        if launch_args is not None and (
            not isinstance(launch_args, tuple)
            or not all(isinstance(arg, str) and arg.strip() for arg in launch_args)
        ):
            raise ValueError("launch_args must contain nonblank strings")
        if startup_preflight is not None and not callable(startup_preflight):
            raise ValueError("startup_preflight must be callable")
        self._launch_args = launch_args
        self._startup_preflight = startup_preflight
        self._profile_lock = BrowserProfileLock(self.profile_dir)
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._pages: dict[str, Any] = {}
        self._owner_thread_id: int | None = None

    @property
    def context(self) -> Any:
        self._require_started_on_owner_thread()
        return self._context

    def start(self) -> PersistentBrowserSession:
        if self._context is not None:
            self._require_owner_thread()
            return self
        if self._owner_thread_id is not None:
            self._require_owner_thread()
        self._owner_thread_id = threading.get_ident()
        try:
            _preflight_dedicated_profile(self.profile_dir)
            ensure_browser_profile_available(self.profile_dir)
            choice = self.browser_choice or detect_browser_choice()
            self.browser_choice = choice
            self._profile_lock.acquire()
            _ensure_dedicated_profile(
                self.profile_dir,
                lock_name=self._profile_lock.lock_path.name,
            )
            manager = self._playwright_factory()
            self._playwright = manager.start()
            launch_args = (
                self._launch_args
                if self._launch_args is not None
                else ("--start-maximized",)
            )
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                executable_path=str(choice.executable_path),
                headless=False,
                viewport=None,
                args=[
                    *launch_args,
                    "--force-renderer-accessibility",
                ],
                ignore_default_args=["--no-sandbox"],
            )
            if self._startup_preflight is not None:
                self._startup_preflight(self._context)
        except BaseException:
            self._close_resources(suppress_errors=True)
            raise
        return self

    def page_for(self, site_family: str) -> Any:
        self._require_started_on_owner_thread()
        context = self._context
        if context is None:  # Narrow the runtime resource for static analysis.
            raise RuntimeError("browser session has not been started")
        normalized_family = site_family.strip()
        if not normalized_family:
            raise ValueError("site_family must not be blank")
        page = self._pages.get(normalized_family)
        if page is not None and not page.is_closed():
            return page
        unassigned_pages = [
            candidate
            for candidate in context.pages
            if candidate not in self._pages.values() and not candidate.is_closed()
        ]
        page = unassigned_pages[0] if unassigned_pages else context.new_page()
        self._pages[normalized_family] = page
        return page

    def automation_page(self) -> Any:
        """Return a fresh, dedicated page for automatic quotation work."""
        self._require_started_on_owner_thread()
        try:
            return self._usable_automation_page()
        except Exception as error:
            if not _is_closed_browser_error(error):
                raise
            self._close_resources(suppress_errors=True)
            self.start()
            return self._usable_automation_page()

    def _usable_automation_page(self) -> Any:
        context = self._context
        if context is None:
            raise RuntimeError("browser session has not been started")
        page = self._pages.get(AUTOMATION_PAGE_KEY)
        if page is None or page.is_closed():
            page = context.new_page()
            self._pages[AUTOMATION_PAGE_KEY] = page
        # A page can look open locally after its Chromium context exits.  A
        # harmless title read detects that stale Playwright handle before an
        # adapter starts a marketplace recovery navigation.
        page.title()
        return page

    def close_unassigned_pages(self) -> int:
        """Close popups so automatic work cannot accidentally switch tabs."""
        self._require_started_on_owner_thread()
        context = self._context
        if context is None:
            raise RuntimeError("browser session has not been started")
        automation = self.automation_page()
        closed = 0
        for candidate in tuple(context.pages):
            if candidate is automation or candidate.is_closed():
                continue
            candidate.close()
            closed += 1
        return closed

    def close(self) -> None:
        if self._owner_thread_id is not None:
            self._require_owner_thread()
        self._close_resources(suppress_errors=False)

    def __enter__(self) -> PersistentBrowserSession:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_started_on_owner_thread(self) -> None:
        self._require_owner_thread()
        if self._context is None:
            raise RuntimeError("browser session has not been started")

    def _require_owner_thread(self) -> None:
        if (
            self._owner_thread_id is not None
            and threading.get_ident() != self._owner_thread_id
        ):
            raise RuntimeError(
                "Playwright browser session must be used on its creating thread"
            )

    def _close_resources(self, *, suppress_errors: bool) -> None:
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        self._pages.clear()
        errors: list[BaseException] = []
        try:
            if context is not None:
                context.close()
        except BaseException as error:
            errors.append(error)
        try:
            if playwright is not None:
                playwright.stop()
        except BaseException as error:
            errors.append(error)
        try:
            self._profile_lock.close()
        except BaseException as error:
            errors.append(error)
        self._owner_thread_id = None
        if errors and not suppress_errors:
            raise errors[0]


def _preflight_dedicated_profile(profile_dir: Path) -> None:
    if not profile_dir.exists():
        return
    if not profile_dir.is_dir():
        raise ProfileValidationError("专用浏览器资料目录路径不是文件夹")
    marker_path = profile_dir / PROFILE_MARKER_NAME
    if marker_path.exists():
        _validate_profile_marker(marker_path)
        return
    try:
        entries = tuple(profile_dir.iterdir())
    except OSError as error:
        raise ProfileValidationError("无法检查专用浏览器资料目录") from error
    if any(not _is_recoverable_profile_entry(entry) for entry in entries):
        raise ProfileValidationError(
            "所选目录不是本程序创建的专用浏览器资料目录，已拒绝启动"
        )


def _ensure_dedicated_profile(
    profile_dir: Path,
    *,
    lock_name: str,
) -> None:
    marker_path = profile_dir / PROFILE_MARKER_NAME
    if marker_path.exists():
        _validate_profile_marker(marker_path)
        _remove_marker_temp_residue(profile_dir)
        return

    try:
        entries = tuple(profile_dir.iterdir())
    except OSError as error:
        raise ProfileValidationError("无法检查专用浏览器资料目录") from error
    unexpected_entries = tuple(
        entry
        for entry in entries
        if entry.name != lock_name and not _is_marker_temp_residue(entry)
    )
    if unexpected_entries:
        raise ProfileValidationError(
            "所选目录不是本程序创建的专用浏览器资料目录，已拒绝启动"
        )
    _remove_marker_temp_residue(profile_dir)
    _write_profile_marker_atomically(marker_path)


def _validate_profile_marker(marker_path: Path) -> None:
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ProfileValidationError("专用浏览器资料目录标记无效")
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfileValidationError("专用浏览器资料目录标记无效") from error
    if (
        type(payload) is not dict
        or payload.get("magic") != PROFILE_MARKER_MAGIC
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != PROFILE_MARKER_SCHEMA_VERSION
        or set(payload) != {"magic", "schema_version"}
    ):
        raise ProfileValidationError("专用浏览器资料目录标记无效")


def _is_recoverable_profile_entry(entry: Path) -> bool:
    if entry.is_symlink() or not entry.is_file():
        return False
    return entry.name == PROFILE_LOCK_NAME or _is_marker_temp_residue(entry)


def _is_marker_temp_residue(entry: Path) -> bool:
    return (
        not entry.is_symlink()
        and entry.is_file()
        and _PROFILE_MARKER_TEMP_PATTERN.fullmatch(entry.name) is not None
    )


def _remove_marker_temp_residue(profile_dir: Path) -> None:
    try:
        residues = tuple(
            entry for entry in profile_dir.iterdir() if _is_marker_temp_residue(entry)
        )
        for residue in residues:
            residue.unlink()
    except OSError as error:
        raise ProfileValidationError("无法清理专用浏览器资料目录临时标记") from error


def _write_profile_marker_atomically(marker_path: Path) -> None:
    marker = {
        "magic": PROFILE_MARKER_MAGIC,
        "schema_version": PROFILE_MARKER_SCHEMA_VERSION,
    }
    payload = (
        json.dumps(marker, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{marker_path.name}.",
        suffix=".tmp",
        dir=marker_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, marker_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from quote_app.browser import profile_lock
from quote_app.browser.profile_lock import BrowserProfileLock, ProfileLockError


def test_second_session_cannot_acquire_a_held_profile_lock(tmp_path: Path) -> None:
    first = BrowserProfileLock(tmp_path / "profile")
    second = BrowserProfileLock(tmp_path / "profile")

    first.acquire()
    try:
        with pytest.raises(ProfileLockError, match="already in use"):
            second.acquire()
    finally:
        first.close()


def test_released_lock_can_be_acquired_again_and_close_is_idempotent(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    first = BrowserProfileLock(profile)
    first.acquire()
    first.close()
    first.close()

    second = BrowserProfileLock(profile)
    second.acquire()
    assert second.is_acquired
    second.close()


def test_context_manager_releases_lock_when_body_raises(tmp_path: Path) -> None:
    profile = tmp_path / "profile"

    with pytest.raises(RuntimeError, match="body failed"):
        with BrowserProfileLock(profile):
            raise RuntimeError("body failed")

    with BrowserProfileLock(profile) as recovered:
        assert recovered.is_acquired


def test_lock_handle_remains_open_for_the_full_acquisition(tmp_path: Path) -> None:
    lock = BrowserProfileLock(tmp_path / "profile")

    lock.acquire()
    try:
        assert lock.is_acquired
        assert lock.handle is not None
        assert not lock.handle.closed
    finally:
        lock.close()


def test_other_process_is_rejected_while_held_then_recovers_after_exit(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
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
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(ProfileLockError, match="already in use"):
            BrowserProfileLock(profile).acquire()
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        with BrowserProfileLock(profile) as recovered:
            assert recovered.is_acquired
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_windows_locking_uses_seek_and_exactly_one_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int, int]] = []
    lock_file = (tmp_path / "lock").open("w+b")
    lock_file.write(b"marker")
    lock_file.seek(3)

    class FakeMsvcrt:
        LK_NBLCK = 10
        LK_UNLCK = 20

        @staticmethod
        def locking(file_descriptor: int, mode: int, byte_count: int) -> None:
            calls.append((file_descriptor, mode, byte_count, lock_file.tell()))

    monkeypatch.setattr(profile_lock.os, "name", "nt")
    monkeypatch.setattr(profile_lock, "import_module", lambda name: FakeMsvcrt)
    try:
        profile_lock._acquire_os_lock(lock_file)
        lock_file.seek(4)
        profile_lock._release_os_lock(lock_file)
    finally:
        lock_file.close()

    assert calls == [
        (calls[0][0], FakeMsvcrt.LK_NBLCK, 1, 0),
        (calls[0][0], FakeMsvcrt.LK_UNLCK, 1, 0),
    ]

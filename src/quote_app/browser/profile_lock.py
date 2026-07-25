from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

PROFILE_LOCK_NAME = ".quotation-browser-profile.lock"


class ProfileLockError(RuntimeError):
    pass


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


class BrowserProfileLock:
    """An OS-held exclusive lock protecting one dedicated browser profile."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.lock_path = self.profile_dir / PROFILE_LOCK_NAME
        self._handle: BinaryIO | None = None
        self._is_acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._is_acquired

    @property
    def handle(self) -> BinaryIO | None:
        return self._handle

    def acquire(self) -> BrowserProfileLock:
        if self._is_acquired:
            return self
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            if self.lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _acquire_os_lock(handle)
        except OSError as error:
            handle.close()
            raise ProfileLockError(
                f"browser profile is already in use: {self.profile_dir}"
            ) from error
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self._is_acquired = True
        return self

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        was_acquired = self._is_acquired
        self._is_acquired = False
        unlock_error: OSError | None = None
        try:
            if was_acquired:
                _release_os_lock(handle)
        except OSError as error:
            unlock_error = error
        finally:
            handle.close()
        if unlock_error is not None:
            raise ProfileLockError(
                f"failed to release browser profile lock: {self.profile_dir}"
            ) from unlock_error

    def __enter__(self) -> BrowserProfileLock:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _acquire_os_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, import_module("msvcrt"))
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl = cast(_FcntlModule, import_module("fcntl"))
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, import_module("msvcrt"))
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = cast(_FcntlModule, import_module("fcntl"))
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

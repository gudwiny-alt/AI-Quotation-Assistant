from __future__ import annotations

import hashlib
import os
import platform as host_platform
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quote_app.evidence.models import (
    EvidenceRecord,
    MacCapturePolicy,
    accepts_capture_validation,
    validate_mac_capture_policy,
)

OpenFileDescriptor = Callable[[Path, int], int]
StatEvidencePath = Callable[[Path], os.stat_result]


@dataclass(frozen=True, slots=True)
class EvidenceFileAudit:
    payload: bytes | None
    error_code: str | None
    error_message: str | None

    @property
    def is_valid(self) -> bool:
        return self.payload is not None


def read_validated_evidence(
    evidence: EvidenceRecord,
    *,
    open_fd: OpenFileDescriptor = os.open,
    lstat_path: StatEvidencePath = os.lstat,
    capture_acceptance_policy: MacCapturePolicy = MacCapturePolicy.STRICT,
) -> EvidenceFileAudit:
    platform_name = host_platform.system()
    validate_mac_capture_policy(
        capture_acceptance_policy,
        platform_name=platform_name,
    )
    if not accepts_capture_validation(
        evidence.validation_code,
        policy=capture_acceptance_policy,
        platform_name=platform_name,
    ):
        return _failure(
            "EVIDENCE_UNVALIDATED",
            "正式截图记录未通过验证",
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = open_fd(evidence.path, flags)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return _failure(
                    "EVIDENCE_NOT_REGULAR",
                    "正式截图不是普通文件",
                )
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                chunks.append(chunk)
            after_read = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(after_read.st_mode)
            or _file_identity(opened) != _file_identity(after_read)
        ):
            return _failure(
                "EVIDENCE_CHANGED",
                "正式截图在读取期间发生变化",
            )
        if digest.hexdigest() != evidence.sha256:
            return _failure(
                "EVIDENCE_HASH_MISMATCH",
                "正式截图哈希校验失败",
            )
        path_after_read = lstat_path(evidence.path)
        if (
            stat.S_ISLNK(path_after_read.st_mode)
            or not stat.S_ISREG(path_after_read.st_mode)
        ):
            return _failure(
                "EVIDENCE_NOT_REGULAR",
                "正式截图不是普通文件",
            )
        if _file_identity(after_read) != _file_identity(path_after_read):
            return _failure(
                "EVIDENCE_CHANGED",
                "正式截图路径在读取期间发生变化",
            )
    except (OSError, ValueError):
        return _unreadable_failure(evidence.path, lstat_path)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return EvidenceFileAudit(
        payload=b"".join(chunks),
        error_code=None,
        error_message=None,
    )


def _unreadable_failure(
    path: Path,
    lstat_path: StatEvidencePath,
) -> EvidenceFileAudit:
    try:
        path_stat = lstat_path(path)
    except OSError:
        return _failure(
            "EVIDENCE_MISSING",
            "正式截图文件缺失或不可读取",
        )
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return _failure(
            "EVIDENCE_NOT_REGULAR",
            "正式截图不是普通文件",
        )
    return _failure(
        "EVIDENCE_UNREADABLE",
        "正式截图文件缺失或不可读取",
    )


def _failure(code: str, message: str) -> EvidenceFileAudit:
    return EvidenceFileAudit(
        payload=None,
        error_code=code,
        error_message=message,
    )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )

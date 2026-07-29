from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EvidenceState(StrEnum):
    NORMAL = "normal"
    NO_MODEL = "no_model"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    COLOR_UNAVAILABLE = "color_unavailable"
    SOLD_OUT = "sold_out"


class MacCapturePolicy(StrEnum):
    """Code-selected policy for macOS formal evidence capture."""

    STRICT = "strict"
    MAC_VISUAL_REVIEW_BETA = "mac_visual_review_beta"


def accepts_capture_validation(
    validation_code: str,
    *,
    policy: MacCapturePolicy,
    platform_name: str,
) -> bool:
    """Accept only formal codes that are valid for the explicit policy."""
    if validation_code == "CAPTURE_OK":
        return True
    return (
        validation_code == "CAPTURE_OK_MAC_VISUAL_REVIEW"
        and policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
        and platform_name == "Darwin"
    )


def validate_mac_capture_policy(
    policy: MacCapturePolicy,
    *,
    platform_name: str,
) -> None:
    if not isinstance(policy, MacCapturePolicy):
        raise ValueError("policy must be a MacCapturePolicy")
    if (
        policy is MacCapturePolicy.MAC_VISUAL_REVIEW_BETA
        and platform_name != "Darwin"
    ):
        raise ValueError(
            "macOS visual-review beta policy is supported only on Darwin"
        )


@dataclass(frozen=True, slots=True)
class EvidenceRectangle:
    """One final annotation rectangle in captured-screen physical pixels."""

    role: str
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, str):
            raise ValueError("rectangle role must be a string")
        if not self.role.strip():
            raise ValueError("rectangle role must not be blank")
        if any(type(value) is not int for value in (self.x, self.y, self.width, self.height)):
            raise ValueError(
                "rectangle coordinates and dimensions must be integers"
            )
        if self.x < 0 or self.y < 0:
            raise ValueError("rectangle origin must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("rectangle dimensions must be positive")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    state: EvidenceState
    path: Path
    sha256: str
    pixel_width: int
    pixel_height: int
    captured_at: datetime
    validation_code: str
    annotations: tuple[EvidenceRectangle, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, EvidenceState):
            raise ValueError("state must be an EvidenceState")
        if not isinstance(self.path, Path):
            raise ValueError("path must be a Path")
        if not isinstance(self.sha256, str):
            raise ValueError("sha256 must be a string")
        if type(self.pixel_width) is not int or type(self.pixel_height) is not int:
            raise ValueError("pixel dimensions must be integers")
        if not isinstance(self.captured_at, datetime):
            raise ValueError("captured_at must be a datetime")
        if not isinstance(self.validation_code, str):
            raise ValueError("validation_code must be a string")
        if not isinstance(self.annotations, tuple | list):
            raise ValueError("annotations must be a sequence")
        if not all(
            isinstance(annotation, EvidenceRectangle)
            for annotation in self.annotations
        ):
            raise ValueError("annotations must contain EvidenceRectangle values")

        object.__setattr__(self, "path", _normalized_path(self.path))
        object.__setattr__(self, "annotations", tuple(self.annotations))

        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if self.pixel_width <= 0 or self.pixel_height <= 0:
            raise ValueError("pixel dimensions must be positive")
        if not _is_timezone_aware(self.captured_at):
            raise ValueError("captured_at must be timezone-aware")
        if not self.validation_code.strip():
            raise ValueError("validation_code must not be blank")
        for annotation in self.annotations:
            if annotation.x + annotation.width > self.pixel_width:
                raise ValueError("annotation rectangle exceeds image width")
            if annotation.y + annotation.height > self.pixel_height:
                raise ValueError("annotation rectangle exceeds image height")

    @property
    def is_validated(self) -> bool:
        return self.validation_code == "CAPTURE_OK"


def _normalized_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_timezone_aware(value: datetime) -> bool:
    if value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False

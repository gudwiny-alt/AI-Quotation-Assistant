from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageFilter, ImageStat


class SemanticHashProbe(Protocol):
    def semantic_hash(self) -> str: ...


class CaptureQualityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ImageQualityMetrics:
    """Measurable image signals only; these do not claim OCR readability."""

    luminance: float
    contrast: float
    entropy: float
    sharpness: float


def assess_capture_quality(
    image: Image.Image,
    *,
    expected_size: tuple[int, int],
    minimum_contrast: float = 5.0,
    minimum_sharpness: float = 8.0,
    minimum_entropy: float = 0.5,
) -> ImageQualityMetrics:
    """Apply measurable contrast, entropy, and sharpness gates.

    Passing these gates means the screenshot is not blank or obviously
    unreadable. It does not prove that any particular text can be read by OCR
    or by a human; that remains part of platform acceptance.
    """
    if not isinstance(image, Image.Image):
        raise ValueError("image must be a Pillow Image")
    if (
        not isinstance(expected_size, tuple)
        or len(expected_size) != 2
        or any(type(value) is not int or value <= 0 for value in expected_size)
    ):
        raise ValueError("expected_size must contain two positive integers")
    for name, value in (
        ("minimum_contrast", minimum_contrast),
        ("minimum_sharpness", minimum_sharpness),
        ("minimum_entropy", minimum_entropy),
    ):
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and non-negative")
    if image.size != expected_size:
        raise CaptureQualityError(
            "CAPTURE_GEOMETRY",
            "captured image dimensions do not match the primary display",
        )

    grayscale = image.convert("L")
    statistics = ImageStat.Stat(grayscale)
    luminance = float(statistics.mean[0])
    contrast = float(statistics.stddev[0])
    entropy = float(grayscale.entropy())
    extrema = grayscale.getextrema()
    if (
        extrema[0] == extrema[1]
        or (luminance >= 251 and contrast <= 2)
        or (luminance <= 4 and contrast <= 2)
    ):
        raise CaptureQualityError(
            "CAPTURE_BLANK",
            "captured image is blank or nearly uniform",
        )

    edge_statistics = ImageStat.Stat(grayscale.filter(ImageFilter.FIND_EDGES))
    sharpness = float(edge_statistics.var[0])
    if (
        contrast < minimum_contrast
        or entropy < minimum_entropy
        or sharpness < minimum_sharpness
    ):
        raise CaptureQualityError(
            "CAPTURE_UNREADABLE",
            "captured image has insufficient measurable contrast or sharpness",
        )
    return ImageQualityMetrics(
        luminance=luminance,
        contrast=contrast,
        entropy=entropy,
        sharpness=sharpness,
    )


def wait_for_stable_hash(
    probe: SemanticHashProbe,
    *,
    minimum_interval_seconds: float,
    max_checks: int = 3,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    if (
        not isinstance(minimum_interval_seconds, int | float)
        or isinstance(minimum_interval_seconds, bool)
        or not math.isfinite(minimum_interval_seconds)
        or minimum_interval_seconds <= 0
    ):
        raise ValueError(
            "minimum_interval_seconds must be finite and positive"
        )
    if type(max_checks) is not int or max_checks < 2:
        raise ValueError("max_checks must be at least 2")
    if not callable(sleeper):
        raise ValueError("sleeper must be callable")

    previous = _read_hash(probe)
    for _ in range(max_checks - 1):
        sleeper(float(minimum_interval_seconds))
        current = _read_hash(probe)
        if current == previous:
            return current
        previous = current
    raise CaptureQualityError(
        "CAPTURE_UNSTABLE",
        "page semantic state did not stabilize before capture",
    )


def _read_hash(probe: SemanticHashProbe) -> str:
    try:
        value = probe.semantic_hash()
    except Exception:
        raise CaptureQualityError(
            "CAPTURE_UNSTABLE",
            "page semantic state could not be read",
        ) from None
    if not isinstance(value, str) or not value.strip():
        raise CaptureQualityError(
            "CAPTURE_UNSTABLE",
            "page semantic state hash is missing",
        )
    return value.strip()

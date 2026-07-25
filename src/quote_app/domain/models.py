from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class QuoteMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")

    @classmethod
    def current(cls, today: date | None = None) -> QuoteMonth:
        value = today or date.today()
        return cls(value.year, value.month)


@dataclass(frozen=True, slots=True)
class InputPaths:
    base: Path
    marketing: Path
    bop: Path
    output_dir: Path


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str
    fatal: bool
    row_number: int | None = None


@dataclass(slots=True)
class QuoteRow:
    source_row_number: int
    material_code: str
    cells: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

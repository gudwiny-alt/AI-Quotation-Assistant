from pathlib import Path
from typing import Sequence

import msoffcrypto  # type: ignore[import-untyped]
from openpyxl import Workbook  # type: ignore[import-untyped]


def save_workbook(
    path: Path,
    headers: Sequence[object],
    rows: Sequence[Sequence[object]],
) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    workbook.close()
    return path


def save_empty_workbook(path: Path) -> Path:
    workbook = Workbook()
    workbook.active.title = "Sheet1"
    workbook.save(path)
    workbook.close()
    return path


def encrypt_workbook(source: Path, encrypted: Path, password: str = "test-password") -> Path:
    with source.open("rb") as source_file, encrypted.open("wb") as encrypted_file:
        msoffcrypto.OfficeFile(source_file).encrypt(password, encrypted_file)
    return encrypted

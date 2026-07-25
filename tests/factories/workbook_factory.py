from pathlib import Path
from typing import Sequence

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

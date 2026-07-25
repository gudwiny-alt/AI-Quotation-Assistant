from dataclasses import dataclass
from pathlib import Path

import pytest

from quote_app.domain.models import InputPaths
from tests.factories.workbook_factory import save_workbook


@dataclass(frozen=True, slots=True)
class ValidInputs:
    paths: InputPaths
    base_codes: tuple[str, ...]


@pytest.fixture
def valid_inputs(tmp_path: Path) -> ValidInputs:
    codes = ("9101", "9102")
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年3月结算报价（元/台）", "2026年7月结算报价（元/台）"],
        [["9101", 3999, 3899], ["9102", "无", "无"]],
    )
    marketing_headers = [f"营销字段{index}" for index in range(1, 9)] + ["物料编码"]
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        marketing_headers,
        [[""] * 8 + ["9101"], [""] * 8 + ["9102"]],
    )
    bop_headers = [f"BOP字段{index}" for index in range(1, 11)] + ["集团一级库编码"]
    bop = save_workbook(tmp_path / "bop.xlsx", bop_headers, [[""] * 10 + ["9101"]])
    return ValidInputs(InputPaths(base, marketing, bop, tmp_path), codes)

# Core Data and Excel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tested local core that validates the three source workbooks, creates one quotation row per Base-A code, applies rolling-month rules and formulas, and emits the quotation workbook plus execution report without website automation.

**Architecture:** Use a Python `src` layout with immutable domain records, pure normalization/month/rule functions, openpyxl workbook gateways, and an orchestration service. Keep browser concerns behind result records so later plans can attach web prices and evidence without changing the core mapping logic.

**Tech Stack:** Python 3.12, dataclasses, openpyxl 3.1.x, Pillow 11.x, SQLite from the standard library, pytest 8.x, ruff 0.12.x, mypy 1.17.x.

## Global Constraints

- Default quotation month is the natural month in which the program is run; the user may change it.
- Input consists of exactly three monthly workbooks: Base, Marketing Product Information, and BOP Resource Information.
- Base column A drives output row count and order; no row may be silently dropped or merged.
- Quote C matches Marketing I and BOP K after safe text normalization.
- Required historical months are quote month minus five natural months for I and quote month minus one natural month for J.
- K, L, M, N, P, and Q remain blank; N has the four approved dropdown values.
- V uses `J="无" OR F="申请配置"`; W uses `I="无" -> "否", otherwise "是"`.
- X, Y, Z, and AA remain Excel formulas with blank guards.
- Only worksheet `5G手机` remains in the quotation output.
- The quotation template appearance and print configuration must match the approved sample; only year/month text changes.
- All processing is local; no business file, screenshot, log, or credential is uploaded.
- Use TDD for every behavior and commit after each task.

---

## Planned File Structure

```text
pyproject.toml
README.md
resources/
  templates/
    quote_template.xlsx
src/
  quote_app/
    __init__.py
    domain/
      models.py
      statuses.py
    core/
      normalization.py
      months.py
      formulas.py
      association.py
      precheck.py
    excel/
      source_reader.py
      template_builder.py
      quote_writer.py
      report_writer.py
    services/
      core_pipeline.py
tests/
  conftest.py
  factories/
    workbook_factory.py
  unit/
    test_normalization.py
    test_months.py
    test_formulas.py
    test_association.py
    test_precheck.py
    test_quote_writer.py
    test_report_writer.py
  integration/
    test_core_pipeline.py
scripts/
  build_quote_template.py
```

## Task 1: Project Foundation and Domain Contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/quote_app/__init__.py`
- Create: `src/quote_app/domain/statuses.py`
- Create: `src/quote_app/domain/models.py`
- Create: `tests/unit/test_domain_models.py`

**Interfaces:**
- Produces: `QuoteMonth`, `InputPaths`, `QuoteRow`, `Issue`, and `RowStatus`.
- Consumes: no earlier application interfaces.

- [ ] **Step 1: Write the failing domain-model tests**

```python
from datetime import date
from quote_app.domain.models import QuoteMonth


def test_quote_month_defaults_to_current_natural_month() -> None:
    month = QuoteMonth.current(today=date(2026, 8, 18))
    assert (month.year, month.month) == (2026, 8)


def test_quote_month_rejects_month_13() -> None:
    try:
        QuoteMonth(2026, 13)
    except ValueError as exc:
        assert str(exc) == "month must be between 1 and 12"
    else:
        raise AssertionError("QuoteMonth accepted month 13")
```

- [ ] **Step 2: Run the focused test and verify the missing-module failure**

Run: `python -m pytest tests/unit/test_domain_models.py -v`

Expected: FAIL because `quote_app.domain.models` does not exist.

- [ ] **Step 3: Add the package configuration and minimal domain contracts**

```toml
[project]
name = "fujian-mobile-quotation"
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = [
  "openpyxl>=3.1.5,<4",
  "Pillow>=11,<13",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.4,<9",
  "ruff>=0.12,<0.13",
  "mypy>=1.17,<2",
]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

```python
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
    def current(cls, today: date | None = None) -> "QuoteMonth":
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
```

```python
from enum import StrEnum


class RowStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
```

- [ ] **Step 4: Run tests, static checks, and verify they pass**

Run: `python -m pytest tests/unit/test_domain_models.py -v`

Expected: 2 tests PASS.

Run: `python -m ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 5: Commit the foundation**

```bash
git add pyproject.toml src/quote_app tests/unit/test_domain_models.py
git commit -m "build: establish quotation core domain"
```

## Task 2: Normalization and Rolling-Month Rules

**Files:**
- Create: `src/quote_app/core/normalization.py`
- Create: `src/quote_app/core/months.py`
- Create: `tests/unit/test_normalization.py`
- Create: `tests/unit/test_months.py`

**Interfaces:**
- Consumes: `QuoteMonth` from Task 1.
- Produces: `normalize_code(value) -> str`, `normalize_brand(value) -> str`, `shift_month(value, delta) -> QuoteMonth`, and `required_price_months(value) -> tuple[QuoteMonth, QuoteMonth]`.

- [ ] **Step 1: Write failing normalization and month tests**

```python
from quote_app.core.normalization import normalize_brand, normalize_code


def test_code_normalization_preserves_digits_and_removes_noise() -> None:
    assert normalize_code(" 910200000041080\n") == "910200000041080"
    assert normalize_code(910200000041080) == "910200000041080"
    assert normalize_code(123.0) == "123"


def test_brand_aliases_use_approved_names() -> None:
    assert normalize_brand("荣耀") == "HONOR"
    assert normalize_brand(" vivo ") == "维沃"
    assert normalize_brand("OPPO") == "欧珀"
```

```python
from quote_app.core.months import required_price_months, shift_month
from quote_app.domain.models import QuoteMonth


def test_august_history_months_are_march_and_july() -> None:
    six_month_start, previous = required_price_months(QuoteMonth(2026, 8))
    assert six_month_start == QuoteMonth(2026, 3)
    assert previous == QuoteMonth(2026, 7)


def test_shift_month_crosses_year_boundary() -> None:
    assert shift_month(QuoteMonth(2026, 1), -5) == QuoteMonth(2025, 8)
```

- [ ] **Step 2: Run tests and verify both modules are missing**

Run: `python -m pytest tests/unit/test_normalization.py tests/unit/test_months.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement exact normalization and month arithmetic**

```python
import re
import unicodedata
from typing import Any

BRAND_ALIASES = {
    "荣耀": "HONOR",
    "HONOR": "HONOR",
    "华为": "华为",
    "HUAWEI": "华为",
    "VIVO": "维沃",
    "维沃": "维沃",
    "OPPO": "欧珀",
    "欧珀": "欧珀",
    "小米": "小米",
    "苹果": "苹果",
    "APPLE": "苹果",
    "ZTE": "ZTE中兴",
    "中兴": "ZTE中兴",
    "ZTE中兴": "ZTE中兴",
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalize_code(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\s+", "", normalize_text(value))


def normalize_brand(value: Any) -> str:
    text = normalize_text(value).upper()
    return BRAND_ALIASES.get(text, normalize_text(value))
```

```python
from quote_app.domain.models import QuoteMonth


def shift_month(value: QuoteMonth, delta: int) -> QuoteMonth:
    zero_based = value.year * 12 + value.month - 1 + delta
    year, month_index = divmod(zero_based, 12)
    return QuoteMonth(year, month_index + 1)


def required_price_months(value: QuoteMonth) -> tuple[QuoteMonth, QuoteMonth]:
    return shift_month(value, -5), shift_month(value, -1)
```

- [ ] **Step 4: Run focused and full core tests**

Run: `python -m pytest tests/unit/test_normalization.py tests/unit/test_months.py -v`

Expected: 4 tests PASS.

- [ ] **Step 5: Commit month and normalization rules**

```bash
git add src/quote_app/core tests/unit/test_normalization.py tests/unit/test_months.py
git commit -m "feat: add safe normalization and month rolling"
```

## Task 3: Synthetic Workbook Factory and Automatic Precheck

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/factories/workbook_factory.py`
- Create: `src/quote_app/excel/source_reader.py`
- Create: `src/quote_app/core/precheck.py`
- Create: `tests/unit/test_precheck.py`

**Interfaces:**
- Consumes: `InputPaths`, `QuoteMonth`, normalization/month functions.
- Produces: `PrecheckResult(fatal_issues, row_issues, identified_sheets)` and `precheck_inputs(paths, quote_month)`.

- [ ] **Step 1: Create synthetic workbook builders and failing precheck tests**

```python
from pathlib import Path
from openpyxl import Workbook


def save_workbook(path: Path, headers: list[str], rows: list[list[object]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path
```

`tests/conftest.py` defines the shared valid three-file fixture used by later integration tests:

```python
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
    marketing_headers = [f"营销字段{index}" for index in range(1, 9)] + ["集团一级库物料编码"]
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        marketing_headers,
        [[""] * 8 + ["9101"], [""] * 8 + ["9102"]],
    )
    bop_headers = [f"BOP字段{index}" for index in range(1, 11)] + ["集团一级库物料编码"]
    bop = save_workbook(tmp_path / "bop.xlsx", bop_headers, [[""] * 10 + ["9101"]])
    return ValidInputs(InputPaths(base, marketing, bop, tmp_path), codes)
```

```python
from quote_app.core.precheck import precheck_inputs
from quote_app.domain.models import InputPaths, QuoteMonth
from tests.factories.workbook_factory import save_workbook


def test_missing_march_header_is_fatal(tmp_path) -> None:
    base = save_workbook(
        tmp_path / "base.xlsx",
        ["集团一级库物料编码", "2026年7月结算报价（元/台）"],
        [["9101", 3999]],
    )
    marketing = save_workbook(
        tmp_path / "marketing.xlsx",
        [f"营销字段{index}" for index in range(1, 10)],
        [[""] * 8 + ["9101"]],
    )
    bop = save_workbook(
        tmp_path / "bop.xlsx",
        [f"BOP字段{index}" for index in range(1, 12)],
        [[""] * 10 + ["9101"]],
    )
    result = precheck_inputs(
        InputPaths(base, marketing, bop, tmp_path),
        QuoteMonth(2026, 8),
    )
    assert any(issue.code == "MISSING_HISTORY_MONTH" for issue in result.fatal_issues)
```

- [ ] **Step 2: Run the test and verify `precheck_inputs` is missing**

Run: `python -m pytest tests/unit/test_precheck.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement workbook identification, header indexing, and fatal checks**

```python
from dataclasses import dataclass
from pathlib import Path
from openpyxl import load_workbook


@dataclass(frozen=True, slots=True)
class SheetTable:
    path: Path
    sheet_name: str
    headers: dict[str, int]
    rows: tuple[tuple[object, ...], ...]

    def records_by_column_letter(self) -> tuple[dict[str, object], ...]:
        from openpyxl.utils import get_column_letter

        return tuple(
            {get_column_letter(index): value for index, value in enumerate(row, start=1)}
            for row in self.rows
        )


def read_first_table(path: Path) -> SheetTable:
    workbook = load_workbook(path, read_only=True, data_only=False)
    sheet = workbook[workbook.sheetnames[0]]
    values = tuple(tuple(row) for row in sheet.iter_rows(values_only=True))
    if not values:
        raise ValueError(f"workbook has no rows: {path.name}")
    headers = {str(value).strip(): index for index, value in enumerate(values[0]) if value}
    return SheetTable(path, sheet.title, headers, values[1:])
```

Implement `precheck_inputs` with these explicit conditions:

```python
MINIMUM_COLUMNS = {
    "base": 1,
    "marketing": 9,
    "bop": 11,
}


def month_header(month: QuoteMonth) -> str:
    return f"{month.year}年{month.month}月结算报价（元/台）"
```

The function must return issues instead of raising for unreadable files, wrong tables, missing Base-A header, Marketing column I, BOP column K, and missing required historical months. Marketing and BOP keys are identified by their approved Excel column positions, not by a literal header named `I` or `K`. It must flag blank/duplicate codes as row issues and leave duplicate Base rows available for later generation.

- [ ] **Step 4: Run precheck tests including success and duplicate-row cases**

Run: `python -m pytest tests/unit/test_precheck.py -v`

Expected: all precheck tests PASS, including a valid August input with March and July headers.

- [ ] **Step 5: Commit the precheck boundary**

```bash
git add src/quote_app/excel/source_reader.py src/quote_app/core/precheck.py tests/conftest.py tests/factories tests/unit/test_precheck.py
git commit -m "feat: validate monthly source workbooks"
```

## Task 4: Exact Data Association and Row-Level Issues

**Files:**
- Create: `src/quote_app/core/association.py`
- Create: `tests/unit/test_association.py`

**Interfaces:**
- Consumes: `SheetTable`, `QuoteMonth`, `QuoteRow`, `normalize_code`, `normalize_brand`.
- Produces: `associate_rows(base, marketing, bop, quote_month) -> list[QuoteRow]`.

- [ ] **Step 1: Write failing association tests**

```python
from quote_app.core.association import associate_records
from quote_app.domain.models import QuoteMonth


def test_base_rows_drive_count_order_and_exact_keys() -> None:
    base = [
        {"A": "9102", "2026年3月结算报价（元/台）": "无", "2026年7月结算报价（元/台）": 4299},
        {"A": "9101", "2026年3月结算报价（元/台）": 3999, "2026年7月结算报价（元/台）": 3899},
    ]
    marketing = [
        {"I": "9101", "C": "荣耀", "E": "HONOR 500", "AQ": "12GB", "AR": "256GB", "AS": "月光银"},
        {"I": "9102", "C": "小米", "E": "小米17", "AQ": "12GB", "AR": "256GB", "AS": "雪山粉"},
    ]
    bop = [{"K": "9101", "F": "F1", "G": "G1"}]
    rows = associate_records(base, marketing, bop, QuoteMonth(2026, 8))
    assert [row.material_code for row in rows] == ["9102", "9101"]
    assert rows[0].cells["B"] == "小米"
    assert rows[0].cells["F"] == "申请配置"
    assert rows[0].cells["G"] == "申请配置"
```

- [ ] **Step 2: Run the focused test and verify the association function is missing**

Run: `python -m pytest tests/unit/test_association.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement deterministic index building and row mapping**

```python
from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def build_index(records: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        normalized = normalize_code(record.get(key))
        if normalized:
            index[normalized].append(record)
    return dict(index)
```

`associate_records` must:

- enumerate Base records without deduplication;
- set quotation C from Base A;
- use Marketing I and BOP K exact indexes;
- apply this exact source-to-output mapping:

```python
FIXED_COLUMN_MAP = {
    "A": ("marketing", "M"),
    "B": ("marketing", "C"),
    "C": ("base", "A"),
    "D": ("marketing", "B"),
    "E": ("marketing", "E"),
    "F": ("bop", "Z"),
    "G": ("bop", "Y"),
    "H": ("marketing", "V"),
    "O": ("marketing", "X"),
    "AB": ("base", "I"),
    "AC": ("base", "J"),
    "AD": ("base", "K"),
    "AE": ("base", "L"),
    "AF": ("base", "M"),
    "AG": ("base", "B"),
}

WEB_QUERY_MAP = {
    "brand": ("marketing", "C"),
    "model_name": ("marketing", "E"),
    "ram": ("marketing", "AQ"),
    "storage": ("marketing", "AR"),
    "color": ("marketing", "AS"),
}
```

- populate quotation I and J from the Base columns whose headers match quote-month-minus-five and quote-month-minus-one, regardless of their physical column positions;
- normalize approved brand aliases;
- set both F and G to `申请配置` when BOP has no exact match;
- attach `MARKETING_NOT_FOUND`, `MARKETING_CONFLICT`, `BOP_CONFLICT`, or `WEB_FIELDS_MISSING` row issues;
- retain the row and skip unsafe candidate selection when an index contains multiple conflicting records.

- [ ] **Step 4: Run association tests for missing, conflict, duplicate Base, and brand alias cases**

Run: `python -m pytest tests/unit/test_association.py -v`

Expected: all association tests PASS.

- [ ] **Step 5: Commit exact association behavior**

```bash
git add src/quote_app/core/association.py tests/unit/test_association.py
git commit -m "feat: associate quotation rows by material code"
```

## Task 5: Formula Generation and Business Decisions

**Files:**
- Create: `src/quote_app/core/formulas.py`
- Create: `tests/unit/test_formulas.py`

**Interfaces:**
- Consumes: one-based Excel row number.
- Produces: `formula_cells(row_number) -> dict[str, str]`, `is_new(j_value, f_value) -> str`, `needs_six_month_adjustment(i_value) -> str`, and `select_minimum_offer(channel_results)`.

- [ ] **Step 1: Write failing rule tests**

```python
from quote_app.core.formulas import (
    formula_cells,
    is_new,
    needs_six_month_adjustment,
    select_minimum_offer,
)


def test_v_and_w_use_confirmed_rules() -> None:
    assert is_new("无", "已配置") == "是"
    assert is_new(3999, "申请配置") == "是"
    assert is_new(3999, "已配置") == "否"
    assert needs_six_month_adjustment("无") == "否"
    assert needs_six_month_adjustment(3999) == "是"


def test_formula_cells_keep_manual_blanks_safe() -> None:
    formulas = formula_cells(2)
    assert formulas["X"] == '=IF(K2="","",IF(J2="无","无",(K2-J2)/J2))'
    assert formulas["AA"] == '=IF(K2="","",K2-5)'


def test_tied_minimum_uses_jd_then_tmall_then_official() -> None:
    result = select_minimum_offer(
        {"AI": (3999, "https://jd.example"), "AJ": (3999, "https://tmall.example"), "AK": (4299, "https://brand.example")}
    )
    assert result == (3999, "https://jd.example")
```

- [ ] **Step 2: Run tests and verify formulas module is missing**

Run: `python -m pytest tests/unit/test_formulas.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement the confirmed formulas and tie priority**

```python
from numbers import Number
from typing import Any


def is_new(j_value: Any, f_value: Any) -> str:
    return "是" if j_value == "无" or f_value == "申请配置" else "否"


def needs_six_month_adjustment(i_value: Any) -> str:
    return "否" if i_value == "无" else "是"


def formula_cells(row_number: int) -> dict[str, str]:
    row = str(row_number)
    return {
        "X": f'=IF(K{row}="","",IF(J{row}="无","无",(K{row}-J{row})/J{row}))',
        "Y": f'=IF(K{row}="","",IF(I{row}="无","无",(K{row}-I{row})/I{row}))',
        "Z": f'=IF(OR(K{row}="",L{row}=""),"",(K{row}-L{row})/L{row})',
        "AA": f'=IF(K{row}="","",K{row}-5)',
    }


def select_minimum_offer(results: dict[str, tuple[Any, str]]) -> tuple[Any, str]:
    valid = [
        (price, priority, url)
        for priority, column in enumerate(("AI", "AJ", "AK"))
        if column in results
        for price, url in (results[column],)
        if isinstance(price, Number)
    ]
    if not valid:
        return "无", "无"
    price, _, url = min(valid, key=lambda item: (item[0], item[1]))
    return price, url
```

- [ ] **Step 4: Run formula tests and inspect exact formula strings**

Run: `python -m pytest tests/unit/test_formulas.py -v`

Expected: all formula tests PASS.

- [ ] **Step 5: Commit formula and decision rules**

```bash
git add src/quote_app/core/formulas.py tests/unit/test_formulas.py
git commit -m "feat: encode quotation formulas and decisions"
```

## Task 6: Build and Verify the Clean Quotation Template

**Files:**
- Create: `scripts/build_quote_template.py`
- Create: `resources/templates/quote_template.xlsx`
- Create: `tests/unit/test_template_builder.py`
- Create: `src/quote_app/excel/template_builder.py`

**Interfaces:**
- Consumes: approved sample workbook path at build time.
- Produces: a clean one-sheet template and `load_clean_template(path) -> Workbook`.

- [ ] **Step 1: Write a failing template invariant test**

```python
from openpyxl import load_workbook


def test_clean_template_has_only_5g_sheet_and_no_instruction_row() -> None:
    workbook = load_workbook("resources/templates/quote_template.xlsx")
    assert workbook.sheetnames == ["5G手机"]
    sheet = workbook["5G手机"]
    assert sheet.max_column >= 40
    assert sheet["A1"].value is not None
    assert sheet["A3"].value is None
```

- [ ] **Step 2: Run the test and verify the template file is missing**

Run: `python -m pytest tests/unit/test_template_builder.py -v`

Expected: FAIL with file-not-found.

- [ ] **Step 3: Implement the one-time template build script**

The script takes the approved sample path as an explicit argument:

```python
from argparse import ArgumentParser
from copy import copy
from pathlib import Path
from openpyxl import Workbook, load_workbook


def build_template(source: Path, destination: Path) -> None:
    source_book = load_workbook(source)
    source_sheet = source_book["5G手机"]
    target_book = Workbook()
    target_sheet = target_book.active
    target_sheet.title = "5G手机"
    for row in source_sheet.iter_rows(min_row=1, max_row=2, min_col=1, max_col=40):
        for source_cell in row:
            target = target_sheet[source_cell.coordinate]
            target.value = source_cell.value if source_cell.row == 1 else None
            target._style = copy(source_cell._style)
            target.number_format = source_cell.number_format
            target.alignment = copy(source_cell.alignment)
            target.protection = copy(source_cell.protection)
    for key, dimension in source_sheet.column_dimensions.items():
        target_sheet.column_dimensions[key].width = dimension.width
        target_sheet.column_dimensions[key].hidden = dimension.hidden
    for key, dimension in source_sheet.row_dimensions.items():
        if key <= 2:
            target_sheet.row_dimensions[key].height = dimension.height
    target_sheet.freeze_panes = source_sheet.freeze_panes
    target_sheet.sheet_properties = copy(source_sheet.sheet_properties)
    target_sheet.page_margins = copy(source_sheet.page_margins)
    target_sheet.page_setup = copy(source_sheet.page_setup)
    target_sheet.print_options = copy(source_sheet.print_options)
    target_sheet.sheet_format = copy(source_sheet.sheet_format)
    target_sheet.print_title_rows = source_sheet.print_title_rows
    target_sheet.print_title_cols = source_sheet.print_title_cols
    target_sheet.sheet_view.showGridLines = source_sheet.sheet_view.showGridLines
    for merged_range in source_sheet.merged_cells.ranges:
        if merged_range.max_row <= 2:
            target_sheet.merge_cells(str(merged_range))
    destination.parent.mkdir(parents=True, exist_ok=True)
    target_book.save(destination)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build_template(args.source, args.destination)
```

Run:

```bash
python scripts/build_quote_template.py \
  "/Users/yangguowei/Desktop/铺货报价系统/铺货报价需求书2026.7.24/2026年8月终端供货价报价表.xlsx" \
  "resources/templates/quote_template.xlsx"
```

- [ ] **Step 4: Run invariants and visually compare template rendering**

Run: `python -m pytest tests/unit/test_template_builder.py -v`

Expected: PASS.

Open both sample and clean template in Excel/WPS and verify header style, merged cells, A:AN widths, row-2 style, freeze panes, print titles, margins, page setup, and print settings match. Record the check in `tests/fixtures/template_visual_check.md` with the tested application and date.

- [ ] **Step 5: Commit the clean template and builder**

```bash
git add scripts/build_quote_template.py resources/templates/quote_template.xlsx src/quote_app/excel/template_builder.py tests/unit/test_template_builder.py tests/fixtures/template_visual_check.md
git commit -m "feat: add clean quotation workbook template"
```

## Task 7: Quotation Workbook Writer

**Files:**
- Create: `src/quote_app/excel/quote_writer.py`
- Create: `tests/unit/test_quote_writer.py`

**Interfaces:**
- Consumes: `QuoteMonth`, ordered `list[QuoteRow]`, clean template path, optional channel results.
- Produces: `write_quote_workbook(request) -> Path`.

- [ ] **Step 1: Write a failing end-to-end writer test**

```python
from openpyxl import load_workbook
from quote_app.domain.models import QuoteMonth, QuoteRow
from quote_app.excel.quote_writer import QuoteWriteRequest, write_quote_workbook


def test_writer_keeps_one_sheet_rows_formulas_and_manual_blanks(tmp_path) -> None:
    rows = [
        QuoteRow(2, "9101", {"C": "9101", "F": "已配置", "I": 3999, "J": 3899}),
        QuoteRow(3, "9102", {"C": "9102", "F": "申请配置", "I": "无", "J": "无"}),
    ]
    output = write_quote_workbook(
        QuoteWriteRequest(
            quote_month=QuoteMonth(2026, 8),
            rows=rows,
            template_path="resources/templates/quote_template.xlsx",
            output_dir=tmp_path,
        )
    )
    workbook = load_workbook(output, data_only=False)
    sheet = workbook["5G手机"]
    assert workbook.sheetnames == ["5G手机"]
    assert sheet.max_row == 3
    assert sheet["C2"].value == "9101"
    assert sheet["K2"].value is None
    assert sheet["V3"].value == "是"
    assert sheet["W3"].value == "否"
    assert sheet["X2"].value.startswith("=IF(")
```

- [ ] **Step 2: Run the writer test and verify it fails**

Run: `python -m pytest tests/unit/test_quote_writer.py -v`

Expected: FAIL because `QuoteWriteRequest` is missing.

- [ ] **Step 3: Implement style replication, formulas, validation, and safe naming**

```python
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.worksheet.datavalidation import DataValidation


@dataclass(frozen=True, slots=True)
class QuoteWriteRequest:
    quote_month: QuoteMonth
    rows: list[QuoteRow]
    template_path: str | Path
    output_dir: Path


def copy_row_style(sheet, source_row: int, target_row: int) -> None:
    for column in range(1, 41):
        source = sheet.cell(source_row, column)
        target = sheet.cell(target_row, column)
        target._style = copy(source._style)
        target.alignment = copy(source.alignment)
        target.number_format = source.number_format
    sheet.row_dimensions[target_row].height = sheet.row_dimensions[source_row].height
```

`write_quote_workbook` must:

- duplicate row-2 style for each output row;
- populate mapped cells and formulas;
- set T and U to `无`;
- calculate V and W from the confirmed rules;
- keep K, L, M, N, P, and Q empty;
- add N validation formula `"货源充足,货源紧缺,新品上市,尾货期"`;
- set X, Y, and Z number format to `0.00%`;
- update only year/month header text;
- generate `YYYY年MM月终端供货价报价表.xlsx`;
- append `YYYYMMDD-HHMMSS` before `.xlsx` if the target already exists.

- [ ] **Step 4: Run writer tests and inspect the generated workbook**

Run: `python -m pytest tests/unit/test_quote_writer.py -v`

Expected: all writer tests PASS.

Use openpyxl assertions to compare row-2 font, fill, border, alignment, widths, heights, freeze panes, and print settings with the template.

- [ ] **Step 5: Commit the quotation writer**

```bash
git add src/quote_app/excel/quote_writer.py tests/unit/test_quote_writer.py
git commit -m "feat: generate formatted quotation workbook"
```

## Task 8: Execution Report and Core Pipeline

**Files:**
- Create: `src/quote_app/excel/report_writer.py`
- Create: `src/quote_app/services/core_pipeline.py`
- Create: `tests/unit/test_report_writer.py`
- Create: `tests/integration/test_core_pipeline.py`
- Create: `README.md`

**Interfaces:**
- Consumes: `InputPaths`, `QuoteMonth`, precheck, association, formula and quotation writer services.
- Produces: `CoreRunResult(quote_path, report_path, summary, rows)` and two usable Excel outputs.

- [ ] **Step 1: Write failing report and pipeline tests**

```python
from openpyxl import load_workbook
from quote_app.excel.report_writer import ReportWriteRequest, write_execution_report


def test_report_has_overview_detail_filters_and_status_colors(tmp_path) -> None:
    output = write_execution_report(
        ReportWriteRequest(
            quote_month=QuoteMonth(2026, 8),
            rows=[QuoteRow(2, "9101", {"B": "小米", "AB": "产品经理甲"})],
            output_dir=tmp_path,
        )
    )
    workbook = load_workbook(output)
    assert workbook.sheetnames == ["运行总览", "处理明细"]
    assert workbook["处理明细"].auto_filter.ref is not None
```

```python
from quote_app.services.core_pipeline import run_core_pipeline


def test_core_pipeline_emits_partial_quote_and_report_for_row_issue(valid_inputs) -> None:
    result = run_core_pipeline(valid_inputs.paths, QuoteMonth(2026, 8))
    assert result.quote_path.exists()
    assert result.report_path.exists()
    assert result.summary.total_rows == len(valid_inputs.base_codes)
```

- [ ] **Step 2: Run tests and verify report and pipeline imports fail**

Run: `python -m pytest tests/unit/test_report_writer.py tests/integration/test_core_pipeline.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement report layout and orchestration**

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunSummary:
    total_rows: int
    completed_rows: int
    partial_rows: int
    failed_rows: int
    unsupported_rows: int
    manual_supplement_rows: int


@dataclass(frozen=True, slots=True)
class CoreRunResult:
    quote_path: Path
    report_path: Path
    summary: RunSummary
    rows: list[QuoteRow]
```

The report writer must create:

- `运行总览` with run metadata, totals, brand summary, manager summary, and JD/Tmall/official counters;
- `处理明细` with manager, brand, code, product/configuration, three source-link states, three channel states, final state, failed step, reason, and recommendation;
- green fill for completed, orange for partial/unsupported, red for failed;
- frozen headers, auto-filter, wrapped text, and readable column widths.

`run_core_pipeline` must stop before output on fatal precheck issues, continue on row issues, and always generate both workbooks when at least one Base row is valid.

- [ ] **Step 4: Run the complete core suite**

Run: `python -m pytest tests/unit tests/integration/test_core_pipeline.py -v`

Expected: all tests PASS.

Run: `python -m ruff check src tests scripts`

Expected: `All checks passed!`

Run: `python -m mypy src/quote_app`

Expected: success with no type errors.

- [ ] **Step 5: Document the core-only run and commit**

Add to `README.md`:

```markdown
## Core validation stage

The core stage reads the three monthly workbooks, performs local precheck and association,
and produces a quotation workbook plus execution report. Website columns AI:AN remain
unresolved until the browser plans are implemented.
```

```bash
git add src/quote_app/excel/report_writer.py src/quote_app/services/core_pipeline.py tests README.md
git commit -m "feat: complete core quotation pipeline"
```

## Plan 1 Completion Gate

Run:

```bash
python -m pytest -v
python -m ruff check src tests scripts
python -m mypy src/quote_app
```

Then run the core pipeline against copies of the supplied three sample files and verify:

- output row count equals non-empty Base A count;
- the sample order is unchanged;
- quotation workbook contains only `5G手机`;
- manual columns remain blank;
- March/July/August headers and formulas are correct for August 2026;
- report totals equal detail counts;
- no input file is modified.

Commit the recorded acceptance evidence:

```bash
git add docs/testing/core-sample-acceptance.md
git commit -m "test: record core sample acceptance"
```

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

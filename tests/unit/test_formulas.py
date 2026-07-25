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
    assert formula_cells(2) == {
        "X": '=IF(K2="","",IF(J2="无","无",(K2-J2)/J2))',
        "Y": '=IF(K2="","",IF(I2="无","无",(K2-I2)/I2))',
        "Z": '=IF(OR(K2="",L2=""),"",(K2-L2)/L2)',
        "AA": '=IF(K2="","",K2-5)',
    }


def test_formula_cells_use_the_requested_excel_row_number() -> None:
    assert formula_cells(37) == {
        "X": '=IF(K37="","",IF(J37="无","无",(K37-J37)/J37))',
        "Y": '=IF(K37="","",IF(I37="无","无",(K37-I37)/I37))',
        "Z": '=IF(OR(K37="",L37=""),"",(K37-L37)/L37)',
        "AA": '=IF(K37="","",K37-5)',
    }


def test_all_invalid_channel_prices_return_no_offer() -> None:
    result = select_minimum_offer(
        {
            "AI": ("无", "https://jd.example"),
            "AJ": (None, "https://tmall.example"),
            "AK": ("4299", "https://brand.example"),
        }
    )

    assert result == ("无", "无")


def test_lowest_numeric_price_is_selected_across_channels() -> None:
    result = select_minimum_offer(
        {
            "AI": (4299, "https://jd.example"),
            "AJ": (4099, "https://tmall.example"),
            "AK": (3999, "https://brand.example"),
        }
    )

    assert result == (3999, "https://brand.example")


def test_tied_minimum_uses_jd_before_other_channels() -> None:
    result = select_minimum_offer(
        {
            "AI": (3999, "https://jd.example"),
            "AJ": (3999, "https://tmall.example"),
            "AK": (3999, "https://brand.example"),
        }
    )

    assert result == (3999, "https://jd.example")


def test_tied_minimum_uses_tmall_before_official_when_jd_is_invalid() -> None:
    result = select_minimum_offer(
        {
            "AI": ("无", "https://jd.example"),
            "AJ": (3999, "https://tmall.example"),
            "AK": (3999, "https://brand.example"),
        }
    )

    assert result == (3999, "https://tmall.example")


def test_single_valid_channel_price_is_selected() -> None:
    result = select_minimum_offer(
        {
            "AI": ("无", "https://jd.example"),
            "AJ": (None, "https://tmall.example"),
            "AK": (4299, "https://brand.example"),
        }
    )

    assert result == (4299, "https://brand.example")

from quote_app.services.screenshot_review import Recognition, assess


def test_only_visible_matching_content_passes():
    scan = Recognition(
        "done", "荣耀Magic8 16GB 512GB 天青釉 京东自营旗舰店 ¥4,999.00 加入购物车", 1600, 1000
    )
    result = assess(scan, "荣耀Magic8", "16GB / 512GB / 天青釉", "jd", "4999")
    assert all(result[code][0] == "通过" for code in ("C01", "C02", "C03", "C04"))


def test_existing_but_blank_or_unrecognizable_image_is_not_missing_or_passed():
    result = assess(Recognition("done", "", 1600, 1000), "荣耀Magic8", "512GB", "jd", "4999")
    assert all(v[0] == "待复核" for v in result.values())
    assert "识别" in result["C01"][2]


def test_price_in_unrelated_text_or_subsidy_does_not_validate_store():
    result = assess(
        Recognition("done", "荣耀Magic8 512GB ¥4999 补贴价", 1600, 1000),
        "荣耀Magic8",
        "512GB",
        "jd",
        "4999",
    )
    assert result["C03"][0] == "待复核"


def test_login_and_incomplete_spec_stay_actionable():
    result = assess(
        Recognition("done", "请完成安全验证 荣耀Magic8 256GB 京东自营 ¥4999", 1600, 1000),
        "荣耀Magic8",
        "512GB",
        "jd",
        "4999",
    )
    assert result["C01"][0] == "未通过"
    assert result["C02"][0] == "待复核"


def test_running_and_unavailable_are_not_missing_files():
    assert assess(Recognition("running"), "X", "", "jd", "")["C01"][0] == "未检查"
    assert (
        assess(Recognition("error", error="本机识别不可用"), "X", "", "jd", "")["C01"][0]
        == "待复核"
    )


def test_verified_no_model_does_not_require_a_product_price():
    result = assess(
        Recognition("done", "京东 搜索 REDMI R70 5G 暂无匹配结果", 1600, 1000),
        "REDMI R70 5G",
        "4GB / 128GB",
        "jd",
        "",
        outcome="no_model",
    )
    assert result["C01"][0] == "通过"
    assert result["C02"][0] == result["C03"][0] == "不适用"
    unknown = assess(
        Recognition("done", "京东 搜索 REDMI R70 5G", 1600, 1000),
        "REDMI R70 5G",
        "4GB / 128GB",
        "jd",
        "",
    )
    assert unknown["C03"][0] == "待复核"

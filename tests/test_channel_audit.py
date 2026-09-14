from quote_app.desktop_state import TaskRow
from quote_app.services.screenshot_review import Recognition


def task(**values):
    return TaskRow(
        "t",
        model_name="荣耀Magic8",
        channel="jd",
        price="4999",
        state="succeeded",
        outcome="price_found",
        url="https://item.jd.com/123.html",
        **values,
    )


def test_channel_has_four_nonduplicated_checks_and_named_store():
    from quote_app.services.channel_audit import inspect_channel

    scan = Recognition(
        "done",
        "京东荣耀自营旗舰店\n荣耀Magic8 16GB 512GB 天青釉\n售价 ¥4999\n加入购物车",
        1600,
        1000,
    )
    checks = inspect_channel(task(), "荣耀Magic8", "16GB / 512GB / 天青釉", scan, "4999")
    assert [c.code for c in checks] == ["B01", "B02", "B03", "C01"]
    assert all(c.status == "通过" for c in checks)
    assert checks[0].title == "京东店铺核验"


def test_unrelated_promotion_does_not_taint_the_adopted_normal_price():
    from quote_app.services.channel_audit import inspect_channel

    scan = Recognition(
        "done",
        "广告 国补到手价¥999\n京东荣耀自营旗舰店\n荣耀Magic8 512GB\n普通售价 ¥4999",
        1600,
        1000,
    )
    checks = inspect_channel(task(), "荣耀Magic8", "512GB", scan, "4999")
    assert checks[2].status == "通过"
    scan = Recognition("done", scan.text.replace("普通售价 ¥4999", "补贴后 ¥4999"), 1600, 1000)
    assert inspect_channel(task(), "荣耀Magic8", "512GB", scan, "4999")[2].status == "待复核"


def test_missing_screenshot_has_only_one_actionable_item():
    from quote_app.services.channel_audit import inspect_channel

    checks = inspect_channel(task(), "荣耀Magic8", "512GB", None, "4999")
    assert [c.code for c in checks if c.status in ("未通过", "待补充", "待复核")] == ["C01"]
    assert all(c.status == "未检查" for c in checks[:3])


def test_wrong_store_domain_and_written_price_are_not_passed():
    from quote_app.services.channel_audit import inspect_channel

    t = task()
    t.url = "https://item.jd.com.evil.example/123"
    scan = Recognition("done", "京东荣耀自营旗舰店 荣耀Magic8 512GB\n售价¥4999", 1600, 1000)
    checks = inspect_channel(t, "荣耀Magic8", "512GB", scan, "4998")
    assert checks[0].status == "未通过"
    assert checks[2].status == "未通过"


def test_missing_memory_is_one_spec_question_not_a_quality_failure():
    from quote_app.services.channel_audit import inspect_channel

    scan = Recognition("done", "京东荣耀自营旗舰店 荣耀Magic8\n售价¥4999", 1600, 1000)
    checks = inspect_channel(task(), "荣耀Magic8", "512GB", scan, "4999")
    assert checks[1].status == "待复核"
    assert checks[3].status == "通过"


def test_promotion_and_plain_price_on_same_line_use_the_matching_label():
    from quote_app.services.channel_audit import inspect_channel

    scan = Recognition(
        "done", "京东荣耀自营旗舰店 荣耀Magic8 512GB\n补贴后¥3999 普通售价¥4999", 1600, 1000
    )
    assert inspect_channel(task(), "荣耀Magic8", "512GB", scan, "4999")[2].status == "通过"


def test_login_page_waits_for_one_valid_screenshot():
    from quote_app.services.channel_audit import inspect_channel

    scan = Recognition("done", "荣耀Magic8 512GB 京东 请完成安全验证 售价¥4999", 1600, 1000)
    checks = inspect_channel(task(), "荣耀Magic8", "512GB", scan, "4999")
    assert [c.status for c in checks] == ["未检查", "未检查", "未检查", "未通过"]


def test_china_apple_official_domain_is_allowed_but_lookalike_is_not():
    from quote_app.services.channel_audit import inspect_channel

    t = task()
    t.channel = "official"
    t.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-17"
    scan = Recognition("done", "Apple iPhone 17 黑色 256GB\n售价¥4999", 1600, 1000)
    assert inspect_channel(t, "iPhone 17", "256GB", scan, "4999")[0].status == "通过"
    t.url = "https://www.apple.com.cn.evil.example/shop"
    assert inspect_channel(t, "iPhone 17", "256GB", scan, "4999")[0].status == "未通过"


def test_adjacent_subsidy_label_keeps_unlabelled_reference_price_for_review():
    from quote_app.services.channel_audit import inspect_channel

    scan = Recognition(
        "done",
        "荣耀京东自营旗舰店\n荣耀Magic8 16GB+512GB 天青釉\n¥4499国补领后价\n¥4999\n最高返224京豆",
        1600,
        1000,
    )
    checks = inspect_channel(task(), "荣耀Magic8", "16GB / 512GB / 天青釉", scan, "4999")
    assert checks[0].status == "通过"
    assert checks[1].status == "通过"
    assert checks[2].status == "待复核"


def test_unlabelled_price_under_subsidy_banner_needs_price_review():
    from quote_app.services.channel_audit import inspect_channel
    scan = Recognition('done', '荣耀京东自营旗舰店\n国家补贴\n领后减¥500立即领取\n荣耀Magic8 512GB\n¥4999\n可再享：\n最高返224京豆', 1600, 1000)
    assert inspect_channel(task(), '荣耀Magic8', '512GB', scan, '4999')[2].status == '待复核'


def test_system_permission_prompt_is_failed_evidence_not_approved_page():
    from quote_app.services.channel_audit import inspect_channel
    scan = Recognition('done', '荣耀Magic8 16GB 512GB 天青釉\n京东荣耀自营旗舰店\n售价¥4999\n正在请求绕过系统无痕浏览窗口选择器，直接访问屏幕和音频。\n打开系统设置\n允许', 3840, 2160)
    checks = inspect_channel(task(), '荣耀Magic8', '16GB / 512GB / 天青釉', scan, '4999')
    assert checks[-1].status == '未通过'
    assert '权限弹窗' in checks[-1].reason
    assert all(c.status == '未检查' for c in checks[:3])

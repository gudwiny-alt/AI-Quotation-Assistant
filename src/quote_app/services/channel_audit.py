"""Four channel checks from saved task facts and offline screenshot recognition."""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from quote_app.services.review_workbook import money


@dataclass
class Finding:
    code: str
    title: str
    status: str
    comparison: str = ""
    reason: str = ""


def compact(value):
    return re.sub(r"[\s·/＋+_-]", "", str(value)).lower()


def inspect_channel(task, title, specification, scan, written_price):
    channel = task.channel
    name = {"jd": "京东", "tmall": "天猫", "official": "官网"}.get(channel, channel)
    checks = [
        Finding(code, name + label, "未检查")
        for code, label in (
            ("B01", "来源核验" if channel == "official" else "店铺核验"),
            ("B02", "商品规格核验"),
            ("B03", "取价准确性核验"),
            ("C01", "截图证据检查"),
        )
    ]
    store, sku, price, evidence = checks
    if (
        scan is None
        or scan.state != "done"
        or scan.width < 800
        or scan.height < 500
        or len(scan.text.strip()) < 10
    ):
        evidence.status = (
            "待补充" if scan is None else "未检查" if scan.state == "running" else "待复核"
        )
        evidence.reason = (
            ("本次未保存可读取的截图" + (f"；任务原因：{task.error}" if task.error else ""))
            if scan is None
            else (
                "已有截图，正在本机识别"
                if scan.state == "running"
                else scan.error or "截图尺寸或文字清晰度不足，请打开原图核对"
            )
        )
        for check in checks[:3]:
            check.reason = "等待截图证据检查完成后自动核验"
        return checks
    text = scan.text
    normalized = compact(text)
    system_prompt = ("打开系统设置" in normalized and
                     ("正在请求绕过系统" in normalized or "录制屏幕和系统音频" in normalized))
    if system_prompt:
        evidence.status = "未通过"
        evidence.comparison = "截图包含macOS权限弹窗"
        evidence.reason = "系统权限弹窗遮挡商品页面；请完成系统授权、关闭弹窗后重新截图，不能以文件已保存代替证据合格"
        for check in checks[:3]:
            check.reason = "等待补充无遮挡的商品页面截图"
        return checks
    auth = any(
        word in text for word in ("请完成安全验证", "请先登录", "拖动滑块完成拼图", "访问过于频繁")
    )
    evidence.status = "未通过" if auth else "通过"
    evidence.comparison = f"{scan.width} × {scan.height}；识别到 {len(text)} 字"
    evidence.reason = (
        "截图仍为登录或验证页，请补充有效页面"
        if auth
        else "截图文件可解码，尺寸及文字可读；商品、店铺和价格分别核验"
    )
    if auth:
        for check in checks[:3]:
            check.reason = "等待有效页面截图"
        return checks
    host = (urlsplit(task.url).hostname or "").lower()
    brands = {
        "荣耀": ("honor.com", "hihonor.com"),
        "华为": ("vmall.com", "huawei.com"),
        "oppo": ("oppo.com", "opposhop.cn"),
        "vivo": ("vivo.com", "vivo.com.cn"),
        "redmi": ("mi.com",),
        "小米": ("mi.com",),
        "iphone": ("apple.com", "apple.com.cn"),
        "苹果": ("apple.com", "apple.com.cn"),
    }
    brand = next((b for b in brands if b in title.lower()), "")
    domains = (
        ("jd.com",)
        if channel == "jd"
        else ("tmall.com",)
        if channel == "tmall"
        else brands.get(brand, ())
    )
    host_ok = bool(host and any(host == d or host.endswith("." + d) for d in domains))
    no_model = task.state == "succeeded" and task.outcome == "no_model"
    visible_store = (channel == "official" and host_ok) or any(
        (("自营" in line or "官方旗舰店" in line) if channel == "jd" else "官方旗舰店" in line)
        and (brand in line.lower() if brand else False)
        for line in text.splitlines()
    )
    store.status = (
        "未通过"
        if host and domains and not host_ok
        else "通过"
        if host_ok and (visible_store or no_model)
        else "待复核"
    )
    store.comparison = task.url or "未关联来源链接"
    store.reason = (
        "来源域名不属于目标渠道"
        if store.status == "未通过"
        else "来源域名及品牌店铺文字符合规定渠道"
        if visible_store
        else "无匹配机型搜索记录：核验渠道域名"
        if no_model and host_ok
        else "未同时识别品牌官方店铺与对应渠道，请对照原图核对"
    )
    if no_model:
        for check in (sku, price):
            check.status = "不适用"
            check.reason = "已关联采集任务：该渠道无匹配机型，不采用其他商品报价"
        if compact(title) not in normalized:
            evidence.status = "待复核"
            evidence.reason = "无匹配机型记录已关联，但搜索截图未清晰识别目标机型，请核对搜索内容"
        return checks
    missing = [
        part.strip()
        for part in specification.split("/")
        if part.strip() and compact(part) not in normalized
    ]
    sku.status = (
        "通过"
        if len(compact(title)) >= 3
        and compact(title) in normalized
        and specification
        and not missing
        else "待复核"
    )
    sku.comparison = f"{title} · {specification}"
    sku.reason = (
        "截图文字与目标机型、容量和颜色一致"
        if sku.status == "通过"
        else "未完整识别目标机型及规格：" + ("、".join(missing) or title) + "；请核对当前选中配置"
    )
    adopted, written = money(task.price), money(written_price)
    found, conditional = False, False
    # Each amount is paired with its preceding label, never another advertisement.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        previous_end = 0
        for match in re.finditer(r"[¥￥]\s*([\d,]+(?:\.\d+)?)", line):
            if money(match[1].replace(",", "")) == adopted and adopted is not None:
                found = True
                label = line[previous_end : match.start()]
                tail = line[match.end() :].split("¥")[0].split("￥")[0]
                if not label.strip() and not tail.strip():
                    label += "\n".join(
                        lines[max(0, index - 4) : index] + lines[index + 1 : index + 2]
                    )
                label += tail
                conditional |= any(
                    word in label
                    for word in ("补贴", "国补", "券后", "到手", "会员", "分期", "划线", "原价")
                )
            previous_end = match.end()
    price.status = (
        "未通过"
        if adopted is not None and written is not None and adopted != written
        else "通过"
        if adopted is not None and adopted == written and found and not conditional
        else "待复核"
    )
    price.comparison = f"采集价 {task.price or '无'}；表内价 {written_price or '无'}"
    price.reason = (
        "采集价与表内回填价不一致"
        if price.status == "未通过"
        else "已识别采用的售价，与表内数值一致，未识别附带优惠条件"
        if price.status == "通过"
        else "采用价格附近存在补贴、优惠或特殊条件，请核对是否符合普通售价口径"
        if conditional
        else "未清晰识别采用的售价或缺少表内数值，请对照原图核验"
    )
    return checks

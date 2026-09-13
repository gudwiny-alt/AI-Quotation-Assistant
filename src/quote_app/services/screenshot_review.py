"""Offline screenshot text checks. Recognition is asynchronous and never drives collection."""

from dataclasses import dataclass
from pathlib import Path
from queue import Queue
import re
import sys
from threading import Lock, Thread

from PIL import Image

from quote_app.services.review_workbook import digest, money


@dataclass(frozen=True)
class Recognition:
    state: str
    text: str = ""
    width: int = 0
    height: int = 0
    error: str = ""


def recognize(path):
    """Use the system Vision framework through the already bundled PyObjC runtime."""
    with Image.open(path) as picture:
        width, height = picture.size
        picture.verify()
    if sys.platform != "darwin":
        return Recognition(
            "error",
            width=width,
            height=height,
            error="当前系统尚未配置本机截图识别，请打开原图复核",
        )
    import objc
    from Foundation import NSBundle, NSURL

    with objc.autorelease_pool():
        if not NSBundle.bundleWithPath_("/System/Library/Frameworks/Vision.framework").load():
            raise RuntimeError("本机文字识别框架无法加载")
        objc.registerMetaDataForSelector(
            b"VNImageRequestHandler",
            b"performRequests:error:",
            {"arguments": {3: {"type_modifier": b"o"}}},
        )
        request = objc.lookUpClass("VNRecognizeTextRequest").alloc().init()
        request.setRecognitionLevel_(0)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        request.setUsesLanguageCorrection_(False)
        handler = (
            objc.lookUpClass("VNImageRequestHandler")
            .alloc()
            .initWithURL_options_(NSURL.fileURLWithPath_(str(Path(path).resolve())), {})
        )
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(
                "本机文字识别未完成" + (f"：{error}" if error else "，请重试或打开原图复核")
            )
        lines = [
            str(candidates[0].string())
            for item in (request.results() or ())
            if (candidates := item.topCandidates_(1)) and candidates[0].confidence() >= 0.45
        ]
        return Recognition("done", "\n".join(lines), width, height)


_queue = Queue()
_lock = Lock()
_cache = {}
_started = False
_revision = 0


def revision():
    with _lock:
        return _revision


def _worker():
    global _revision
    while True:
        key, path = _queue.get()
        try:
            result = recognize(path)
        except Exception as error:
            result = Recognition("error", error=f"本机识别未完成：{error}")
        with _lock:
            _cache[key] = result
            _revision += 1
        _queue.task_done()


def request_scan(path):
    global _started
    key = digest(path)
    if key == "missing":
        return Recognition("error", error="截图文件无法读取")
    with Image.open(path) as picture:
        if picture.width < 300 or picture.height < 200:
            return Recognition(
                "done",
                width=picture.width,
                height=picture.height,
                error="图片尺寸过小，无法可靠自动核验",
            )
    with _lock:
        if key not in _cache:
            # Bound memory without invalidating jobs currently in the queue.
            if len(_cache) >= 256:
                for old in [k for k, v in _cache.items() if v.state != "running"][:128]:
                    del _cache[old]
            _cache[key] = Recognition("running")
            _queue.put((key, Path(path)))
        if not _started:
            Thread(target=_worker, name="quotation-screenshot-review", daemon=True).start()
            _started = True
        return _cache[key]


def _compact(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value).lower())


def assess(scan, title, specification, channel, price, *, outcome=""):
    """Passing means these visible fields matched, not that OCR proves all price policy."""
    codes = ("C01", "C02", "C03", "C04")
    if scan.state != "done":
        status = "未检查" if scan.state == "running" else "待复核"
        reason = "已有截图，正在本机识别内容，请稍候" if scan.state == "running" else scan.error
        return {code: (status, reason, reason) for code in codes}
    text = scan.text
    compact = _compact(text)
    model = _compact(title)
    model_ok = len(model) >= 3 and model in compact
    auth = any(
        word in text for word in ("请完成安全验证", "请先登录", "拖动滑块完成拼图", "访问过于频繁")
    )
    parts = [part.strip() for part in specification.split("/") if part.strip()]
    specs_ok = bool(parts) and all(_compact(part) in compact for part in parts)
    names = {
        "jd": ("京东自营", "京东自营旗舰店"),
        "tmall": ("官方旗舰店",),
        "official": ("vmall.com", "honor.com", "oppo.com", "vivo.com", "mi.com", "apple.com"),
    }
    store_ok = any(_compact(name) in compact for name in names.get(channel, ()))
    # Require currency-marked amounts. Unrelated bare numbers cannot validate prices.
    amounts = [money(v.replace(",", "")) for v in re.findall(r"[¥￥]\s*([\d,]+(?:\.\d+)?)", text)]
    price_ok = money(price) is not None and money(price) in amounts
    conditional = any(
        word in text for word in ("补贴", "券后", "到手价", "划线价", "会员价", "分期")
    )
    result = {
        "C01": (
            "未通过" if auth else "通过" if model_ok else "待复核",
            f"目标商品：{title}",
            "截图出现登录或验证提示，请补充有效商品页"
            if auth
            else "已从截图文字识别到目标机型"
            if model_ok
            else "已有截图，但未清晰识别到完整目标机型，请打开原图核对",
        ),
        "C02": (
            "通过" if specs_ok and model_ok and not auth else "待复核",
            specification or "缺少目标规格",
            "截图已识别目标机型及全部规格文字"
            if specs_ok and model_ok and not auth
            else "尚未同时识别到目标机型、容量和颜色；请核对选中规格，不能用其他配置文字代替",
        ),
        "C03": (
            "通过"
            if price_ok and store_ok and model_ok and not conditional and not auth
            else "待复核",
            f"表内价格：{price or '无数值价格'}；价格文字{'已找到' if price_ok else '未明确找到'}；来源文字{'已找到' if store_ok else '待核对'}",
            "目标机型、来源文字和价格文字已对照；仅为截图文字核验"
            if price_ok and store_ok and model_ok and not conditional and not auth
            else "存在优惠/补贴等价格条件，需核对采用的普通售价"
            if conditional
            else "价格或规定来源文字不清晰，请对照原图复核",
        ),
        "C04": (
            "通过"
            if scan.width >= 800
            and scan.height >= 500
            and model_ok
            and price_ok
            and store_ok
            and not auth
            else "待复核",
            f"{scan.width} × {scan.height}；识别到 {len(text)} 字",
            "已检查图片尺寸及机型、价格、来源关键文字是否可识别；边缘或其他内容仍可打开原图查看",
        ),
    }
    if outcome == "no_model":
        # Only restored task provenance can supply this outcome; a blank workbook
        # price is never interpreted as proof that the requested model is absent.
        for code in ("C02", "C03"):
            result[code] = (
                "不适用",
                "采集结果：无匹配机型",
                "该渠道没有目标商品报价，不要求商品详情价格与选中规格；搜索截图内容另项核验",
            )
        result["C01"] = (
            "未通过" if auth else "通过" if model_ok else "待复核",
            f"搜索目标：{title}",
            "对照已保存的无匹配机型任务及搜索文字；不认定其他机型价格为有效报价",
        )
        result["C04"] = (
            "通过"
            if model_ok and scan.width >= 800 and scan.height >= 500 and not auth
            else "待复核",
            f"{scan.width} × {scan.height}；搜索文字核验",
            "检查搜索截图尺寸与目标文字可读性；不要求出现目标商品售价",
        )
    return result

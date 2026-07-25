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

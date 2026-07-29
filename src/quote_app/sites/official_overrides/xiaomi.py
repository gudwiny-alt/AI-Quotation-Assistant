from __future__ import annotations

import unicodedata


class XiaomiOfficialOverride:
    """Accept only the bounded Xiaomi current-SKU selling-price context."""

    @staticmethod
    def accepts_price_context(context: str) -> bool:
        if not isinstance(context, str):
            return False
        return unicodedata.normalize("NFKC", context).strip() == "销售价"

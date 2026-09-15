"""A serializable business snapshot. No model output enters the business engine."""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import urlsplit

from .review_support import (
    RULE_VERSION,
    digest,
    money,
    periodic_adjustment,
    price_ceiling,
    profit_trial,
)

SECTION_TITLES = ("AI批次总结", "价格情报理解", "报价方案推理", "稽核异常分析")
CONTEXT_KEYS = (
    "category",
    "stock",
    "entry_date",
    "first_quote_date",
    "first_quote_price",
    "quote_month",
    "youfu",
    "warehouse_price",
    "previous_price",
    "history_prices",
)


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _text(value):
    # File paths and bearer-like secrets have no analytical value. Notes are data.
    text = str(value or "")
    text = re.sub(
        r"(?:/Users/|/private/|/var/|/tmp/|/Volumes/|/home/|/mnt/|/media/|[A-Za-z]:[\\/]|\\\\)[^\n；，）]*",
        "[本机文件]",
        text,
    )
    text = re.sub(r"https?://[^\s；，）]+", "[链接已隐藏]", text)
    text = re.sub(r"(?i)(?:bearer\s+|sk-)[a-z0-9_\-.]+", "[凭据已隐藏]", text)
    return text


def build_snapshot(session):
    """Evaluate a detached session so refresh/association cannot mutate the live session."""
    if session.running:
        raise ValueError("当前任务仍在运行，请完成后再生成批次分析。")
    if not session.products or session.quote_path is None:
        raise ValueError("尚无报价批次，请先完成报价或打开已生成的报价表。")
    version = digest(session.quote_path, fresh=True)
    if version == "missing" or version != session._version:
        raise ValueError("报价文件已变化或缺失，请重新打开本批次后生成分析。")
    if session.load_error:
        raise ValueError("本批次复核记录尚未正确加载，请重新打开并检查稽核数据。")
    current = copy.deepcopy(session)
    for product in current.products:
        for path in product.attachments + [
            c.evidence_path for c in product.channels if c.evidence_path
        ]:
            digest(path, fresh=True)
    checks = current.all_checks()
    sources = {"batch": "当前批次与数据覆盖", "rules": "当前程序报价规则与计算口径"}
    check_rows = []
    for check in checks:
        ref = "check:" + check.id
        sources[ref] = _text(check.title)
        manual = "；人工复核：" in check.reason
        # Reviewer identity is not needed; retain only the substantive reason.
        reason = check.reason.split("；人工复核：")[0]
        record = current._reviews.get(check.id, {})
        check_rows.append(
            dict(
                ref=ref,
                product_id=check.product_id,
                category=check.category,
                title=_text(check.title),
                channel=check.channel,
                status=check.status,
                comparison=_text(check.comparison),
                reason=_text(reason),
                human_review_applied=manual,
                human_review_reason=_text(record.get("reason", "")) if manual else "",
            )
        )
    products = []
    month = current.month
    next_year, next_zero = divmod(month.year * 12 + month.month, 12)
    next_date = f"{next_year:04d}-{next_zero + 1:02d}-01"
    for p in current.products:
        ref = "product:" + p.id
        sources[ref] = _text(p.title + " · " + p.specification)
        channels = []
        for index, task in enumerate(p.channels):
            channel_ref = f"{ref}:channel:{index}"
            sources[channel_ref] = _text(f"{p.title} · {task.channel}渠道记录")
            try:
                host = urlsplit(task.url).hostname or ""
            except ValueError:
                host = ""
            channels.append(
                dict(
                    ref=channel_ref,
                    channel=task.channel,
                    price=money(task.price),
                    outcome=task.outcome,
                    task_state=task.state,
                    evidence_state=task.evidence_state,
                    evidence_version=digest(task.evidence_path, fresh=True),
                    source_host=host,
                    has_source_link=bool(task.url),
                    manual_correction=task.state == "manual_corrected",
                )
            )
        ceiling, reason = price_ceiling(p)
        elapsed, reduction, limit = periodic_adjustment(p)
        projected = replace(p, context={**p.context, "quote_month": next_date})
        _, next_reduction, next_limit = periodic_adjustment(projected)
        products.append(
            dict(
                id=p.id,
                ref=ref,
                title=_text(p.title),
                specification=_text(p.specification),
                values={c: money(p.values.get(c)) for c in ("K", "L", "M", "P", "Q")},
                quotation_note=_text(p.values.get("AP", "")),
                attachment_count=len(p.attachments),
                attachment_versions=[digest(a, fresh=True) for a in p.attachments],
                context={k: _text(p.context.get(k, "")) for k in CONTEXT_KEYS},
                confirmation=current.confirmation_status(p),
                written=current.is_written(p),
                channels=channels,
                trial=profit_trial(p.values.get("L"), p.values.get("K")),
                ceiling=dict(
                    amount=ceiling, basis=reason, meaning="已知数值上限，不代表最终批准或最优报价"
                ),
                periodic=dict(elapsed_months=elapsed, reduction=reduction, limit=limit),
                next_month=dict(
                    month=next_date[:7],
                    reduction=next_reduction,
                    periodic_limit=next_limit,
                    conditions="仅沿用首次报价基准测算；未假设采购成本、销售数量和其他上限发生变化。",
                ),
            )
        )
    channels = [c for p in products for c in p["channels"]]
    payload = dict(
        schema_version=1,
        batch_id=current.batch_id,
        quote_month=f"{month.year:04d}-{month.month:02d}",
        workbook_version=version,
        rule_version=RULE_VERSION,
        stage="自动取价结束；报价及稽核进度以各商品当前状态为准，未推定已报送",
        units=dict(
            prices="元/台",
            margin="百分数，(K-L)/K×100",
            markup="百分数，(K-L)/L×100",
            reduction="比例，0.05表示下调5%",
            K="当月铺货结算报价",
            L="终端公司采购价",
            M="全国采购系统均价",
            P="渠道买断价",
            Q="分销零售价",
        ),
        rules=dict(
            phone_markup_max_percent="4.5",
            purchase_not_above_national_mean=True,
            quotation_caps="采购价×1.045、分销零售价、一级库建议零售价最高档、已提供历史报价、已取渠道最低价、周期调价上限取低；资格另行判断",
            periodic="首次报价月计为第1个月，第7个月起较首次报价下调5%，第13个月起下调10%",
            youfu="入一级库超过14个月，确认属于优福包后才列入不报价",
        ),
        coverage=dict(
            products=len(products),
            channel_records=len(channels),
            price_found=sum(
                c["outcome"] == "price_found" and c["price"] is not None for c in channels
            ),
            channel_outcomes=dict(Counter(c["outcome"] or "unknown" for c in channels)),
            checks=len(check_rows),
            check_statuses=dict(Counter(c["status"] for c in check_rows)),
            human_reviewed_checks=sum(c["human_review_applied"] for c in check_rows),
        ),
        missing_context=[
            "未提供销量、费用、返利及资金成本，不能推算整批利润",
            "未提供可比的渠道价格时间序列，不能判断价格趋势",
            "未向模型发送截图原图及附件内容；只能分析现有程序检查摘要，不能声称已阅图",
            "报价备注可能包含测试或占位文本，不能据此声称真实经营收益",
        ],
        products=products,
        checks=check_rows,
        sources=sources,
    )
    payload = _plain(payload)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["snapshot_id"] = hashlib.sha256(encoded.encode()).hexdigest()
    return payload
